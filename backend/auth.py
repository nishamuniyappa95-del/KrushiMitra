import hashlib, re, secrets, time
import httpx, jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from . import config
from .db import conn

bearer = HTTPBearer()
PHONE_RE = re.compile(r"^\+?\d{10,13}$")
h = lambda s: hashlib.sha256((s + config.JWT_SECRET).encode()).hexdigest()

async def send_otp(phone: str):
    if not PHONE_RE.match(phone):
        raise HTTPException(400, "Invalid phone number")
    code = f"{secrets.randbelow(10**6):06d}"
    with conn() as c:
        c.execute("REPLACE INTO otps VALUES(?,?,?,0)", (phone, h(code), time.time() + 300))
    if config.DEV_MODE:
        return {"message": "OTP generated (DEV_MODE)", "dev_otp": code}
    sid, tok, frm = config.TWILIO
    async with httpx.AsyncClient() as cl:
        r = await cl.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok),
                          data={"To": phone, "From": frm, "Body": f"KrushiMithra OTP: {code} (valid 5 min)"})
    if r.status_code >= 300:
        raise HTTPException(502, "SMS provider error")
    return {"message": "OTP sent"}

def verify_otp(phone: str, code: str):
    with conn() as c:
        r = c.execute("SELECT * FROM otps WHERE phone=?", (phone,)).fetchone()
        if not r or r["expires"] < time.time() or r["attempts"] >= 5:
            raise HTTPException(400, "OTP expired or too many attempts. Request a new one.")
        if not secrets.compare_digest(r["code_hash"], h(code)):
            c.execute("UPDATE otps SET attempts=attempts+1 WHERE phone=?", (phone,))
            raise HTTPException(400, "Wrong OTP")
        c.execute("DELETE FROM otps WHERE phone=?", (phone,))
        u = c.execute("SELECT id, language FROM users WHERE phone=?", (phone,)).fetchone()
        if not u:
            uid = secrets.token_hex(8)
            c.execute("INSERT INTO users(id,phone,created) VALUES(?,?,?)", (uid, phone, time.time()))
            lang = "en"
        else:
            uid, lang = u["id"], u["language"]
    token = jwt.encode({"sub": uid, "exp": time.time() + 30 * 86400}, config.JWT_SECRET, algorithm="HS256")
    return {"access_token": token, "user_id": uid, "language": lang,
            "next": "request_permissions: location, camera, microphone"}

def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    try:
        return jwt.decode(cred.credentials, config.JWT_SECRET, algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
