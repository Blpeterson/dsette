#!/usr/bin/env python3
"""Manual large-transfer acceptance against a disposable localhost HTTPS server.

Run from a checkout with its dependencies installed:
    python scripts/acceptance_large.py --size-mib 1024

Needs about three times the chosen file size in free disk space. It uses its own
port, database, keys, certificate and temporary directory; existing services are
not contacted. No invite token or private key is printed.
"""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"server"),str(ROOT/"client")]

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dsette_client.api import DsetteAPI,enroll,tls_context
from dsette_client import transfer
from dsette_server.config import config
from dsette_server.auth import create_invite
from dsette_server.store import init_db


class TemporaryServer:
    def __init__(self,directory):
        self.directory=directory; self.process=None; self.max_rss_kib=0
        self.monitor_stop=threading.Event()
        self.data=directory/"server-data"; config.data_dir=self.data; init_db()
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"localhost")])
        now=datetime.datetime.now(datetime.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
              .not_valid_after(now+datetime.timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),critical=False)
              .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
        self.cert=directory/"certificate.pem"; self.key=directory/"tls.key"
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.key.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        if os.name!="nt": self.key.chmod(0o600)
        self.fingerprint=cert.fingerprint(hashes.SHA256()).hex()
        with socket.socket() as probe: probe.bind(("127.0.0.1",0)); self.port=probe.getsockname()[1]
        self.url=f"https://127.0.0.1:{self.port}"
        self.env=os.environ.copy(); self.env["DSETTE_DATA_DIR"]=str(self.data)
        self.env["PYTHONPATH"]=os.pathsep.join([str(ROOT/"server"),str(ROOT/"client")])
        self.monitor=threading.Thread(target=self._monitor,daemon=True); self.monitor.start()

    def _monitor(self):
        while not self.monitor_stop.wait(.05):
            process=self.process
            if process is None: continue
            try:
                for line in Path(f"/proc/{process.pid}/status").read_text().splitlines():
                    if line.startswith(("VmRSS:","VmHWM:")):
                        self.max_rss_kib=max(self.max_rss_kib,int(line.split()[1]))
            except (OSError,ValueError): pass

    def start(self):
        self.process=subprocess.Popen([sys.executable,"-m","uvicorn","dsette_server.app:app","--host","127.0.0.1","--port",str(self.port),"--ssl-keyfile",str(self.key),"--ssl-certfile",str(self.cert),"--no-access-log"],cwd=ROOT,env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        with httpx.Client(verify=tls_context({"ca_cert":str(self.cert)}),trust_env=False,timeout=.5) as client:
            for _ in range(150):
                if self.process.poll() is not None: raise RuntimeError("Disposable server failed to start")
                try:
                    if client.get(self.url+"/api/v1/health").status_code==200: return
                except httpx.HTTPError: pass
                time.sleep(.05)
        raise RuntimeError("Disposable server startup timed out")

    def stop(self):
        if self.process is None: return
        process=self.process
        process.terminate()
        try: process.communicate(timeout=15)
        except subprocess.TimeoutExpired: process.kill(); process.communicate()
        self.process=None

    def close(self):
        self.stop(); self.monitor_stop.set(); self.monitor.join(timeout=2)


class CutAfterCommit(httpx.BaseTransport):
    def __init__(self,server,verify,threshold):
        self.inner=httpx.HTTPTransport(verify=verify,trust_env=False)
        self.server=server; self.threshold=threshold; self.cut=False
    def handle_request(self,request):
        if self.cut: raise httpx.ConnectError("Intentional acceptance-test disconnect",request=request)
        response=self.inner.handle_request(request)
        if request.method=="PUT" and request.url.path.endswith("/chunk") and int(request.url.params["offset"])+len(request.content)>=self.threshold:
            response.read(); self.cut=True; self.server.stop()
            raise httpx.ReadError("Intentional server shutdown after committed chunk",request=request)
        return response
    def close(self): self.inner.close()


class RecordRanges(httpx.BaseTransport):
    def __init__(self,verify):
        self.inner=httpx.HTTPTransport(verify=verify,trust_env=False); self.ranges=[]
    def handle_request(self,request):
        if request.url.path.endswith("/download"): self.ranges.append(request.headers.get("range"))
        return self.inner.handle_request(request)
    def close(self): self.inner.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib",type=int,default=1024)
    parser.add_argument("--chunk-mib",type=int,default=4)
    parser.add_argument("--restart-after-mib",type=int,default=32)
    parser.add_argument("--memory-limit-mib",type=int,default=256)
    parser.add_argument("--work-dir",help="Parent directory with at least 3x the file size free")
    args=parser.parse_args()
    if args.size_mib<4 or not 1<=args.chunk_mib<=16: parser.error("size must be >=4 MiB and chunks 1-16 MiB")
    size=args.size_mib*1024**2; chunk=args.chunk_mib*1024**2
    threshold=min(max(chunk,args.restart_after_mib*1024**2),size-chunk)
    if threshold<=0: parser.error("file must be larger than one chunk")
    started=time.monotonic(); transfer.BLOCK_SIZE=chunk
    parent=Path(args.work_dir).resolve() if args.work_dir else None
    if parent: parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dsette-accept-",dir=parent) as temporary:
        directory=Path(temporary)
        if shutil.disk_usage(directory).free<size*3+256*1024**2: raise SystemExit("Not enough free disk space for acceptance test")
        source=directory/"source.bin"; block=os.urandom(1024**2)
        with source.open("wb") as output:
            for _ in range(args.size_mib): output.write(block)
        expected=transfer.file_digest(source)
        server=TemporaryServer(directory)
        try:
            server.start()
            cfg_a=directory/"sender"; cfg_b=directory/"receiver"
            a=enroll(server.url,create_invite(),"acceptance-sender",cfg_a,fingerprint=server.fingerprint)
            b=enroll(server.url,create_invite(),"acceptance-receiver",cfg_b,fingerprint=server.fingerprint)
            cut=CutAfterCommit(server,tls_context(a),threshold)
            client=httpx.Client(base_url=server.url,transport=cut,timeout=20)
            print(f"Uploading {args.size_mib} MiB; forcing a server shutdown after {threshold//1024**2} MiB.",flush=True)
            with DsetteAPI(a,cfg_a,client=client) as interrupted:
                try: interrupted.send_file(source)
                except httpx.TransportError: pass
                else: raise AssertionError("The interrupted upload unexpectedly completed")
            assert cut.cut,"Server shutdown was not exercised"
            server.start(); progress=[]
            with DsetteAPI(a,cfg_a) as sender:
                result=sender.send_file(source,progress=lambda done,total:progress.append(done))
            assert result["status"]=="verified"
            assert progress[0]>=threshold and progress[0]<size,"Upload did not resume from its committed offset"
            tid=result["transfer_id"]; recorder=RecordRanges(tls_context(b))
            client=httpx.Client(base_url=server.url,transport=recorder,timeout=180)
            with DsetteAPI(b,cfg_b,client=client) as receiver:
                # A bounded Range request must return only the selected MiB.
                with receiver.stream("GET",f"/api/v1/files/{tid}/download",headers={"Range":"bytes=1048576-2097151"}) as response:
                    assert response.status_code==206
                    count=sum(len(part) for part in response.iter_bytes(1024**2))
                    assert count==1024**2
                destination=directory/"received.bin"
                suffix=hashlib.sha256(f"{receiver.server_url}:{tid}:{destination}".encode()).hexdigest()[:32]
                partial=directory/(".dsette-"+suffix+".partial")
                with partial.open("xb") as output: output.write(block)
                receiver.receive_file(tid,destination)
                assert recorder.ranges[-1]=="bytes=1048576-"
                assert transfer.file_digest(destination)==expected
            rss_mib=server.max_rss_kib/1024
            if sys.platform.startswith("linux"):
                assert 0<rss_mib<args.memory_limit_mib,f"Peak server RSS {rss_mib:.1f} MiB exceeded limit"
            report={"result":"PASS","file_mib":args.size_mib,"resume_offset_mib":progress[0]//1024**2,
                    "server_peak_rss_mib":round(rss_mib,1) if rss_mib else None,
                    "range_verified":True,"sha256_verified":True,"seconds":round(time.monotonic()-started,1)}
            print(json.dumps(report,indent=2))
        finally: server.close()


if __name__=="__main__": main()
