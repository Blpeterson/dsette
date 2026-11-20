"""Authenticated HTTPS API shared by the CLI and terminal UI."""
import base64
import hashlib
import hmac
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .keys import config_directory,load_config,load_key,public_key,save_config,write_private


def validate_url(url):
    parts=urlsplit(url)
    if parts.scheme!="https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Use an HTTPS server URL without credentials, query, or fragment")
    if parts.path not in ("","/"): raise ValueError("Server URL must not contain a path")
    return url.rstrip("/")


def normalize_fingerprint(value):
    result=value.lower().replace(":","").replace("sha256","").replace("=","").strip()
    if len(result)!=64 or any(c not in "0123456789abcdef" for c in result):
        raise ValueError("Expected a SHA-256 certificate fingerprint (64 hex digits)")
    return result


def pin_certificate(url,expected,config_dir=None):
    """Inspect only the certificate, then trust it only after explicit pin match."""
    url=validate_url(url); parts=urlsplit(url); expected=normalize_fingerprint(expected)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname=False; context.verify_mode=ssl.CERT_NONE
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    with socket.create_connection((parts.hostname,parts.port or 443),timeout=15) as connection:
        with context.wrap_socket(connection,server_hostname=parts.hostname) as secure:
            certificate=secure.getpeercert(binary_form=True)
    actual=hashlib.sha256(certificate).hexdigest()
    if not hmac.compare_digest(actual,expected): raise ValueError("Server certificate fingerprint does not match; enrollment stopped")
    path=config_directory(config_dir)/"server-cert.pem"
    write_private(path,ssl.DER_cert_to_PEM_cert(certificate).encode())
    return str(path),actual


def tls_context(config):
    ca=config.get("ca_cert")
    context=ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    return context


def bootstrap_update_settings(directory,checkout=None,trusted_signers=None):
    """Pin bundled public release signers outside the mutable Git checkout."""
    if checkout:
        repo=Path(checkout).expanduser().resolve()
        if not (repo/".git").exists(): raise ValueError("--checkout must point to a Git source checkout")
    else:
        current=Path.cwd().resolve(); module=Path(__file__).resolve()
        candidates=list(dict.fromkeys([current,*current.parents,*module.parents]))
        repo=next((candidate for candidate in candidates if (candidate/".git").exists() and
                   ((candidate/"deploy"/"release-signers").is_file() or trusted_signers)),None)
    signer_file=Path(trusted_signers).expanduser().resolve() if trusted_signers else (repo/"deploy"/"release-signers" if repo else None)
    if signer_file is None: return {}
    if not signer_file.is_file() or signer_file.stat().st_size>1024*1024:
        raise ValueError("Release allowed-signers file is missing or invalid; pass --trusted-signers")
    data=signer_file.read_bytes()
    if not data.strip(): raise ValueError("Release allowed-signers file is empty")
    pinned=Path(directory)/"release-signers"
    write_private(pinned,data)
    result={"trusted_signers":str(pinned)}
    if repo: result["client_checkout"]=str(repo)
    return result


def enroll(url,invite,device_name,config_dir=None,ca_cert=None,fingerprint=None,passphrase=None,checkout=None,trusted_signers=None):
    url=validate_url(url); directory=config_directory(config_dir)
    if (directory/"config.json").exists():
        raise ValueError("This device is already enrolled; use a different --config-dir for another identity")
    cfg={"url":url,"device_name":device_name}
    cfg.update(bootstrap_update_settings(directory,checkout,trusted_signers))
    if fingerprint:
        cfg["ca_cert"],cfg["fingerprint"]=pin_certificate(url,fingerprint,directory)
    elif ca_cert:
        destination=directory/"server-cert.pem"
        write_private(destination,Path(ca_cert).read_bytes()); cfg["ca_cert"]=str(destination)
    key=load_key(directory,passphrase=passphrase)
    with httpx.Client(base_url=url,verify=tls_context(cfg),timeout=30,trust_env=False) as client:
        response=client.post("/api/v1/enroll",json={"invite_token":invite,"public_key":public_key(key),"device_name":device_name})
        response.raise_for_status(); result=response.json()
    cfg.update(device_id=result["device_id"],admin=bool(result.get("admin",False)))
    save_config(cfg,directory)
    return cfg


