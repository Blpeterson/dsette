"""Exercise two real clients against an isolated HTTPS Uvicorn process."""
import datetime
import hashlib
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dsette_client.api import DsetteAPI,enroll,pin_certificate,tls_context
from dsette_client import transfer
from dsette_server.auth import create_invite
from dsette_server.config import config as server_config
from dsette_server.store import init_db


@pytest.fixture
def https_service(tmp_path,monkeypatch):
    root=Path(__file__).resolve().parents[2]
    data=tmp_path/"server-data"
    monkeypatch.setattr(server_config,"data_dir",data); init_db()
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"localhost")])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False)
          .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    certificate=tmp_path/"cert.pem"; private=tmp_path/"tls.key"
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    with socket.socket() as probe: probe.bind(("127.0.0.1",0)); port=probe.getsockname()[1]
    env=os.environ.copy(); env["DSETTE_DATA_DIR"]=str(data)
    env["PYTHONPATH"]=os.pathsep.join([str(root/"server"),str(root/"client")])
    process=subprocess.Popen([sys.executable,"-m","uvicorn","dsette_server.app:app","--host","127.0.0.1","--port",str(port),"--ssl-keyfile",str(private),"--ssl-certfile",str(certificate),"--no-access-log"],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    url=f"https://127.0.0.1:{port}"
    try:
        with httpx.Client(verify=tls_context({"ca_cert":str(certificate)}),trust_env=False,timeout=.5) as client:
            for _ in range(100):
                if process.poll() is not None: pytest.fail("Temporary HTTPS server exited: "+process.stderr.read().decode(errors="replace"))
                try:
                    if client.get(url+"/api/v1/health").status_code==200: break
                except httpx.HTTPError: pass
                time.sleep(.05)
            else: pytest.fail("Temporary HTTPS server did not start")
        yield url,cert.fingerprint(hashes.SHA256()).hex(),env
    finally:
        process.terminate()
        try: process.communicate(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.communicate()


class InterruptedTransport(httpx.BaseTransport):
    def __init__(self,verify):
        self.inner=httpx.HTTPTransport(verify=verify,trust_env=False)
    def handle_request(self,request):
        if request.method=="PUT" and request.url.path.endswith("/chunk") and int(request.url.params["offset"])>=65536:
            raise httpx.ReadError("Simulated cable disconnect",request=request)
        return self.inner.handle_request(request)
    def close(self): self.inner.close()


def test_two_devices_real_https_clipboard_resumed_file_and_live(tmp_path,https_service,monkeypatch):
    url,fingerprint,env=https_service
    cfg_a=tmp_path/"sender"; cfg_b=tmp_path/"receiver"
    a=enroll(url,create_invite(),"sender",cfg_a,fingerprint=fingerprint)
    b=enroll(url,create_invite(),"receiver",cfg_b,fingerprint=fingerprint)
    assert a["admin"] is True and b["admin"] is False
    with pytest.raises(ValueError,match="fingerprint"):
        pin_certificate(url,"0"*64,tmp_path/"bad-pin")
    source=tmp_path/"source.bin"; payload=os.urandom(256000); source.write_bytes(payload)
    monkeypatch.setattr(transfer,"BLOCK_SIZE",65536)
    with DsetteAPI(a,cfg_a) as sender,DsetteAPI(b,cfg_b) as receiver:
        clip=sender.push_clipboard("hello over verified TLS")
        assert receiver.wait_clipboard(after=clip["clip_id"]-1,timeout=1)["data"]=="hello over verified TLS"
        client=httpx.Client(base_url=url,transport=InterruptedTransport(tls_context(a)),timeout=10)
        with DsetteAPI(a,cfg_a,client=client) as interrupted:
            with pytest.raises(httpx.ReadError): interrupted.send_file(source)
        assert (cfg_a/"uploads.json").is_file()
        progress=[]
        result=sender.send_file(source,progress=lambda done,total:progress.append((done,total)))
        assert progress[0][0]==65536
        assert result["status"]=="verified"
        destination=tmp_path/"received.bin"
        assert receiver.receive_file(result["transfer_id"],destination)==destination
        assert destination.read_bytes()==payload
        announced=threading.Event(); state={}
        def announce(tid): state["tid"]=tid; announced.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            sending=pool.submit(sender.send_live,source,None,announce)
            assert announced.wait(10)
            live_destination=tmp_path/"live.bin"
            assert receiver.receive_live(state["tid"],live_destination)==live_destination
            assert sending.result(timeout=10)["status"]=="verified"
        assert live_destination.read_bytes()==payload
        assert len(receiver.devices())==2
        assert receiver.version()["server_version"]
    # Exercise the command entry point using the enrolled identity and pinned TLS.
    result=subprocess.run([sys.executable,"-m","dsette_client.main","--config-dir",str(cfg_b),"version"],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert "server_version" in result.stdout
    if os.name=="nt":
        permissions=subprocess.run(["icacls",str(cfg_a/"device.key")],capture_output=True,text=True,check=True).stdout
        assert "(I)" not in permissions
