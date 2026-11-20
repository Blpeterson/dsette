"""One-worker live relay with bounded buffering and disconnect cleanup."""
import asyncio
import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import current_device
from .config import config
from .store import audit

router = APIRouter(prefix="/api/v1/files/live")
BLOCK_SIZE = 256 * 1024
QUEUE_BLOCKS = 8
IDLE_SECONDS = 120
MAX_RELAYS = 32
relays = {}


class LiveAnnounce(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


@dataclass
class Relay:
    sender: str
    name: str
    size: int
    sha256: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_BLOCKS))
    receiver_ready: asyncio.Event = field(default_factory=asyncio.Event)
    aborted: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    sending: bool = False
    connected: bool = False
    received: int = 0
    verified: bool | None = None


async def interruptible(operation, relay):
    """Bound waits and release a blocked producer when the receiver disconnects."""
    work = asyncio.ensure_future(operation)
    abort = asyncio.create_task(relay.aborted.wait())
    try:
        done, _ = await asyncio.wait((work, abort), timeout=IDLE_SECONDS,
                                     return_when=asyncio.FIRST_COMPLETED)
        if relay.aborted.is_set():
            raise HTTPException(409, "live peer disconnected")
        if work not in done:
            relay.aborted.set()
            raise HTTPException(408, "live peer timed out")
        return work.result()
    finally:
        work.cancel()
        abort.cancel()
        await asyncio.gather(work, abort, return_exceptions=True)


def forget_later(tid, delay=60):
    def forget():
        relays.pop(tid,None)
        (config.data_dir/f"active-files-{os.getpid()}-{tid}").unlink(missing_ok=True)
    asyncio.get_running_loop().call_later(delay,forget)


@router.post("/announce")
async def announce(x: LiveAnnounce, d=Depends(current_device)):
    if (config.data_dir/"maintenance").exists():
        raise HTTPException(503,"server updating; try again shortly")
    if x.size > config.max_file_size:
        raise HTTPException(413, "file exceeds size limit")
    if len(relays) >= MAX_RELAYS or sum(r.sender == d["id"] for r in relays.values()) >= 4:
        raise HTTPException(429, "too many live transfers")
    name = re.split(r"[/\\]", x.name)[-1]
    if name in ("", ".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(400, "invalid filename")
    tid = str(uuid.uuid4())
    relay = relays[tid] = Relay(d["id"], name, x.size, x.sha256.lower())
    config.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker=config.data_dir/f"active-files-{os.getpid()}-{tid}"
    marker.touch(mode=0o600)
    def expire_unstarted():
        if not relay.sending and not relay.connected:
            relay.aborted.set()
            relays.pop(tid, None)
            marker.unlink(missing_ok=True)
    asyncio.get_running_loop().call_later(IDLE_SECONDS, expire_unstarted)
    audit("live_announce", d["id"], tid)
    return {"transfer_id": tid, "mode": "live", "buffer_bytes": BLOCK_SIZE * QUEUE_BLOCKS}


@router.get("")
async def available(d=Depends(current_device)):
    return [{"transfer_id": tid, "name": r.name, "size": r.size, "sha256": r.sha256,
             "from_device": r.sender, "connected": r.connected, "received": r.received}
            for tid, r in relays.items() if not r.finished.is_set() and not r.aborted.is_set()]


@router.put("/{tid}/stream")
async def send(tid: str, request: Request, d=Depends(current_device)):
    r = relays.get(tid)
    if not r or r.sender != d["id"]:
        raise HTTPException(404, "live transfer not found")
    if r.sending or r.finished.is_set() or r.aborted.is_set():
        raise HTTPException(409, "live transfer already used")
    # All state changes happen on one event loop before yielding control.
    r.sending = True
    hasher = hashlib.sha256()
    try:
        # Do not consume even a small upload before its receiver joins.
        await interruptible(r.receiver_ready.wait(), r)
        stream = request.stream().__aiter__()
        while True:
            try:
                part = await interruptible(anext(stream), r)
            except StopAsyncIteration:
                break
            if r.received + len(part) > r.size:
                raise HTTPException(413, "stream exceeds announced size")
            for start in range(0, len(part), BLOCK_SIZE):
                block = part[start:start + BLOCK_SIZE]
                await interruptible(r.queue.put(block), r)
                hasher.update(block)
                r.received += len(block)
        r.verified = r.received == r.size and hmac.compare_digest(hasher.hexdigest(), r.sha256)
        return {"status": "verified" if r.verified else "hash_mismatch", "received": r.received}
    except BaseException:
        r.verified = False
        r.aborted.set()
        raise
    finally:
        r.finished.set()
        # Successful responses may still be draining to a slow receiver. That
        # response schedules cleanup once its generator closes.
        if r.aborted.is_set() or not r.connected:
            forget_later(tid)
        audit("live_complete", d["id"], f"{tid}:{r.verified}")


async def receive_blocks(r, tid=None):
    try:
        while True:
            if not r.queue.empty():
                yield r.queue.get_nowait()
                continue
            if r.finished.is_set() or r.aborted.is_set():
                break
            get = asyncio.create_task(r.queue.get())
            finish = asyncio.create_task(r.finished.wait())
            abort = asyncio.create_task(r.aborted.wait())
            try:
                done, _ = await asyncio.wait((get, finish, abort), timeout=IDLE_SECONDS,
                                             return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield get.result()
                elif not done:
                    r.aborted.set()
                    break
            finally:
                for task in (get, finish, abort):
                    task.cancel()
                await asyncio.gather(get, finish, abort, return_exceptions=True)
    finally:
        # This also runs when Starlette cancels the response after a disconnect.
        if not r.finished.is_set():
            r.aborted.set()
        if tid is not None:
            (config.data_dir/f"active-files-{os.getpid()}-{tid}").unlink(missing_ok=True)
            forget_later(tid)


@router.get("/{tid}/stream")
async def receive(tid: str, d=Depends(current_device)):
    r = relays.get(tid)
    if not r or r.connected or r.finished.is_set() or r.aborted.is_set():
        raise HTTPException(404, "live transfer unavailable")
    r.connected = True
    r.receiver_ready.set()
    forget_later(tid, 24 * 3600)
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(r.name, safe=""),
               "X-Dsette-SHA256": r.sha256, "X-Dsette-Size": str(r.size),
               "Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    return StreamingResponse(receive_blocks(r, tid), media_type="application/octet-stream", headers=headers)


@router.get("/{tid}/status")
async def status(tid: str, d=Depends(current_device)):
    r = relays.get(tid)
    if not r:
        raise HTTPException(404, "live transfer not found")
    return {"connected": r.connected, "done": r.finished.is_set(), "received": r.received,
            "verified": r.verified, "aborted": r.aborted.is_set()}
