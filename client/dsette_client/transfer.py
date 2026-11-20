"""Streaming transfer primitives with restart-safe inbox downloads/uploads."""
import hashlib
import hmac
import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

import httpx

from .keys import write_private

BLOCK_SIZE=1024*1024
_state_lock=threading.RLock()


def file_digest(path):
    hasher=hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda:source.read(BLOCK_SIZE),b""): hasher.update(block)
    return hasher.hexdigest()


def _progress(callback,done,total):
    if callback: callback(done,total)


def _state(api):
    path=api.config_dir/"uploads.json"
    try: return json.loads(path.read_text())
    except (FileNotFoundError,ValueError): return {}


def _save_state(api,key,tid):
    with _state_lock:
        state=_state(api)
        if tid is None: state.pop(key,None)
        else: state[key]=tid
        write_private(api.config_dir/"uploads.json",json.dumps(state).encode())


def send_file(api,path,progress=None):
    path=Path(path).expanduser().resolve(); size=path.stat().st_size; digest=file_digest(path)
    metadata={"name":path.name,"size":size,"sha256":digest,"chunk_size":BLOCK_SIZE}
    state_key=hashlib.sha256(f"{api.server_url}:{api.device_id}:{path}:{size}:{digest}".encode()).hexdigest()
    with _state_lock: tid=_state(api).get(state_key)
    status=None
    if tid:
        try: status=api.json("GET",f"/api/v1/files/{tid}/status")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code!=404: raise
    if not status or status.get("status") not in ("uploading","verified"):
        status=api.json("POST","/api/v1/files/announce",json=metadata)
        tid=status["transfer_id"]; _save_state(api,state_key,tid)
    if status.get("status")=="verified":
        _save_state(api,state_key,None); _progress(progress,size,size)
        return {"transfer_id":tid,"status":"verified","sha256":digest}
    offset=int(status.get("received",status.get("upload_offset",0)))
    if offset<0 or offset>size: raise ValueError("Server returned an invalid upload offset")
    _progress(progress,offset,size)
    with path.open("rb") as source:
        failures=0
        while offset<size:
            source.seek(offset); block=source.read(min(BLOCK_SIZE,size-offset))
            if not block: raise ValueError("File changed while being sent")
            try:
                result=api.json("PUT",f"/api/v1/files/{tid}/chunk",params={"offset":offset},content=block)
                received=int(result["received"])
                if received!=offset+len(block): raise ValueError("Server returned an invalid chunk offset")
                offset=received; failures=0
            except (httpx.TransportError,httpx.HTTPStatusError) as exc:
                if isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code not in (409,502,503,504): raise
                failures+=1
                if failures>3: raise
                time.sleep(.3*2**(failures-1))
                state=api.json("GET",f"/api/v1/files/{tid}/status")
                offset=int(state.get("received",state.get("upload_offset",0)))
                if offset<0 or offset>size: raise ValueError("Server returned an invalid upload offset")
            _progress(progress,offset,size)
    result=api.json("POST",f"/api/v1/files/{tid}/finish")
    if result.get("status")!="verified":
        _save_state(api,state_key,None); raise ValueError("Server rejected the file SHA-256; source may have changed")
    _save_state(api,state_key,None)
    return {**result,"transfer_id":tid,"sha256":digest}


def _destination(destination):
    raw=Path(destination).expanduser()
    if os.path.lexists(raw): raise FileExistsError("Destination already exists; choose another filename")
    dest=raw.parent.resolve()/raw.name
    if not dest.parent.is_dir(): raise FileNotFoundError("Destination directory does not exist")
    return dest


def _publish(partial,destination):
    # Creating a link gives atomic no-overwrite semantics on NTFS and POSIX.
    os.link(partial,destination)
    partial.unlink()
    return destination


