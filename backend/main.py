from contextlib import asynccontextmanager
from datetime import date
from typing import Optional
import json
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from . import auth, db, engine, services as sv
from .knowledge import CROPS, SCHEMES

@asynccontextmanager
async def lifespan(_):
    db.init(); yield

app = FastAPI(title="KrushiMithra API", version="1.0", lifespan=lifespan,
              description="Climate-based crop recommendation & agriculture planning for farmers")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
User = Depends(auth.current_user)

@app.get("/")
def root(): return {"app": "KrushiMithra", "docs": "/docs", "languages": sv.LANGS}

# ---------------- 2. Login ----------------
class PhoneIn(BaseModel): phone: str
class VerifyIn(PhoneIn): otp: str = Field(min_length=6, max_length=6)
class LangIn(BaseModel): language: str

@app.post("/auth/send-otp")
async def send_otp(b: PhoneIn): return await auth.send_otp(b.phone)

@app.post("/auth/verify-otp")
def verify(b: VerifyIn): return auth.verify_otp(b.phone, b.otp)

@app.put("/me/language")
def set_language(b: LangIn, uid: str = User):
    if b.language not in sv.LANGS: raise HTTPException(400, f"Supported: {list(sv.LANGS)}")
    with db.conn() as c: c.execute("UPDATE users SET language=? WHERE id=?", (b.language, uid))
    return {"language": b.language}

# ---------------- 3 & 1. Home search (text + camera + mic) / AI assistant ----------------
@app.post("/assistant/ask")
async def ask(text: Optional[str] = Form(None), language: str = Form("en"), image: Optional[UploadFile] = File(None),
              audio: Optional[UploadFile] = File(None), uid: str = User):
    """Unified search bar: type, speak (audio) or snap a photo (crop disease / pest / plant)."""
    heard = None
    if audio:
        try: heard, detected = await sv.stt(await audio.read(), language); text = (text or "") + " " + heard
        except ImportError: raise HTTPException(501, "Install faster-whisper for voice input, or do speech-to-text on-device")
    if not text and not image: raise HTTPException(400, "Provide text, image or audio")
    img = (await image.read(), image.content_type or "image/jpeg") if image else None
    ctx = db.latest(uid, "analysis")
    system = (f"You are KrushiMithra, a trusted agriculture assistant for Indian farmers. Reply ONLY in {sv.lang_name(language)}, simple words, "
              "no markdown, under 150 words. If a photo shows a plant problem, name the likely cause, confidence, and safe treatment (organic first). "
              "Recommend consulting the local KVK for serious or uncertain cases. Never invent prices or schemes.")
    q = (text or "Identify what is in this photo and advise.") + (f"\nFarmer context: {json.dumps(ctx.get('summary'))}" if ctx else "")
    ans = await sv.ask_llm(system, q, image=img)
    if not ans: raise HTTPException(503, "AI assistant unavailable (set ANTHROPIC_API_KEY)")
    return {"heard": heard, "answer": ans, "language": language}

@app.post("/voice/speak")
async def speak(text: str = Form(...), language: str = Form("en"), uid: str = User):
    """Text -> MP3 so the farmer can listen to any explanation in his language."""
    return Response(await sv.tts(text, language), media_type="audio/mpeg")

# ---------------- 4. Weather ----------------
@app.get("/weather")
async def weather(lat: float, lon: float, uid: str = User):
    w, region = await sv.get_weather(lat, lon), await sv.reverse_geocode(lat, lon)
    return {"region": region, **w, "alerts": engine.climate_alerts(w)["alerts"]}

# ---------------- 5. Soil fertility ----------------
@app.post("/soil/analyze")
async def soil_analyze(lat: float = Form(...), lon: float = Form(...), previous_crops: str = Form("", description="comma separated, latest first"),
                       language: str = Form("en"), photo: UploadFile = File(...), uid: str = User):
    if not (photo.content_type or "").startswith("image/"): raise HTTPException(400, "Upload an image")
    img = await photo.read()
    if len(img) > 8_000_000: raise HTTPException(413, "Image too large (max 8 MB)")
    prev = [p.strip() for p in previous_crops.split(",") if p.strip()]
    w, region = await sv.get_weather(lat, lon), await sv.reverse_geocode(lat, lon)
    soil = await sv.analyze_soil_photo(img, photo.content_type, region, prev)
    treat, status = engine.soil_treatments(soil), engine.soil_status(soil)
    summary = {"region": region, "soil_status": status, "soil": soil, "previous_crops": prev}
    exp = await sv.explain(json.dumps({**summary, "treatments": treat}), "Explain the soil condition and what treatment is needed before sowing.", language)
    aid = db.save(uid, "analysis", {"lat": lat, "lon": lon, "weather": w, "soil": soil, "previous": prev, "region": region, "summary": summary})
    return {"analysis_id": aid, "region": region, "soil": soil, "status": status, "treatment_required": status != "Good" or len(treat) > 1 or "No major" not in treat[0],
            "treatments": treat, "explanation": exp,
            "disclaimer": "Photo-based estimate. Confirm with a Soil Health Card lab test (free at KVK)."}

