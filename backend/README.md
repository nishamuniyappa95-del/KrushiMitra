# KrushiMithra backend (FastAPI)
## Run
    python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    cp .env.example .env        # add ANTHROPIC_API_KEY
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
Open http://localhost:8000/docs  (interactive API; click Authorize after login and paste the token).

## Flow (maps to your 11 features)
| Step | Endpoint |
|---|---|
| Login | POST /auth/send-otp, POST /auth/verify-otp (DEV_MODE returns OTP in response) |
| Language | PUT /me/language (en hi kn te ta ml mr bn gu pa or) |
| Home search (text/mic/camera) | POST /assistant/ask (multipart: text, audio, image) |
| Voice playback | POST /voice/speak -> mp3 |
| Weather | GET /weather?lat=&lon= |
| Soil check | POST /soil/analyze (multipart: lat, lon, previous_crops, photo) |
| Recommendation | POST /crops/recommend |
| Why not crop X? | POST /crops/why-not |
| Decide + plan + budget + price + alerts | POST /crops/decide, GET /plan/{id} |
| Climate alerts | GET /climate/alerts?plan_id= |
| Govt schemes | GET /schemes |
