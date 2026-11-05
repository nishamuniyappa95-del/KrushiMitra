"""Core decision engine: soil treatment, crop scoring, cultivation plan, price forecast, climate alerts."""
import math
from datetime import date, timedelta
from .knowledge import CROPS, ALIASES, SCHEMES, HAZARD_SCHEMES

# ---------- Soil treatment ----------
def soil_treatments(s):
    t = []
    ph = s.get("ph_estimate", 7)
    if ph < 5.5: t.append("Soil is acidic: apply agricultural lime 200-400 kg/acre and mix well 2-3 weeks before sowing.")
    if ph > 8.0: t.append("Soil is alkaline: apply gypsum 200-400 kg/acre and add organic matter; leach with good-quality water.")
    if s.get("salinity_risk") == "high": t.append("Salinity risk: provide drainage, flush with irrigation before sowing, grow tolerant crops.")
    if s.get("organic_matter") == "low": t.append("Low organic matter: add well-rotted FYM/compost 4-5 tonnes/acre or green manure (sunhemp/dhaincha).")
    if s.get("nitrogen") == "low": t.append("Low nitrogen: include a legume in rotation, use Azospirillum/Rhizobium biofertiliser, plan split urea doses.")
    if s.get("phosphorus") == "low": t.append("Low phosphorus: apply SSP/DAP as basal dose with PSB biofertiliser.")
    if s.get("potassium") == "low": t.append("Low potassium: apply Muriate of Potash (MOP) 20-25 kg/acre as basal dose.")
    if s.get("moisture") == "wet": t.append("Soil is waterlogged: create drainage channels / raised beds before sowing.")
    return t or ["No major treatment required. Maintain with FYM each season."]

def soil_status(s):
    sc = s.get("fertility_score", 50)
    return "Good" if sc >= 70 else "Moderate" if sc >= 45 else "Poor"

# ---------- Crop scoring ----------
def _norm_crop(name):
    n = (name or "").strip().lower()
    n = ALIASES.get(n, n)
    return n if n in CROPS else None

def _tex(t):
    t = (t or "loam").lower()
    return "clay" if "clay" in t else "sandy" if "sand" in t else "silty" if "silt" in t else "loam"

def _range_score(v, lo, hi, opt_lo=None, opt_hi=None):
    if v is None: return 60
    opt_lo = lo if opt_lo is None else opt_lo; opt_hi = hi if opt_hi is None else opt_hi
    if opt_lo <= v <= opt_hi: return 100
    if v < lo or v > hi: return 15
    d = (opt_lo - v) / (opt_lo - lo + 1e-9) if v < opt_lo else (v - opt_hi) / (hi - opt_hi + 1e-9)
    return round(100 - 60 * d)

def score_crop(key, weather, soil, previous, month):
    c = CROPS[key]; reasons, concerns = [], []
    temp = _range_score(weather["outlook"]["avg_temp_7d"], c["temp"][0], c["temp"][1], c["temp"][2], c["temp"][3])
    ar = weather["climate"]["annual_rain"]
    rain = _range_score(ar, c["rain"][0] * .6, c["rain"][1] * 1.5, c["rain"][0], c["rain"][1]) if ar else 60
    rain = max(rain, 40)  # irrigation can compensate
    ph = _range_score(soil.get("ph_estimate"), c["ph"][0] - .5, c["ph"][1] + .5, c["ph"][0], c["ph"][1])
    tex = 100 if _tex(soil.get("texture")) in c["soils"] else 45
    season = 100 if month in c["sow"] else 60 if ((month % 12) + 1 in c["sow"] or (month - 2) % 12 + 1 in c["sow"]) else 20
    fams = [CROPS[p]["family"] for p in previous if p]
    last = fams[0] if fams else None
    rot = 70 if not last else 25 if last == c["family"] else 100 if (last == "legume" and c["family"] != "legume") else 90 if c["family"] == "legume" else 75
    total = temp * .25 + season * .25 + rain * .15 + ph * .15 + tex * .10 + rot * .10
    if soil.get("fertility_score", 50) < 40 and c["n"] == "high": total -= 8
    for label, v, good, bad in [
        ("temp", temp, f"Current temperature (~{weather['outlook']['avg_temp_7d']}°C) suits {c['name']}", "Current temperature is not ideal"),
        ("season", season, "Now is the right sowing window", f"Best sowing months are {c['sow']} — not this time"),
        ("rain", rain, "Rainfall pattern of your area fits this crop", "Area rainfall is outside its comfort range; irrigation needed"),
        ("ph", ph, "Soil pH is in the ideal range", "Soil pH is not ideal; correct it first"),
        ("soil", tex, "Soil texture suits this crop", "Soil texture is not the best match"),
        ("rotation", rot, "Good rotation after your previous crop (breaks pests, restores soil)", "Same family as previous crop: pest/disease build-up risk")]:
        (reasons if v >= 80 else concerns if v < 50 else []).append(good if v >= 80 else bad)
    return {"crop": key, "name": c["name"], "score": round(total), "components": {"temperature": temp, "season": season, "rainfall": rain, "soil_ph": ph,
            "soil_texture": tex, "rotation": rot}, "reasons": reasons, "concerns": concerns, "economics": economics(key, 1)}

def recommend(weather, soil, previous_raw, month):
    prev = [_norm_crop(p) for p in previous_raw]
    ranked = sorted((score_crop(k, weather, soil, prev, month) for k in CROPS), key=lambda x: -x["score"])
    return ranked

