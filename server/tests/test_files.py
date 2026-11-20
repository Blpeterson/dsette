import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64
from fastapi.testclient import TestClient

from dsette_server.app import app, auth_failures
from dsette_server.auth import create_invite, enroll, challenge, verify
from dsette_server.config import config
from dsette_server.store import connect,init_db


def device(name):
    key=Ed25519PrivateKey.generate()
    public=base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode()
    did,_=enroll(create_invite(),public,name)
    nonce=challenge(did)
    token=verify(did,nonce,base64.b64encode(key.sign(nonce.encode())).decode())
    return did,{"Authorization":"Bearer "+token}


@pytest.fixture
def station(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"data_dir",tmp_path)
    auth_failures.clear()
    with TestClient(app) as client:
        sender,send=device("sender")
        receiver,recv=device("receiver")
        yield client,send,recv
    auth_failures.clear()


def announce(c,h,payload=b"hello",name="file.txt"):
    out=c.post("/api/v1/files/announce",headers=h,json={"name":name,"size":len(payload),"sha256":hashlib.sha256(payload).hexdigest()})
    assert out.status_code==200,out.text
    return out.json()["transfer_id"]


def test_resume_verify_range_ack_delete(station):
    c,s,r=station; data=b"hello world"; tid=announce(c,s,data,"../../hello.txt")
    assert c.put(f"/api/v1/files/{tid}/chunk?offset=0",headers=s,content=data[:4]).json()["received"]==4
    assert announce(c,s,data,"hello.txt")==tid
    assert c.put(f"/api/v1/files/{tid}/chunk?offset=0",headers=s,content=b"x").status_code==409
    assert c.get(f"/api/v1/files/{tid}/status",headers=r).status_code==404
    assert c.put(f"/api/v1/files/{tid}/chunk?offset=4",headers=s,content=data[4:]).status_code==200
    for _ in range(2): assert c.post(f"/api/v1/files/{tid}/finish",headers=s).json()["status"]=="verified"
    assert c.get(f"/api/v1/files/{tid}/status",headers=r).json()["name"]=="hello.txt"
    partial=c.get(f"/api/v1/files/{tid}/download",headers={**r,"Range":"bytes=4-"})
    assert partial.status_code==206 and partial.content==data[4:]
    assert partial.headers["content-range"]=="bytes 4-10/11"
    assert c.get(f"/api/v1/files/{tid}/download",headers={**r,"Range":"bytes=20-"}).status_code==416
    assert c.post(f"/api/v1/files/{tid}/ack",headers=r,json={"verified":True}).status_code==200
    assert c.delete(f"/api/v1/files/{tid}",headers=r).status_code==403
    assert c.delete(f"/api/v1/files/{tid}",headers=s).status_code==200
    assert not list((config.data_dir/"inbox").iterdir())


def test_tamper_chunk_cap_quota_and_maintenance(station,monkeypatch):
    c,s,r=station; tid=announce(c,s,b"hello")
    assert c.put(f"/api/v1/files/{tid}/chunk?offset=0",headers=s,content=b"wrong").status_code==200
    assert c.post(f"/api/v1/files/{tid}/finish",headers=s).json()["status"]=="hash_mismatch"
    assert not (config.data_dir/"staging"/tid).exists()
    monkeypatch.setattr(config,"device_quota",1)
    assert c.post("/api/v1/files/announce",headers=s,json={"name":"big","size":2,"sha256":"0"*64}).status_code==413
    (config.data_dir/"maintenance").touch()
    assert c.post("/api/v1/files/announce",headers=s,json={"name":"big","size":0,"sha256":"0"*64}).status_code==503


def test_concurrent_offset_claim_only_one_writes(station):
    c,s,r=station; tid=announce(c,s,b"ok")
    with ThreadPoolExecutor(2) as pool:
        codes=list(pool.map(lambda _:c.put(f"/api/v1/files/{tid}/chunk?offset=0",headers=s,content=b"ok").status_code,range(2)))
    assert sorted(codes)==[200,409]
    assert (config.data_dir/"staging"/tid).read_bytes()==b"ok"


def test_clipboard_wait_expiry_and_json_bound(station):
    c,s,r=station
    with ThreadPoolExecutor(1) as pool:
        waiting=pool.submit(c.get,"/api/v1/clipboard/wait?timeout=3",headers=r)
        start=time.monotonic(); c.put("/api/v1/clipboard",headers=s,json={"data":"private"})
        assert waiting.result().json()["data"]=="private"
        assert time.monotonic()-start<1
    with connect() as db: db.execute("UPDATE clipboard SET expires=0")
    assert c.get("/api/v1/clipboard/latest",headers=r).status_code==404
    with connect() as db: assert db.execute("SELECT COUNT(*) FROM clipboard").fetchone()[0]==0
    assert c.put("/api/v1/clipboard",headers=s,content=b"x"*(2*1024**2+1)).status_code==413


def test_auth_admin_and_public_surface(station):
    c,s,r=station
    assert c.get("/api/v1/devices",headers=r).status_code==200
    assert c.get("/api/v1/version",headers=r).json()["admin"] is False
    assert c.post("/api/v1/admin/update",headers=r,json={}).status_code==403
    assert c.get("/openapi.json").status_code==404
    assert c.get("/api/v1/files/inbox").status_code==401
    for _ in range(5): c.post("/api/v1/auth/challenge",json={"device_id":"missing"})
    assert c.post("/api/v1/auth/challenge",json={"device_id":"missing"}).status_code==429
