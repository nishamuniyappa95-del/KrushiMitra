import json, sqlite3, time, uuid
from .config import DB_PATH

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS otps(phone TEXT PRIMARY KEY, code_hash TEXT, expires REAL, attempts INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, phone TEXT UNIQUE, language TEXT DEFAULT 'en', created REAL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, user_id TEXT, kind TEXT, data TEXT, created REAL);
        """)

def save(user_id, kind, data):
    rid = uuid.uuid4().hex[:12]
    with conn() as c:
        c.execute("INSERT INTO records VALUES(?,?,?,?,?)", (rid, user_id, kind, json.dumps(data), time.time()))
    return rid

def load(rid, user_id, kind=None):
    with conn() as c:
        r = c.execute("SELECT * FROM records WHERE id=? AND user_id=?", (rid, user_id)).fetchone()
    if not r or (kind and r["kind"] != kind):
        return None
    return json.loads(r["data"])

def update(rid, data):
    with conn() as c:
        c.execute("UPDATE records SET data=? WHERE id=?", (json.dumps(data), rid))

def latest(user_id, kind):
    with conn() as c:
        r = c.execute("SELECT data FROM records WHERE user_id=? AND kind=? ORDER BY created DESC LIMIT 1", (user_id, kind)).fetchone()
    return json.loads(r["data"]) if r else None