def receive_file(api,tid,destination,progress=None):
    dest=_destination(destination)
    metadata=api.json("GET",f"/api/v1/files/{tid}/status")
    if metadata.get("status")!="verified": raise ValueError("This file is not verified and ready for download")
    size=int(metadata["size"]); expected=metadata["sha256"]
    suffix=hashlib.sha256(f"{api.server_url}:{tid}:{dest}".encode()).hexdigest()[:32]
    partial=dest.parent/(".dsette-"+suffix+".partial")
    flags=os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0)
    if partial.is_symlink(): raise ValueError("Partial download must not be a symlink")
    fd=os.open(partial,flags,0o600)
    with os.fdopen(fd,"r+b") as output:
        if not stat.S_ISREG(os.fstat(output.fileno()).st_mode): raise ValueError("Partial download is not a regular file")
        offset=output.seek(0,os.SEEK_END)
        if offset>size: output.truncate(0); offset=0
        _progress(progress,offset,size)
        if offset<size:
            headers={"Range":f"bytes={offset}-"} if offset else {}
            with api.stream("GET",f"/api/v1/files/{tid}/download",headers=headers,timeout=180) as response:
                if offset and response.status_code==200:
                    output.seek(0); output.truncate(0); offset=0
                elif offset:
                    content_range=response.headers.get("Content-Range","")
                    if not content_range.startswith(f"bytes {offset}-") or not content_range.endswith(f"/{size}"):
                        raise ValueError("Server returned an invalid download range")
                output.seek(offset)
                for block in response.iter_bytes(BLOCK_SIZE):
                    offset+=len(block)
                    if offset>size: raise ValueError("Server sent more than the announced file size")
                    output.write(block); _progress(progress,offset,size)
                output.flush(); os.fsync(output.fileno())
    if partial.stat().st_size!=size or not hmac.compare_digest(file_digest(partial),expected):
        partial.unlink(missing_ok=True)
        try: api.json("POST",f"/api/v1/files/{tid}/ack",json={"verified":False})
        except httpx.HTTPError: pass
        raise ValueError("File size or SHA-256 mismatch; the partial download was deleted")
    _publish(partial,dest)
    try: api.json("POST",f"/api/v1/files/{tid}/ack",json={"verified":True})
    except httpx.HTTPError: pass
    return dest


def send_live(api,path,progress=None,announced=None):
    path=Path(path).expanduser().resolve(); size=path.stat().st_size; digest=file_digest(path)
    info=api.json("POST","/api/v1/files/live/announce",json={"name":path.name,"size":size,"sha256":digest})
    tid=info["transfer_id"]
    if announced: announced(tid)
    def blocks():
        sent=0
        with path.open("rb") as source:
            for block in iter(lambda:source.read(256*1024),b""):
                sent+=len(block); yield block; _progress(progress,sent,size)
    response=api.client.put(f"/api/v1/files/live/{tid}/stream",content=blocks(),headers=api.headers(),timeout=180)
    response.raise_for_status(); result=response.json()
    if result.get("status")!="verified": raise ValueError("Live transfer failed server SHA-256 verification")
    return {**result,"transfer_id":tid,"sha256":digest}


def receive_live(api,tid,destination,progress=None):
    dest=_destination(destination); hasher=hashlib.sha256(); received=0; partial=None
    try:
        with api.stream("GET",f"/api/v1/files/live/{tid}/stream",timeout=180) as response:
            expected=response.headers["X-Dsette-SHA256"]; size=int(response.headers["X-Dsette-Size"])
            fd,name=tempfile.mkstemp(prefix=".dsette-",suffix=".partial",dir=dest.parent); partial=Path(name)
            with os.fdopen(fd,"wb") as output:
                for block in response.iter_bytes(256*1024):
                    received+=len(block)
                    if received>size: raise ValueError("Stream exceeded announced size")
                    output.write(block); hasher.update(block); _progress(progress,received,size)
                output.flush(); os.fsync(output.fileno())
        if received!=size or not hmac.compare_digest(hasher.hexdigest(),expected): raise ValueError("Live transfer SHA-256 mismatch")
        state=api.json("GET",f"/api/v1/files/live/{tid}/status")
        if not state.get("done") or state.get("verified") is not True or state.get("aborted"):
            raise ValueError("Live sender did not complete a verified transfer")
        return _publish(partial,dest)
    finally:
        if partial is not None: partial.unlink(missing_ok=True)
