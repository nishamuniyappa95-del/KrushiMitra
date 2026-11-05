import os
from dotenv import load_dotenv
load_dotenv()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
DEV_MODE = os.getenv("DEV_MODE", "true").lower() == "true"
DB_PATH = os.getenv("DB_PATH", "krushimithra.db")
TWILIO = (os.getenv("TWILIO_SID", ""), os.getenv("TWILIO_TOKEN", ""), os.getenv("TWILIO_FROM", ""))