class DsetteAPI:
    def __init__(self,config=None,config_dir=None,passphrase=None,client=None):
        self.config_dir=config_directory(config_dir)
        self.config=load_config(self.config_dir) if config is None else config
        self.server_url=validate_url(self.config["url"])
        self.device_id=self.config["device_id"]
        self.fingerprint=self.config.get("fingerprint","")
        self.key=load_key(self.config_dir,passphrase=passphrase,create=False)
        self.client=client or httpx.Client(base_url=self.server_url,verify=tls_context(self.config),timeout=35,trust_env=False)
        self._token=None; self._expires=0; self._auth_lock=threading.RLock()

    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self): self.client.close()

    def login(self,force=False):
        with self._auth_lock:
            if not force and self._token and time.monotonic()<self._expires: return self._token
            response=self.client.post("/api/v1/auth/challenge",json={"device_id":self.device_id})
            response.raise_for_status(); nonce=response.json()["nonce"]
            signature=base64.b64encode(self.key.sign(nonce.encode())).decode()
            response=self.client.post("/api/v1/auth/verify",json={"device_id":self.device_id,"nonce":nonce,"signature":signature})
            response.raise_for_status(); result=response.json()
            self._token=result["session_token"]; self._expires=time.monotonic()+max(1,int(result.get("expires_in",900))-15)
            self.login_info=result
            return self._token

    def headers(self): return {"Authorization":"Bearer "+self.login()}

    def request(self,method,path,**kwargs):
        retryable=method.upper() in ("GET","HEAD")
        last_error=None
        for attempt in range(3 if retryable else 1):
            try:
                response=self.client.request(method,path,headers=self.headers(),**kwargs)
                if response.status_code==401:
                    self.login(force=True)
                    response=self.client.request(method,path,headers=self.headers(),**kwargs)
                response.raise_for_status(); return response
            except httpx.TransportError as exc:
                last_error=exc
                if not retryable or attempt==2: raise
                time.sleep(.3*(2**attempt))
        raise last_error

    def json(self,method,path,**kwargs):
        response=self.request(method,path,**kwargs)
        return None if response.status_code==204 else response.json()

    @contextmanager
    def stream(self,method,path,**kwargs):
        custom_headers=kwargs.pop("headers",{})
        with self.client.stream(method,path,headers={**custom_headers,**self.headers()},**kwargs) as response:
            if response.status_code==401:
                response.read()
            else:
                response.raise_for_status(); yield response; return
        self.login(force=True)
        with self.client.stream(method,path,headers={**custom_headers,**self.headers()},**kwargs) as response:
            response.raise_for_status(); yield response

    def push_clipboard(self,text,ttl_s=600): return self.json("PUT","/api/v1/clipboard",json={"content_type":"text/plain","data":text,"ttl_s":ttl_s})
    def latest_clipboard(self):
        try: return self.json("GET","/api/v1/clipboard/latest")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code==404: return None
            raise
    def wait_clipboard(self,after=0,timeout=30): return self.json("GET","/api/v1/clipboard/wait",params={"after":after,"timeout":timeout},timeout=min(timeout,30)+10)
    def inbox(self): return self.json("GET","/api/v1/files/inbox")
    def ack_file(self,transfer_id,verified=True): return self.json("POST",f"/api/v1/files/{transfer_id}/ack",json={"verified":verified})
    def delete_file(self,transfer_id): return self.json("DELETE",f"/api/v1/files/{transfer_id}")
    def devices(self): return self.json("GET","/api/v1/devices")
    def version(self): return self.json("GET","/api/v1/version")
    def update(self,ref="main"): return self.json("POST","/api/v1/admin/update",json={"ref":ref})
    def update_status(self,job_id): return self.json("GET",f"/api/v1/admin/update/{job_id}")
    def live_list(self): return self.json("GET","/api/v1/files/live")
    def send_file(self,path,progress=None):
        from .transfer import send_file
        return send_file(self,path,progress)
    def receive_file(self,transfer_id,destination,progress=None):
        from .transfer import receive_file
        return receive_file(self,transfer_id,destination,progress)
    def send_live(self,path,progress=None,announced=None):
        from .transfer import send_live
        return send_live(self,path,progress,announced)
    def receive_live(self,tid,destination,progress=None):
        from .transfer import receive_live
        return receive_live(self,tid,destination,progress)
