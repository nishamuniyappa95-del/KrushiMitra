"""External services: weather, geocoding, Claude (chat/vision/translation), TTS, STT."""
import asyncio, base64, io, json, os, re, tempfile, time
from datetime import date, timedelta
import httpx
from . import config

LANGS = {"en": "English", "hi": "Hindi", "kn": "Kannada", "te": "Telugu", "ta": "Tamil", "ml": "Malayalam",
         "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "or": "Odia"}
WMO = {0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 51: "Light drizzle", 61: "Rain",
       63: "Moderate rain", 65: "Heavy rain", 80: "Rain showers", 95: "Thunderstorm", 96: "Thunderstorm with hail"}
_cache = {}

def lang_name(code): return LANGS.get(code, "English")

# ---------- Weather & location ----------
async def get_weather(lat, lon):
    key = (round(lat, 2), round(lon, 2))
    if key in _cache and time.time() - _cache[key][0] < 900:
        return _cache[key][1]
    async with httpx.AsyncClient(timeout=25) as c:
        f = await c.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
            "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"})
        f.raise_for_status()
        fj = f.json()
        climate = {"annual_rain": None, "avg_temp": None}
        try:
            end = date.today() - timedelta(days=3)
            a = await c.get("https://archive-api.open-meteo.com/v1/archive", params={
                "latitude": lat, "longitude": lon, "start_date": (end - timedelta(days=364)).isoformat(),
                "end_date": end.isoformat(), "daily": "temperature_2m_mean,precipitation_sum", "timezone": "auto"})
            d = a.json()["daily"]
            rain = [x for x in d["precipitation_sum"] if x is not None]
            temp = [x for x in d["temperature_2m_mean"] if x is not None]
            climate = {"annual_rain": round(sum(rain)), "avg_temp": round(sum(temp) / len(temp), 1)}
        except Exception:
            pass
    cur, dl = fj["current"], fj["daily"]
    mx, mn = dl["temperature_2m_max"], dl["temperature_2m_min"]
    out = {
        "current": {"temp_c": cur["temperature_2m"], "humidity": cur["relative_humidity_2m"], "rain_mm": cur["precipitation"],
                    "wind_kmh": cur["wind_speed_10m"], "condition": WMO.get(cur["weather_code"], "Unknown")},
        "forecast": [{"date": dl["time"][i], "tmax": mx[i], "tmin": mn[i], "rain_mm": dl["precipitation_sum"][i],
                      "wind_kmh": dl["wind_speed_10m_max"][i]} for i in range(len(mx))],
        "outlook": {"avg_temp_7d": round(sum((a + b) / 2 for a, b in zip(mx, mn)) / len(mx), 1),
                    "rain_7d_mm": round(sum(dl["precipitation_sum"]), 1)},
        "climate": climate}
    _cache[key] = (time.time(), out)
    return out

async def reverse_geocode(lat, lon):
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "KrushiMithra/1.0"}) as c:
            r = await c.get("https://nominatim.openstreetmap.org/reverse",
                            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10})
            a = r.json().get("address", {})
            return {"district": a.get("state_district") or a.get("county") or a.get("city"), "state": a.get("state"),
                    "country": a.get("country")}
    except Exception:
        return {"district": None, "state": None, "country": None}

# ---------- Claude ----------
_client = None
async def ask_llm(system, user, image=None, max_tokens=1500):
    """image = (bytes, media_type). Returns None when no API key / on failure so callers can fall back."""
    global _client
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        if _client is None:
            from anthropic import AsyncAnthropic
            _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
        content = []
        if image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": image[1],
                                                          "data": base64.b64encode(image[0]).decode()}})
        content.append({"type": "text", "text": user})
        r = await _client.messages.create(model=config.MODEL, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": content}])
        return "".join(b.text for b in r.content if b.type == "text")
    except Exception as e:
        print("LLM error:", e)
        return None

async def explain(context, instruction, language):
    """Farmer-friendly narrative in the farmer's language. Falls back to plain English if no LLM."""
    sys = (f"You are KrushiMithra, a friendly agriculture advisor for Indian farmers. Reply ONLY in {lang_name(language)}, "
           "using simple everyday words, short sentences, and no markdown. Be honest about risks. Use only the facts given.")
    text = await ask_llm(sys, f"{instruction}\n\nFACTS:\n{context}")
    return {"text": text or context, "language": language if text else "en"}

async def analyze_soil_photo(img: bytes, media_type: str, region: dict, previous: list):
    sys = "You are a soil scientist. Respond with ONLY a JSON object, no prose."
    prompt = ("Assess this soil photo. Region: %s. Previous crops: %s. JSON keys: texture (sandy|loam|clay|silty), "
              "color, moisture (dry|moist|wet), organic_matter (low|medium|high), ph_estimate (float 4.5-9), "
              "salinity_risk (low|medium|high), nitrogen/phosphorus/potassium (low|medium|high), "
              "fertility_score (0-100), observations (list of short strings), confidence (0-1)." % (region, previous))
    raw = await ask_llm(sys, prompt, image=(img, media_type))
    if raw:
        try:
            return {**json.loads(re.search(r"\{.*\}", raw, re.S).group(0)), "method": "ai_vision"}
        except Exception:
            pass
    return _heuristic_soil(img)

def _heuristic_soil(img):
    """Offline fallback: crude colour-based estimate. Clearly flagged as low confidence."""
    from PIL import Image
    im = Image.open(io.BytesIO(img)).convert("RGB").resize((64, 64))
    px = list(im.getdata()); n = len(px)
    r, g, b = (sum(p[i] for p in px) / n for i in range(3))
    bright = (r + g + b) / 3
    om = "high" if bright < 70 else "medium" if bright < 120 else "low"
    tex = "clay" if bright < 90 else "sandy" if bright > 150 else "loam"
    red = r > g * 1.25
    return {"texture": tex, "color": "reddish" if red else "dark" if bright < 90 else "light brown", "moisture": "moist" if bright < 100 else "dry",
            "organic_matter": om, "ph_estimate": 5.8 if red else 7.0, "salinity_risk": "high" if bright > 185 else "low",
            "nitrogen": "low" if om == "low" else "medium", "phosphorus": "medium", "potassium": "medium",
            "fertility_score": {"high": 75, "medium": 55, "low": 35}[om],
            "observations": ["Colour-based estimate only (AI vision unavailable)"], "confidence": 0.3, "method": "heuristic"}

# ---------- Voice ----------
async def tts(text: str, language: str) -> bytes:
    from gtts import gTTS
    def run():
        buf = io.BytesIO()
        gTTS(text=text[:1800], lang=language if language in LANGS else "en").write_to_fp(buf)
        return buf.getvalue()
    return await asyncio.to_thread(run)

_whisper = None
async def stt(audio: bytes, language: str | None):
    def run():
        global _whisper
        from faster_whisper import WhisperModel
        if _whisper is None:
            _whisper = WhisperModel("base", compute_type="int8")
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
            f.write(audio); path = f.name
        try:
            segs, info = _whisper.transcribe(path, language=language if language in LANGS else None)
            return " ".join(s.text for s in segs).strip(), info.language
        finally:
            os.unlink(path)
    return await asyncio.to_thread(run)
