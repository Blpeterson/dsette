import hashlib
import json
import os
import stat

import httpx
import pytest

from dsette_client.api import DsetteAPI,normalize_fingerprint,validate_url
from dsette_client.keys import load_key,load_config,save_config
from dsette_client import transfer
from dsette_client.selfupdate import update_checkout,update_required


def make_api(tmp_path,handler):
    load_key(tmp_path)
    def transport(request):
        if request.url.path.endswith("/auth/challenge"):
            return httpx.Response(200,json={"nonce":"test-nonce"})
        if request.url.path.endswith("/auth/verify"):
            return httpx.Response(200,json={"session_token":"session","expires_in":900})
        assert request.headers["Authorization"]=="Bearer session"
        return handler(request)
    client=httpx.Client(base_url="https://test.example",transport=httpx.MockTransport(transport))
    return DsetteAPI(config={"url":"https://test.example","device_id":"test"},config_dir=tmp_path,client=client)


def test_device_key_is_stable_and_can_be_encrypted(tmp_path):
    key=load_key(tmp_path,passphrase="local-passphrase")
    with pytest.raises(TypeError): load_key(tmp_path)
    assert load_key(tmp_path,passphrase="local-passphrase").sign(b"sample")==key.sign(b"sample")
    save_config({"device_id":"test"},tmp_path)
    assert load_config(tmp_path)["device_id"]=="test"
    if os.name!="nt":
        assert stat.S_IMODE((tmp_path/"device.key").stat().st_mode)==0o600
        assert stat.S_IMODE((tmp_path/"config.json").stat().st_mode)==0o600


def test_http_urls_and_malformed_fingerprints_rejected():
    for url in ("http://example.com","https://user:pass@example.com","https://example.com/api","https://example.com/?token=secret"):
        with pytest.raises(ValueError): validate_url(url)
    with pytest.raises(ValueError): normalize_fingerprint("incorrect")
    assert normalize_fingerprint(":".join(["AA"]*32))=="aa"*32


def test_session_reauth_after_401(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401 if len(calls)==1 else 200,json={"server_version":"0.1.0"})
    with make_api(tmp_path,handler) as api:
        assert api.version()["server_version"]=="0.1.0"
    assert len(calls)==2


def test_upload_restarts_from_server_offset(tmp_path,monkeypatch):
    monkeypatch.setattr(transfer,"BLOCK_SIZE",65536)
    monkeypatch.setattr(transfer.time,"sleep",lambda _:None)
    source=tmp_path/"source.bin"; data=b"abcdef"*40000; source.write_bytes(data)
    uploaded=bytearray(); state={"disconnected":True,"announces":0,"offsets":[]}
    def handler(request):
        path=request.url.path
        if path.endswith("/announce"):
            state["announces"]+=1
            return httpx.Response(200,json={"transfer_id":"transfer","upload_offset":0})
        if path.endswith("/status"):
            return httpx.Response(200,json={"status":"uploading","received":len(uploaded)})
        if path.endswith("/chunk"):
            offset=int(request.url.params["offset"]); state["offsets"].append(offset)
            if state["disconnected"] and offset>=65536: raise httpx.ReadError("network disconnected",request=request)
            assert offset==len(uploaded); uploaded.extend(request.content)
            return httpx.Response(200,json={"received":len(uploaded)})
        if path.endswith("/finish"):
            assert bytes(uploaded)==data
            return httpx.Response(200,json={"status":"verified"})
        raise AssertionError(path)
    with make_api(tmp_path/"identity",handler) as api:
        with pytest.raises(httpx.ReadError): api.send_file(source)
    assert len(uploaded)==65536
    state["disconnected"]=False; state["offsets"].clear()
    with make_api(tmp_path/"identity",handler) as api:
        assert api.send_file(source)["status"]=="verified"
    assert state["announces"]==1
    assert state["offsets"][0]==65536


class InterruptedDownload(httpx.SyncByteStream):
    def __iter__(self):
        yield b"abcd"
        raise httpx.ReadError("download disconnected")


def test_download_range_resumes_and_verifies(tmp_path,monkeypatch):
    monkeypatch.setattr(transfer,"BLOCK_SIZE",4)
    data=b"abcdefghij"; digest=hashlib.sha256(data).hexdigest(); state={"first":True,"range":None}
    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200,json={"status":"verified","size":len(data),"sha256":digest})
        if request.url.path.endswith("/ack"): return httpx.Response(200,json={"ok":True})
        if request.url.path.endswith("/download"):
            if state["first"]:
                state["first"]=False
                return httpx.Response(200,stream=InterruptedDownload())
            state["range"]=request.headers.get("range")
            return httpx.Response(206,content=data[4:],headers={"Content-Range":"bytes 4-9/10"})
        raise AssertionError(request.url.path)
    destination=tmp_path/"received.bin"
    with make_api(tmp_path/"identity",handler) as api:
        with pytest.raises(httpx.ReadError): api.receive_file("transfer",destination)
        assert not destination.exists()
        assert api.receive_file("transfer",destination)==destination
    assert state["range"]=="bytes=4-"
    assert destination.read_bytes()==data
    assert not list(tmp_path.glob("*.partial"))


def test_download_hash_mismatch_never_publishes(tmp_path):
    def handler(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200,json={"status":"verified","size":3,"sha256":"0"*64})
        if request.url.path.endswith("/ack"): return httpx.Response(200,json={"ok":True})
        return httpx.Response(200,content=b"bad")
    destination=tmp_path/"received.bin"
    with make_api(tmp_path/"identity",handler) as api:
        with pytest.raises(ValueError,match="SHA-256"): api.receive_file("transfer",destination)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.partial"))


def test_download_refuses_overwrite(tmp_path):
    destination=tmp_path/"exists"; destination.write_bytes(b"keep")
    with make_api(tmp_path/"identity",lambda request:pytest.fail("must not contact server")) as api:
        with pytest.raises(FileExistsError): api.receive_file("transfer",destination)
    assert destination.read_bytes()==b"keep"


def test_selfupdate_requires_trusted_signer_before_git(tmp_path):
    (tmp_path/".git").mkdir()
    with pytest.raises(ValueError,match="trusted_signers"): update_checkout(tmp_path)
    assert update_required("0.2.0","0.1.0")
    assert not update_required("0.1.0","0.2.0")
