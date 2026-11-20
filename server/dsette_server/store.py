import os, sqlite3, time
from contextlib import contextmanager
from .config import config

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,public_key TEXT NOT NULL,admin INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,revoked REAL);
CREATE TABLE IF NOT EXISTS invites(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL,used REAL);
CREATE TABLE IF NOT EXISTS nonces(device_id TEXT PRIMARY KEY,nonce TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,device_id TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS clipboard(id INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,content_type TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,device_id TEXT NOT NULL,name TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,received INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'uploading',created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_receipts(transfer_id TEXT NOT NULL,device_id TEXT NOT NULL,verified INTEGER NOT NULL,created REAL NOT NULL,PRIMARY KEY(transfer_id,device_id));
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL NOT NULL,event TEXT NOT NULL,device_id TEXT,detail TEXT);
"""

@contextmanager
def connect():
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt": config.data_dir.chmod(0o700)
    fd = os.open(config.db, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    if os.name != "nt": config.db.chmod(0o600)
    db = sqlite3.connect(config.db, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()

def init_db():
    with connect() as db: db.executescript(SCHEMA)

def audit(event, device_id=None, detail=None):
    with connect() as db: db.execute("INSERT INTO audit_log(ts,event,device_id,detail) VALUES(?,?,?,?)",(time.time(),event,device_id,detail))

