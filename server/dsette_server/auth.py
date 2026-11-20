import base64, hashlib, hmac, secrets, time, uuid
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, Header, HTTPException
from .store import connect, audit
from .config import config

def digest(v): return hashlib.sha256(v.encode()).hexdigest()
def token(n=32): return secrets.token_urlsafe(n)

def create_invite():
    raw = token()
    with connect() as db: db.execute("INSERT INTO invites VALUES(?,?,NULL)",(digest(raw),time.time()+config.invite_seconds))
    return raw

def enroll(invite, public_key, name):
    now=time.time(); ih=digest(invite)
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT * FROM invites WHERE token_hash=?",(ih,)).fetchone()
        if not row or row["used"] is not None or row["expires"] <= now: raise ValueError("invalid or expired invite")
        did=str(uuid.uuid4()); admin=db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
        db.execute("UPDATE invites SET used=? WHERE token_hash=?",(now,ih))
        db.execute("INSERT INTO devices VALUES(?,?,?,?,?,NULL)",(did,name[:100],public_key,int(admin),now))
    audit("enroll",did); return did, admin

def challenge(device_id):
    now=time.time(); raw=token()
    with connect() as db:
        row=db.execute("SELECT revoked FROM devices WHERE id=?",(device_id,)).fetchone()
        if not row or row["revoked"] is not None: raise ValueError("unknown device")
        db.execute("INSERT OR REPLACE INTO nonces VALUES(?,?,?)",(device_id,raw,now+60))
    return raw

def verify(device_id, nonce, signature):
    now=time.time(); failure=None; raw=None
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT n.nonce,n.expires,d.public_key,d.revoked FROM nonces n JOIN devices d ON d.id=n.device_id WHERE n.device_id=?",(device_id,)).fetchone()
        db.execute("DELETE FROM nonces WHERE device_id=?",(device_id,))
        try:
            if not row or row["expires"] <= now or row["revoked"] is not None or not hmac.compare_digest(row["nonce"],nonce): raise ValueError("invalid challenge")
            Ed25519PublicKey.from_public_bytes(base64.b64decode(row["public_key"],validate=True)).verify(base64.b64decode(signature,validate=True),nonce.encode())
        except Exception:
            # Commit the deletion even after a bad signature: every attempt burns
            # its nonce, rather than rolling it back along with the failed login.
            failure=ValueError("invalid challenge or signature")
        else:
            raw=token(); db.execute("INSERT INTO sessions VALUES(?,?,?)",(digest(raw),device_id,now+config.session_seconds))
    if failure: raise failure
    audit("login",device_id); return raw

def current_device(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "): raise HTTPException(401,"authentication required")
    th=digest(authorization[7:]); now=time.time()
    with connect() as db:
        row=db.execute("SELECT d.*,s.token_hash FROM sessions s JOIN devices d ON d.id=s.device_id WHERE s.token_hash=? AND s.expires>? AND d.revoked IS NULL",(th,now)).fetchone()
    if not row or not hmac.compare_digest(th,row["token_hash"]): raise HTTPException(401,"invalid or expired session")
    result=dict(row); result.pop("token_hash"); return result

def require_admin(device=Depends(current_device)):
    if not device["admin"]: raise HTTPException(403,"admin required")
    return device

