"""Bounded stored transfers. Live transfers are handled separately in live.py."""
import hashlib
import hmac
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import current_device
from .config import config
from .store import audit, connect

router = APIRouter(prefix="/api/v1/files")


class Announce(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    chunk_size: int = Field(default=1048576, ge=65536, le=4194304)


class Receipt(BaseModel):
    verified: bool


def filename(value):
    name = re.split(r"[/\\]", value)[-1]
    if name in ("", ".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(400, "invalid filename")
    # Strip paths, but reject portable special filenames and Windows ADS syntax.
    if ":" in name or name.endswith((".", " ")):
        raise HTTPException(400, "invalid filename")
    return name


def guard_maintenance():
    if (config.data_dir / "maintenance").exists():
        raise HTTPException(503, "server updating; try again shortly", headers={"Retry-After": "5"})


def hash_file(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024**2), b""):
            hasher.update(block)
    return hasher.hexdigest()


def metadata(row):
    return {"transfer_id": row["id"], "name": row["name"], "size": row["size"],
            "sha256": row["sha256"], "upload_offset": row["received"],
            "received": row["received"], "status": row["status"],
            "from_device": row["device_id"], "ts": row["created"]}


@router.post("/announce")
def announce(x: Announce, d=Depends(current_device)):
    guard_maintenance()
    if x.size > config.max_file_size:
        raise HTTPException(413, "file exceeds configured size limit")
    name = filename(x.name)
    sha = x.sha256.lower()
    for directory in ("staging", "inbox"):
        (config.data_dir / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM transfers WHERE device_id=? AND name=? AND size=? AND sha256=? AND status='uploading' ORDER BY created DESC LIMIT 1",
                              (d["id"], name, x.size, sha)).fetchone()
        if previous and (config.data_dir / "staging" / previous["id"]).exists():
            return metadata(previous)
        reserved = db.execute("SELECT COALESCE(SUM(size),0) FROM transfers WHERE device_id=? AND status IN ('uploading','verifying','verified')", (d["id"],)).fetchone()[0]
        if reserved + x.size > config.device_quota:
            raise HTTPException(413, "device storage quota exceeded")
        outstanding = db.execute("SELECT COALESCE(SUM(size-received),0) FROM transfers WHERE status='uploading'").fetchone()[0]
        if shutil.disk_usage(config.data_dir).free < outstanding + x.size + config.disk_reserve:
            raise HTTPException(507, "insufficient unreserved storage")
        tid = str(uuid.uuid4())
        path = config.data_dir / "staging" / tid
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            db.execute("INSERT INTO transfers(id,device_id,name,size,sha256,created) VALUES(?,?,?,?,?,?)", (tid,d["id"],name,x.size,sha,time.time()))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return {"transfer_id": tid, "upload_offset": 0, "status": "uploading", "chunk_size": min(x.chunk_size, config.max_chunk_size)}


@router.get("/inbox")
def inbox(d=Depends(current_device)):
    with connect() as db:
        rows = db.execute("SELECT * FROM transfers WHERE status='verified' ORDER BY created DESC").fetchall()
    return [metadata(row) for row in rows]


@router.get("/{tid}/status")
def status(tid: str, d=Depends(current_device)):
    with connect() as db:
        row = db.execute("SELECT * FROM transfers WHERE id=? AND (device_id=? OR status='verified')", (tid,d["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "transfer not found")
    return metadata(row)


@router.put("/{tid}/chunk")
async def chunk(tid: str, request: Request, offset: int, d=Depends(current_device)):
    if offset < 0:
        raise HTTPException(400, "offset must be positive")
    with connect() as db:
        row = db.execute("SELECT * FROM transfers WHERE id=? AND device_id=? AND status='uploading'", (tid,d["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "active upload not found")
    body = bytearray()
    async for part in request.stream():
        if len(body)+len(part) > config.max_chunk_size:
            raise HTTPException(413, "chunk exceeds configured limit")
        body.extend(part)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM transfers WHERE id=? AND device_id=? AND status='uploading'", (tid,d["id"])).fetchone()
        if not row or row["received"] != offset:
            raise HTTPException(409, "offset mismatch; query transfer status")
        if offset+len(body) > row["size"]:
            raise HTTPException(413, "chunk exceeds announced size")
        if shutil.disk_usage(config.data_dir).free < len(body)+config.disk_reserve:
            raise HTTPException(507, "insufficient storage")
        with (config.data_dir / "staging" / tid).open("r+b") as target:
            target.seek(offset)
            target.write(body)
            target.truncate()
            target.flush()
            os.fsync(target.fileno())
        received = offset+len(body)
        db.execute("UPDATE transfers SET received=? WHERE id=?", (received,tid))
    return {"received": received}


@router.post("/{tid}/finish")
def finish(tid: str, d=Depends(current_device)):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM transfers WHERE id=? AND device_id=?", (tid,d["id"])).fetchone()
        if not row:
            raise HTTPException(404, "transfer not found")
        if row["status"] == "verified":
            return {"status": "verified"}
        if row["status"] != "uploading" or row["received"] != row["size"]:
            raise HTTPException(409, "upload incomplete or verification in progress")
        db.execute("UPDATE transfers SET status='verifying' WHERE id=?", (tid,))
    staging = config.data_dir / "staging" / tid
    destination = config.data_dir / "inbox" / tid
    source = staging if staging.exists() else destination
    try:
        valid = source.stat().st_size == row["size"] and hmac.compare_digest(hash_file(source),row["sha256"])
        if valid:
            if source != destination:
                os.replace(staging,destination)
            result = "verified"
        else:
            source.unlink(missing_ok=True)
            result = "hash_mismatch"
    except OSError:
        with connect() as db:
            db.execute("UPDATE transfers SET status='uploading' WHERE id=? AND status='verifying'", (tid,))
        raise HTTPException(409, "staging file unavailable; retry or delete the transfer")
    with connect() as db:
        db.execute("UPDATE transfers SET status=? WHERE id=?", (result,tid))
    audit("upload_"+result,d["id"],tid)
    return {"status": result}


@router.get("/{tid}/download")
def download(tid: str, d=Depends(current_device)):
    with connect() as db:
        row = db.execute("SELECT * FROM transfers WHERE id=? AND status='verified'", (tid,)).fetchone()
    if row is None:
        raise HTTPException(404, "verified transfer not found")
    return FileResponse(config.data_dir / "inbox" / tid,filename=row["name"],media_type="application/octet-stream",
                        headers={"ETag": '"'+row["sha256"]+'"', "X-Dsette-SHA256":row["sha256"], "Cache-Control":"no-store"})


@router.post("/{tid}/ack")
def acknowledge(tid: str, x: Receipt, d=Depends(current_device)):
    with connect() as db:
        if not db.execute("SELECT 1 FROM transfers WHERE id=? AND status='verified'", (tid,)).fetchone():
            raise HTTPException(404, "verified transfer not found")
        db.execute("INSERT OR REPLACE INTO transfer_receipts VALUES(?,?,?,?)", (tid,d["id"],int(x.verified),time.time()))
    audit("download_verified" if x.verified else "download_failed",d["id"],tid)
    return {"acknowledged": True}


@router.delete("/{tid}")
def delete(tid: str, d=Depends(current_device)):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
        if not row:
            raise HTTPException(404, "transfer not found")
        if row["device_id"] != d["id"] and not d["admin"]:
            raise HTTPException(403, "only sender or administrator can delete")
        if row["status"] == "verifying":
            raise HTTPException(409, "verification in progress")
        for directory in ("staging", "inbox"):
            (config.data_dir / directory / tid).unlink(missing_ok=True)
        db.execute("DELETE FROM transfer_receipts WHERE transfer_id=?", (tid,))
        db.execute("DELETE FROM transfers WHERE id=?", (tid,))
    audit("transfer_deleted",d["id"],tid)
    return {"deleted": True}


def purge():
    """Remove expired secrets/clips and stale unfinished uploads, keep inbox files."""
    now = time.time()
    with connect() as db:
        db.execute("DELETE FROM clipboard WHERE expires<=?", (now,))
        db.execute("DELETE FROM invites WHERE expires<=?", (now,))
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        db.execute("DELETE FROM nonces WHERE expires<=?", (now,))
        rows = db.execute("SELECT id FROM transfers WHERE status='uploading' AND created<?", (now-config.upload_idle_seconds,)).fetchall()
        for row in rows:
            (config.data_dir / "staging" / row["id"]).unlink(missing_ok=True)
            db.execute("DELETE FROM transfers WHERE id=?", (row["id"],))
    return {"stale_uploads_removed": len(rows)}


def recover():
    # A killed finish operation may have moved its file before committing state.
    with connect() as db:
        db.execute("UPDATE transfers SET status='uploading' WHERE status='verifying'")