# ---------- Economics & price ----------
def forecast_price(key, harvest: date):
    c = CROPS[key]; yrs = max((harvest - date.today()).days, 0) / 365
    exp = c["price"] * (1.05 ** yrs) * 0.97  # ~5%/yr drift, small harvest-time glut discount (heuristic)
    return {"expected_per_quintal": round(exp), "low": round(exp * (1 - c["vol"])), "high": round(exp * (1 + c["vol"])),
            "note": "Heuristic estimate from base mandi price & volatility. Check Agmarknet/local mandi before selling."}

def economics(key, acres, sow: date | None = None):
    c = CROPS[key]; sow = sow or date.today()
    harvest = sow + timedelta(days=c["days"]); p = forecast_price(key, harvest)
    cost = sum(c["cost"].values()) * acres; y = c["yield_q"] * acres
    rev = y * p["expected_per_quintal"]
    return {"cost": round(cost), "yield_quintals": round(y, 1), "revenue": round(rev), "profit": round(rev - cost),
            "profit_range": [round(y * p["low"] - cost), round(y * p["high"] - cost)], "price": p, "harvest_date": harvest.isoformat()}

# ---------- Cultivation plan ----------
def build_plan(key, acres, sow: date, region):
    c = CROPS[key]; e = economics(key, acres, sow); d = c["days"]
    cost = {k: round(v * acres) for k, v in c["cost"].items()}; cost["contingency_10pct"] = round(sum(cost.values()) * .1)
    stages = [("Land preparation", -10, "Plough 2-3 times, level, apply basal FYM"),
              ("Sowing / transplanting", 0, f"Seed rate ~{c['seed_kg']} kg/acre ({round(c['seed_kg']*acres,1)} kg total)"),
              ("Establishment & weeding", round(d * .15), "First weeding, gap filling"),
              ("Vegetative growth", round(d * .45), "Top-dressing, pest scouting weekly"),
              ("Flowering / fruit set", round(d * .70), "Critical stage: do not allow water stress"),
              ("Maturity & harvest", d, "Harvest at right moisture; dry & grade before sale")]
    return {"crop": c["name"], "acres": acres, "region": region, "sowing_date": sow.isoformat(), "duration_days": d,
            "harvest_date": e["harvest_date"], "seeds": {"varieties": c["varieties"], "seed_rate_kg_per_acre": c["seed_kg"],
            "treatment": "Treat seed with Trichoderma/biofertiliser before sowing"},
            "fertilizer_schedule": [{"when": w, "what": a, "note": f"Quantities are per acre; multiply by {acres} acres"} for w, a in c["fert"]],
            "pest_management": [{"pest": p, "action": a} for p, a in c["pests"]] + [{"pest": "General IPM", "action": "Neem oil 5 ml/L, sticky traps, spray only above economic threshold, follow label & wear PPE"}],
            "timeline": [{"stage": s, "day": o, "date": (sow + timedelta(days=o)).isoformat(), "task": t} for s, o, t in stages],
            "budget": {"breakdown_inr": cost, "total_inr": sum(cost.values())},
            "expected": {"yield_quintals": e["yield_quintals"], "price_forecast": e["price"], "revenue_inr": e["revenue"],
                         "profit_inr": e["revenue"] - sum(cost.values()), "profit_range_inr": e["profit_range"]}}

# ---------- Climate alerts ----------
ACTIONS = {
 "heatwave": (["Irrigate in early morning/evening", "Mulch with straw/dry leaves", "Spray 1% potassium nitrate or kaolin to reduce heat stress"],
              ["Shade nets for nursery/vegetables", "Avoid fertiliser top-dressing during peak heat"]),
 "frost": (["Light irrigation in the evening raises night temperature", "Burn smoke fires/straw upwind at night"], ["Cover nurseries with plastic/straw", "Spray 0.1% sulphur solution"]),
 "heavy_rain": (["Open drainage channels now", "Delay fertiliser/pesticide sprays"], ["Stake tall crops", "Harvest mature produce early and store dry", "Apply copper-based fungicide after rain to prevent disease"]),
 "drought": (["Switch to drip/alternate-furrow irrigation", "Mulch to conserve moisture", "Irrigate at critical stages only (flowering, grain fill)"], ["Spray 2% urea/DAP for stress recovery", "Remove weeds that compete for water"]),
 "wind": (["Stake and support plants", "Delay spraying"], ["Plant windbreaks (e.g., Glyricidia, Casuarina) for the future", "Re-earth-up cereals"]),
}
def climate_alerts(weather, crop_key=None):
    f = weather["forecast"]; found = []
    if max(x["tmax"] for x in f) >= 40: found.append(("heatwave", "Max temperature ≥ 40°C expected"))
    if min(x["tmin"] for x in f) <= 5: found.append(("frost", "Min temperature ≤ 5°C expected"))
    if max((x["rain_mm"] or 0) for x in f) >= 50 or weather["outlook"]["rain_7d_mm"] >= 120: found.append(("heavy_rain", "Heavy rainfall expected"))
    if weather["outlook"]["rain_7d_mm"] < 3 and weather["outlook"]["avg_temp_7d"] > 32: found.append(("drought", "Hot and dry week ahead"))
    if max(x["wind_kmh"] for x in f) >= 50: found.append(("wind", "Strong winds expected"))
    out = []
    for h, why in found:
        pre, prot = ACTIONS[h]
        out.append({"hazard": h, "reason": why, "severity": "high", "prevention_steps": pre, "protection_steps": prot,
                    "if_damaged": {"steps": ["Take dated photos/videos of the damage", "Report to insurer within 72 hours (helpline 14447)", "Inform village accountant/Agriculture office"],
                                   "schemes": [SCHEMES[i] for i in HAZARD_SCHEMES[h]]}})
    return {"status": "alerts" if out else "all_clear", "alerts": out, "crop": CROPS[crop_key]["name"] if crop_key else None}