# ---------------- 6. Recommendation ----------------
class RecIn(BaseModel): analysis_id: str; language: str = "en"; top_n: int = 3

@app.post("/crops/recommend")
async def recommend(b: RecIn, uid: str = User):
    a = db.load(b.analysis_id, uid, "analysis")
    if not a: raise HTTPException(404, "Analysis not found")
    ranked = engine.recommend(a["weather"], a["soil"], a["previous"], date.today().month)
    top = ranked[:b.top_n]
    facts = json.dumps([{k: t[k] for k in ("name", "score", "reasons", "concerns")} | {"profit_per_acre": t["economics"]["profit"]} for t in top])
    exp = await sv.explain(facts, "Recommend the best crop first, explain why in simple words, mention the main risk, and compare briefly with the other options.", b.language)
    return {"analysis_id": b.analysis_id, "recommendations": top, "explanation": exp, "listen": "POST /voice/speak with explanation.text"}

# ---------------- 7. Why not another crop? ----------------
class WhyNotIn(BaseModel): analysis_id: str; crop: str; language: str = "en"

@app.post("/crops/why-not")
async def why_not(b: WhyNotIn, uid: str = User):
    a = db.load(b.analysis_id, uid, "analysis")
    key = engine._norm_crop(b.crop)
    if not a: raise HTTPException(404, "Analysis not found")
    if not key: raise HTTPException(404, f"Crop not in database. Available: {list(CROPS)}")
    ranked = engine.recommend(a["weather"], a["soil"], a["previous"], date.today().month)
    me = next(r for r in ranked if r["crop"] == key); best = ranked[0]
    facts = json.dumps({"asked_crop": me["name"], "score": me["score"], "best_crop": best["name"], "best_score": best["score"],
                        "advantages": me["reasons"], "problems": me["concerns"], "profit_per_acre_asked": me["economics"]["profit"],
                        "profit_per_acre_best": best["economics"]["profit"], "profit_range_asked": me["economics"]["profit_range"]})
    exp = await sv.explain(facts, "The farmer asks why he cannot grow this crop. Be balanced: give its profit potential, its faults/risks, "
                           "and say honestly if it can still be grown with extra effort.", b.language)
    return {"crop": me, "compared_with_best": best["name"], "rank": [r["crop"] for r in ranked].index(key) + 1, "explanation": exp}

# ---------------- 8 & 9. Decision + cultivation plan ----------------
class DecideIn(BaseModel):
    analysis_id: str; crop: str; acres: float = Field(gt=0, le=1000); sowing_date: Optional[date] = None; language: str = "en"

@app.post("/crops/decide")
async def decide(b: DecideIn, uid: str = User):
    a = db.load(b.analysis_id, uid, "analysis"); key = engine._norm_crop(b.crop)
    if not a or not key: raise HTTPException(404, "Analysis or crop not found")
    plan = engine.build_plan(key, b.acres, b.sowing_date or date.today(), a["region"])
    plan["climate_alerts"] = engine.climate_alerts(a["weather"], key)
    plan["soil_treatment_before_sowing"] = engine.soil_treatments(a["soil"])
    plan["summary"] = await sv.explain(json.dumps({k: plan[k] for k in ("crop", "acres", "duration_days", "harvest_date", "budget", "expected")}),
                                        "Summarise the cultivation plan: cost, duration, expected yield, price and profit. Remind that price is an estimate.", b.language)
    pid = db.save(uid, "plan", {**plan, "lat": a["lat"], "lon": a["lon"], "crop_key": key})
    return {"plan_id": pid, **plan}

@app.get("/plan/{plan_id}")
def get_plan(plan_id: str, uid: str = User):
    p = db.load(plan_id, uid, "plan")
    if not p: raise HTTPException(404, "Plan not found")
    return p

# ---------------- 10. Sudden climate change ----------------
@app.get("/climate/alerts")
async def alerts(plan_id: Optional[str] = None, lat: Optional[float] = None, lon: Optional[float] = None, language: str = "en", uid: str = User):
    crop = None
    if plan_id:
        p = db.load(plan_id, uid, "plan")
        if not p: raise HTTPException(404, "Plan not found")
        lat, lon, crop = p["lat"], p["lon"], p["crop_key"]
    if lat is None or lon is None: raise HTTPException(400, "Provide plan_id or lat & lon")
    res = engine.climate_alerts(await sv.get_weather(lat, lon), crop)
    if res["alerts"]:
        res["explanation"] = await sv.explain(json.dumps(res["alerts"]), "Warn the farmer clearly and list prevention and protection steps in order of urgency.", language)
    return res

# ---------------- 11. Government schemes ----------------
@app.get("/schemes")
async def schemes(language: str = "en", uid: str = User):
    exp = await sv.explain(json.dumps(SCHEMES), "Explain in simple words what help a farmer can get if crops are damaged, and what to do first.", language)
    return {"schemes": SCHEMES, "explanation": exp, "tip": "Report crop loss within 72 hours: call 14447 (PMFBY helpline)."}

@app.get("/crops")
def crops(): return {k: {"name": v["name"], "duration_days": v["days"], "sowing_months": v["sow"]} for k, v in CROPS.items()}
