"""Private device API, independent of other services on the host."""
import asyncio
import os
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import __version__
from .auth import challenge, current_device, enroll, verify
from .config import config
from .files import purge, recover, router as files_router
from .live import router as live_router
from .store import audit, connect, init_db
from .updater_routes import router as updater_router


@asynccontextmanager
async def lifespan(app):
    init_db()
    for directory in ("staging", "inbox"):
        (config.data_dir / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    recover()
    # A previous process cannot still own live relays after service restart.
    for marker in config.data_dir.glob("active-files-*"):
        marker.unlink(missing_ok=True)
    async def cleanup():
        while True:
            await asyncio.to_thread(purge)
            await asyncio.sleep(60)
    task = asyncio.create_task(cleanup())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Datasette", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class BodyLimit:
    """Bound request JSON before it reaches Pydantic's parser."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        streaming = scope.get("path", "").endswith(("/chunk", "/stream"))
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"} or streaming:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if len(body) + len(message.get("body", b"")) > 2 * 1024**2:
                return await Response("request body too large", status_code=413)(scope, receive, send)
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        pending = True
        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


app.add_middleware(BodyLimit)
auth_failures = {}


@app.middleware("http")
async def auth_rate_limit(request: Request, call_next):
    if request.url.path not in {"/api/v1/enroll", "/api/v1/auth/challenge", "/api/v1/auth/verify"}:
        return await call_next(request)
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if ip not in auth_failures and len(auth_failures) >= 4096:
        auth_failures.pop(next(iter(auth_failures)))
    failures = auth_failures.setdefault(ip, deque(maxlen=5))
    while failures and failures[0] < now - 60:
        failures.popleft()
    if len(failures) >= 5:
        return Response(status_code=429, headers={"Retry-After": "60"})
    response = await call_next(request)
    if response.status_code >= 400:
        failures.append(time.monotonic())
    return response


app.include_router(live_router)
app.include_router(files_router)
app.include_router(updater_router)


class Enrollment(BaseModel):
    invite_token: str = Field(min_length=10, max_length=256)
    public_key: str = Field(min_length=40, max_length=128)
    device_name: str = Field(min_length=1, max_length=100)


class Auth(BaseModel):
    device_id: str = Field(max_length=100)
    nonce: str = Field(max_length=256)
    signature: str = Field(max_length=256)


class Device(BaseModel):
    device_id: str = Field(max_length=100)


class Clip(BaseModel):
    content_type: str = "text/plain"
    data: str = Field(max_length=1048576)
    ttl_s: int = Field(default=600, ge=1, le=86400)


@lru_cache(maxsize=1)
def git_commit():
    try:
        value = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path.cwd(), capture_output=True,
                               text=True, timeout=3, check=True).stdout.strip()
        if len(value) == 40:
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    return os.getenv("DSETTE_GIT_COMMIT", "unknown")


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "version": __version__, "git_commit": git_commit()}


@app.get("/api/v1/version")
def version(d=Depends(current_device)):
    from .updater import UpdateConfig, UpdateError
    cfg = UpdateConfig()
    try:
        cfg.validate()
        enabled = True
    except UpdateError:
        enabled = False
    return {"server_version": __version__, "git_commit": git_commit(), "min_client_version": "0.2.0",
            "admin": bool(d["admin"]), "update_configured": enabled}


@app.get("/api/v1/devices")
def devices(d=Depends(current_device)):
    with connect() as db:
        rows = db.execute("SELECT id,name,admin,created,revoked FROM devices ORDER BY created").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/v1/enroll")
def do_enroll(x: Enrollment):
    try:
        did, admin = enroll(x.invite_token, x.public_key, x.device_name)
        return {"device_id": did, "admin": admin}
    except Exception:
        audit("enroll_failed")
        raise HTTPException(400, "invalid enrollment")


@app.post("/api/v1/auth/challenge")
def do_challenge(x: Device):
    try:
        return {"nonce": challenge(x.device_id), "expires_at": time.time()+60, "expires_in": 60}
    except Exception:
        audit("challenge_failed")
        raise HTTPException(401, "unknown device")


@app.post("/api/v1/auth/verify")
def do_verify(x: Auth):
    try:
        session = verify(x.device_id, x.nonce, x.signature)
    except Exception:
        audit("login_failed", x.device_id)
        raise HTTPException(401, "verification failed")
    with connect() as db:
        device = db.execute("SELECT admin FROM devices WHERE id=?", (x.device_id,)).fetchone()
    result = {"session_token": session, "expires_in": config.session_seconds,
              "server_version": __version__, "admin": bool(device["admin"])}
    if device["admin"]:
        from .updater import check_update_on_admin_login
        result.update(check_update_on_admin_login())
    return result


@app.put("/api/v1/clipboard")
def put_clip(x: Clip, d=Depends(current_device)):
    if x.content_type != "text/plain" or len(x.data.encode()) > 1024**2:
        raise HTTPException(413, "text clipboard limit is 1 MB")
    now = time.time()
    with connect() as db:
        db.execute("DELETE FROM clipboard WHERE expires<=?", (now,))
        cur = db.execute("INSERT INTO clipboard(device_id,content_type,data,created,expires) VALUES(?,?,?,?,?)",
                         (d["id"], x.content_type, x.data, now, now+x.ttl_s))
        return {"clip_id": cur.lastrowid}


def clip_result(row):
    return {"clip_id": row["id"], "content_type": row["content_type"], "data": row["data"],
            "from_device": row["device_id"], "ts": row["created"]}


@app.get("/api/v1/clipboard/latest")
def latest(d=Depends(current_device)):
    with connect() as db:
        db.execute("DELETE FROM clipboard WHERE expires<=?", (time.time(),))
        row = db.execute("SELECT * FROM clipboard ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise HTTPException(404, "no clipboard item")
    return clip_result(row)


@app.get("/api/v1/clipboard/wait")
async def wait_clip(after: int = 0, timeout: int = 30, d=Depends(current_device)):
    deadline = time.monotonic()+min(max(timeout, 1), 30)
    while time.monotonic() < deadline:
        # Revalidate on each poll so revocation takes effect during a long wait.
        with connect() as db:
            active = db.execute("SELECT revoked FROM devices WHERE id=?", (d["id"],)).fetchone()
            if active is None or active["revoked"] is not None:
                raise HTTPException(401, "device revoked")
            db.execute("DELETE FROM clipboard WHERE expires<=?", (time.time(),))
            row = db.execute("SELECT * FROM clipboard WHERE id>? ORDER BY id LIMIT 1", (after,)).fetchone()
        if row:
            return clip_result(row)
        await asyncio.sleep(.25)
    return Response(status_code=204)

