import argparse, os, shutil, stat, time, uuid
from pathlib import Path
from .auth import create_invite
from .store import init_db, connect, audit
from .config import config

def place(tid,destination,rename=None,force=False):
    def allowed(path):
        return any(path==root or root in path.parents for root in config.allowed_roots)
    dest=Path(destination).expanduser().resolve()
    if not allowed(dest): raise SystemExit("destination is outside DSETTE_ALLOWED_ROOTS")
    with connect() as db: row=db.execute("SELECT * FROM transfers WHERE id=? AND status='verified'",(tid,)).fetchone()
    if not row: raise SystemExit("verified transfer not found")
    name=row["name"] if rename is None else rename
    if not name or name in (".","..") or any(c in name for c in ("/","\\",":")) or any(ord(c)<32 for c in name):
        raise SystemExit("rename must be a filename without paths or control characters")
    dest.mkdir(parents=True,exist_ok=True)
    dest=dest.resolve()
    if not allowed(dest): raise SystemExit("destination is outside DSETTE_ALLOWED_ROOTS")
    source=config.data_dir/"inbox"/tid; target=dest/name
    # Anchor publication to the opened directory on Linux. A destination
    # symlink is never followed, including when --force replaces a file.
    dirfd=None
    if os.name != "nt":
        dirfd=os.open(dest,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        if not allowed(Path(f"/proc/self/fd/{dirfd}").resolve()):
            os.close(dirfd); raise SystemExit("destination moved outside allowed roots")
    temp_name=".dsette-"+uuid.uuid4().hex
    temp_path=dest/temp_name
    def location(item): return item if dirfd is not None else dest/item
    kwargs={"dir_fd":dirfd} if dirfd is not None else {}
    try:
        try: existing=os.stat(location(name),follow_symlinks=False,**kwargs)
        except FileNotFoundError: existing=None
        if existing and stat.S_ISLNK(existing.st_mode): raise SystemExit("destination is a symlink")
        if existing and not force: raise SystemExit("destination exists; use --force")
        fd=os.open(location(temp_name),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600,**kwargs)
        source_fd=None
        try:
            source_fd=os.open(source,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
            with os.fdopen(source_fd,"rb") as src, os.fdopen(fd,"wb") as output:
                source_fd=None; fd=None
                shutil.copyfileobj(src,output,1024**2)
                output.flush()
                if os.name != "nt": os.fchmod(output.fileno(),0o644)
                os.fsync(output.fileno())
        finally:
            if source_fd is not None: os.close(source_fd)
            if fd is not None: os.close(fd)
        publish_kwargs={"src_dir_fd":dirfd,"dst_dir_fd":dirfd} if dirfd is not None else {}
        if force:
            os.replace(location(temp_name),location(name),**publish_kwargs)
        else:
            # Hard-link publication fails atomically on existing targets. The
            # copied temporary file is already on the destination filesystem.
            try: os.link(location(temp_name),location(name),follow_symlinks=False,**publish_kwargs)
            except FileExistsError: raise SystemExit("destination exists; use --force")
        source.unlink()
    finally:
        try: os.unlink(location(temp_name),**kwargs)
        except FileNotFoundError: pass
        if dirfd is not None: os.close(dirfd)
    with connect() as db: db.execute("UPDATE transfers SET status='placed' WHERE id=?",(tid,))
    audit("place",detail=f"{tid}:{target}"); print(target)

def main():
    p=argparse.ArgumentParser(prog="dsetd"); p.add_argument("--data-dir",type=Path)
    sub=p.add_subparsers(dest="cmd",required=True)
    s=sub.add_parser("serve"); s.add_argument("--allow-http-loopback",action="store_true",help="Only behind a TLS reverse proxy")
    sub.add_parser("invite"); sub.add_parser("list-devices"); sub.add_parser("inbox"); sub.add_parser("purge"); sub.add_parser("tui")
    u=sub.add_parser("update"); u.add_argument("--force",action="store_true")
    r=sub.add_parser("revoke"); r.add_argument("device_id")
    q=sub.add_parser("place"); q.add_argument("transfer_id"); q.add_argument("destination"); q.add_argument("--rename"); q.add_argument("--force",action="store_true")
    a=p.parse_args()
    if a.data_dir: config.data_dir=a.data_dir.expanduser().resolve()
    init_db()
    if a.cmd=="serve":
        import uvicorn
        cert=config.tls_cert; key=config.tls_key
        if not (cert and key) and not (a.allow_http_loopback and config.listen_host in {"127.0.0.1","::1"}):
            raise SystemExit("Set DSETTE_TLS_CERT and DSETTE_TLS_KEY. TLS is required.")
        uvicorn.run("dsette_server.app:app",host=config.listen_host,port=config.listen_port,
                    ssl_certfile=cert,ssl_keyfile=key,access_log=False,workers=1,
                    proxy_headers=True,forwarded_allow_ips="127.0.0.1,::1")
    elif a.cmd=="invite":
        print("One-time invite (expires in 10 minutes):",create_invite())
        cert=Path(config.tls_cert) if config.tls_cert else config.data_dir/"tls"/"server.crt"
        if cert.exists():
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            fingerprint=x509.load_pem_x509_certificate(cert.read_bytes()).fingerprint(hashes.SHA256()).hex(":")
            print("TLS SHA256:",fingerprint)
    elif a.cmd=="purge":
        from .files import purge
        print(purge())
    elif a.cmd=="tui":
        from .server_tui import run
        run()
    elif a.cmd=="update":
        from .updater import launch_update,UpdateError
        try: print("Update job:",launch_update(force=a.force))
        except UpdateError as exc: raise SystemExit(str(exc))
    elif a.cmd=="list-devices":
        with connect() as db:
            for x in db.execute("SELECT id,name,admin,created,revoked FROM devices"): print(dict(x))
    elif a.cmd=="inbox":
        with connect() as db:
            for x in db.execute("SELECT id,name,size,sha256,created FROM transfers WHERE status='verified'"): print(dict(x))
    elif a.cmd=="revoke":
        with connect() as db: db.execute("UPDATE devices SET revoked=? WHERE id=?",(time.time(),a.device_id)); db.execute("DELETE FROM sessions WHERE device_id=?",(a.device_id,))
        audit("revoke",a.device_id)
    elif a.cmd=="place": place(a.transfer_id,a.destination,a.rename,a.force)

if __name__=="__main__": main()

