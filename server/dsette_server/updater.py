"""Fail-closed signed, fast-forward release updater.

The updater is intentionally usable from a separate supervisor process.  The
web service may request an update, but the supervisor owns the symlink swap,
restart, health check, and rollback so it survives replacement of the service.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import config

SAFE_GIT_NAME = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SECRET = re.compile(r"(?i)(token|password|authorization|private[_-]?key)(\s*[=:]\s*)(\S+)")
URL_SECRET = re.compile(r"(?i)(https?://)([^/@\s]+)@")
BEARER_SECRET = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")


class UpdateError(RuntimeError):
    pass


def _paths(value: str) -> tuple[str, ...]:
    return tuple(x.strip().replace(" ", "") for x in value.split(",") if x.strip())


@dataclass(frozen=True)
class UpdateConfig:
    repo: Path = field(default_factory=lambda: Path(os.getenv("DSETTE_UPDATE_REPO", "/opt/dsette")))
    data_dir: Path = field(default_factory=lambda: config.data_dir)
    remote: str = field(default_factory=lambda: os.getenv("DSETTE_UPDATE_REMOTE", "origin"))
    branch: str = field(default_factory=lambda: os.getenv("DSETTE_UPDATE_BRANCH", "main"))
    remote_url: str | None = field(default_factory=lambda: os.getenv("DSETTE_UPDATE_REMOTE_URL"))
    trusted_signers: tuple[str, ...] = field(default_factory=lambda: _paths(os.getenv("DSETTE_TRUSTED_SIGNERS", "")))
    allowed_signers_file: Path | None = field(default_factory=lambda: Path(os.environ["DSETTE_SSH_ALLOWED_SIGNERS"]) if os.getenv("DSETTE_SSH_ALLOWED_SIGNERS") else None)
    signature_format: str = field(default_factory=lambda: os.getenv("DSETTE_SIGNATURE_FORMAT", "gpg"))
    wait_seconds: int = field(default_factory=lambda: int(os.getenv("DSETTE_UPDATE_WAIT_SECONDS", "300")))
    health_url: str = field(default_factory=lambda: os.getenv("DSETTE_UPDATE_HEALTH_URL", "https://127.0.0.1:8223/api/v1/health"))
    ca_file: Path | None = field(default_factory=lambda: Path(os.environ["DSETTE_UPDATE_CA_FILE"]) if os.getenv("DSETTE_UPDATE_CA_FILE") else None)
    control_dir: Path | None = field(default_factory=lambda: Path(os.environ["DSETTE_UPDATE_CONTROL_DIR"]) if os.getenv("DSETTE_UPDATE_CONTROL_DIR") else None)
    control_timeout: float = field(default_factory=lambda: float(os.getenv("DSETTE_UPDATE_CONTROL_TIMEOUT", "60")))
    restart_command: tuple[str, ...] = field(default_factory=lambda: (("/bin/systemctl", "restart", "dsette") if os.getenv("DSETTE_UPDATE_ROOT_SUPERVISOR") == "1" else ("sudo", "/bin/systemctl", "restart", "dsette")))
    stop_command: tuple[str, ...] = field(default_factory=lambda: (("/bin/systemctl", "stop", "dsette") if os.getenv("DSETTE_UPDATE_ROOT_SUPERVISOR") == "1" else ("sudo", "/bin/systemctl", "stop", "dsette")))
    start_command: tuple[str, ...] = field(default_factory=lambda: (("/bin/systemctl", "start", "dsette") if os.getenv("DSETTE_UPDATE_ROOT_SUPERVISOR") == "1" else ("sudo", "/bin/systemctl", "start", "dsette")))

    @property
    def releases(self) -> Path: return self.data_dir / "releases"
    @property
    def jobs(self) -> Path: return self.data_dir / "update-jobs"
    @property
    def backups(self) -> Path: return self.data_dir / "backups"
    @property
    def current(self) -> Path: return self.data_dir / "current"
    @property
    def maintenance(self) -> Path: return self.data_dir / "maintenance"
    @property
    def lock(self) -> Path: return self.data_dir / "update.lock"
    @property
    def trigger(self) -> Path: return self.data_dir / "update-trigger"

    def validate(self) -> None:
        if not SAFE_GIT_NAME.fullmatch(self.remote) or self.remote.startswith("-"):
            raise UpdateError("unsafe configured git remote")
        if not SAFE_GIT_NAME.fullmatch(self.branch) or self.branch.startswith(("-", "/")) or ".." in self.branch:
            raise UpdateError("unsafe configured git branch")
        if self.signature_format not in {"gpg", "ssh"}: raise UpdateError("signature format must be gpg or ssh")
        if not self.trusted_signers: raise UpdateError("DSETTE_TRUSTED_SIGNERS is required; signature verification is fail-closed")
        if self.signature_format == "ssh" and (not self.allowed_signers_file or not self.allowed_signers_file.is_file()):
            raise UpdateError("DSETTE_SSH_ALLOWED_SIGNERS must name an existing allowed-signers file")


def redact(text: str) -> str:
    text=BEARER_SECRET.sub(lambda m:m.group(1)+"[REDACTED]",text)
    text=SECRET.sub(lambda m: m.group(1)+m.group(2)+"[REDACTED]",text)
    text=URL_SECRET.sub(lambda m:m.group(1)+"[REDACTED]@",text)
    return text


def maintenance_active(cfg: UpdateConfig | None = None) -> bool:
    return (cfg or UpdateConfig()).maintenance.exists()


def _job_path(cfg: UpdateConfig, job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id): raise UpdateError("invalid job id")
    return cfg.jobs / f"{job_id}.json"

def _jobs_fd(cfg: UpdateConfig) -> int | None:
    cfg.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name=="nt":
        if cfg.jobs.is_symlink(): raise UpdateError("unsafe update jobs directory")
        return None
    return os.open(cfg.jobs, os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0))


def read_job(job_id: str, cfg: UpdateConfig | None = None) -> dict:
    cfg=cfg or UpdateConfig(); _job_path(cfg,job_id); directory=_jobs_fd(cfg)
    if directory is None:
        path=_job_path(cfg,job_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024: raise UpdateError("update job not found")
        value=json.loads(path.read_text(encoding="utf-8"))
        if value.get("job_id")!=job_id or value.get("state") not in {"queued","running","complete","failed"}: raise UpdateError("invalid update job")
        return value
    try:
        try: fd=os.open(f"{job_id}.json",os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=directory)
        except FileNotFoundError: raise UpdateError("update job not found")
        with os.fdopen(fd,"rb") as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>1024*1024: raise UpdateError("unsafe update job file")
            value=json.loads(handle.read().decode("utf-8"))
        if value.get("job_id")!=job_id or value.get("state") not in {"queued","running","complete","failed"}: raise UpdateError("invalid update job")
        return value
    finally: os.close(directory)


def create_job(*, force: bool = False, cfg: UpdateConfig | None = None) -> str:
    cfg = cfg or UpdateConfig(); cfg.validate(); cfg.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    job_id = uuid.uuid4().hex
    _write_job(cfg, job_id, {"job_id": job_id, "state": "queued", "force": bool(force), "created": time.time(), "log": []})
    return job_id


def launch_update(*, force: bool = False, cfg: UpdateConfig | None = None) -> str:
    """Queue a job for the separate systemd supervisor.

    A child process is deliberately not used: systemd kills every process in a
    service cgroup during restart, even one created with start_new_session.
    """
    cfg = cfg or UpdateConfig(); job_id = create_job(force=force, cfg=cfg)
    cfg.trigger.touch(mode=0o600, exist_ok=True)
    return job_id


def _write_job(cfg: UpdateConfig, job_id: str, value: dict) -> None:
    _job_path(cfg,job_id); directory=_jobs_fd(cfg); name=f"{job_id}.json"; temp=f".{job_id}.{uuid.uuid4().hex}.tmp"
    if directory is None:
        path=cfg.jobs/name; temporary=cfg.jobs/temp
        if path.exists() and (path.is_symlink() or not path.is_file()): raise UpdateError("unsafe update job target")
        with temporary.open("xb") as handle: handle.write(json.dumps(value,sort_keys=True).encode("utf-8")); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary,0o600); os.replace(temporary,path); return
    try:
        owner=None
        try:
            info=os.stat(name,dir_fd=directory,follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode): raise UpdateError("unsafe update job target")
            owner=info
        except FileNotFoundError: pass
        fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=directory)
        try:
            os.write(fd,json.dumps(value,sort_keys=True).encode("utf-8")); os.fsync(fd)
            if owner and hasattr(os,"fchown"): os.fchown(fd,owner.st_uid,owner.st_gid)
        finally: os.close(fd)
        os.replace(temp,name,src_dir_fd=directory,dst_dir_fd=directory)
    finally:
        try: os.unlink(temp,dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


class FileLock:
    def __init__(self, path: Path): self.path, self.fd = path, None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.fd).st_size == 0: os.write(self.fd, b"0")
                os.lseek(self.fd, 0, os.SEEK_SET); msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl; fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            os.close(self.fd); self.fd = None
            raise UpdateError("another update is already running")
        os.ftruncate(self.fd, 0); os.write(self.fd, str(os.getpid()).encode()); return self
    def __exit__(self, *_):
        if self.fd is not None: os.close(self.fd)
        # Keep the inode: unlinking a locked file permits another process to
        # create and lock a different inode before this descriptor is closed.


class Updater:
    def __init__(self, cfg: UpdateConfig | None = None, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.cfg, self._run = cfg or UpdateConfig(), run
        self.job: dict = {}

    def log(self, message: str) -> None:
        line = redact(message)[:4000]
        self.job.setdefault("log", []).append(line)
        self.job["log"] = self.job["log"][-200:]
        if self.job.get("job_id"):
            _write_job(self.cfg, self.job["job_id"], self.job)
        # Audit is best effort: update logging must not import the process-global
        # database config or turn a transient SQLite lock into a failed rollback.
        database=self.cfg.data_dir/"dsette.db"
        if database.is_file():
            try:
                with sqlite3.connect(database,timeout=.25) as db:
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
                    if exists: db.execute("INSERT INTO audit_log(ts,event,detail) VALUES(?,?,?)",(time.time(),"update_step",line))
            except sqlite3.Error: pass

    def cmd(self, args: Sequence[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
        self.log("$ " + " ".join(args))
        result = self._run(list(args), cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        output = redact(result.stdout or ""); self.log(output[-4000:]) if output else None
        if result.returncode: raise UpdateError(f"command failed ({result.returncode}): {args[0]}")
        return (result.stdout or "").strip()

    def _active_uploads(self) -> int:
        count = 0
        for marker in self.cfg.data_dir.glob("active-files-*"):
            match = re.match(r"active-files-(\d+)-", marker.name)
            if match and os.name != "nt":
                try: os.kill(int(match.group(1)), 0)
                except ProcessLookupError:
                    marker.unlink(missing_ok=True); continue
                except PermissionError: pass
            count += 1
        if not self.cfg.data_dir.joinpath("dsette.db").exists(): return count
        with sqlite3.connect(self.cfg.data_dir / "dsette.db") as db:
            return count + int(db.execute("SELECT COUNT(*) FROM transfers WHERE status='uploading'").fetchone()[0])

    def _wait_for_uploads(self, force: bool) -> None:
        deadline = time.monotonic() + self.cfg.wait_seconds
        while self._active_uploads():
            if force: self.log("force enabled: proceeding with active upload records"); return
            if time.monotonic() >= deadline: raise UpdateError("active uploads did not finish before timeout")
            time.sleep(min(1, self.cfg.wait_seconds or 1))

    def _git(self, *args: str) -> str: return self.cmd(("git", "-C", str(self.cfg.repo), *args))

    def _resolve_target(self) -> tuple[str, str]:
        self._git("fetch", "--no-tags", "--", self.cfg.remote, self.cfg.branch)
        target = self._git("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        # Compare against the actually deployed worktree, not merely the source
        # checkout's HEAD. Detached release worktrees do not advance source HEAD;
        # using it here could otherwise permit a signed downgrade on update #2.
        deployed = self.cfg.current.resolve() if self.cfg.current.is_symlink() else self.cfg.repo
        current = self.cmd(("git", "-C", str(deployed), "rev-parse", "--verify", "HEAD^{commit}"))
        result = self._run(["git", "-C", str(self.cfg.repo), "merge-base", "--is-ancestor", current, target], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, check=False)
        if result.returncode: raise UpdateError("target is not a fast-forward of the deployed source revision")
        return current, target

    def _verify_signature(self, commit: str) -> None:
        prefix = ["git", "-C", str(self.cfg.repo)]
        if self.cfg.signature_format == "ssh":
            prefix += ["-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={self.cfg.allowed_signers_file}"]
        args = prefix + ["verify-commit", "--raw", commit]
        self.cmd(args, timeout=60)
        # The same SSH allowed-signers configuration is required for both
        # verify-commit and the %G?/%GF status query; otherwise Git reports N.
        identity = self.cmd(prefix + ["show", "-s", "--format=%G?;%GF", commit], timeout=60).splitlines()[-1]
        try: status, fingerprint = identity.split(";", 1)
        except ValueError: raise UpdateError("could not determine commit signer")
        fingerprint = fingerprint.replace(" ", "")
        trusted = self.cfg.trusted_signers
        if self.cfg.signature_format == "gpg":
            fingerprint, trusted = fingerprint.upper(), tuple(x.upper() for x in trusted)
        # SSH SHA256 fingerprints contain case-sensitive base64; never case-fold.
        if status != "G" or not any(__import__("hmac").compare_digest(fingerprint, x) for x in trusted):
            raise UpdateError("commit signer is not trusted")

    def _stage(self, commit: str) -> Path:
        release = self.cfg.releases / commit
        if release.exists(): raise UpdateError("target release directory already exists")
        self.cfg.releases.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git("worktree", "add", "--detach", str(release), commit)
        try:
            lock = release / "requirements.lock"
            if not lock.is_file() or "--hash=" not in lock.read_text(encoding="utf-8"):
                raise UpdateError("release lacks a hash-locked requirements.lock")
            py = str(release / ".venv" / "bin" / "python")
            self.cmd((sys.executable, "-m", "venv", str(release / ".venv")), timeout=300)
            self.cmd((py, "-m", "pip", "install", "--require-hashes", "-r", str(lock)), timeout=900)
            self.cmd((py, "-m", "pip", "install", "--no-deps", str(release)), timeout=300)
            self.cmd((py, "-m", "pytest", "-q"), cwd=release, timeout=900)
            smoke = "from fastapi.testclient import TestClient;from dsette_server.app import app;r=TestClient(app).get('/api/v1/health');assert r.status_code==200"
            env = os.environ.copy(); env["DSETTE_DATA_DIR"] = str(release / ".smoke-data")
            result = subprocess.run([py, "-c", smoke], cwd=release, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60, check=False)
            if result.returncode: raise UpdateError("release smoke test failed: " + redact((result.stdout or "")[-1000:]))
            (release/".dsette-commit").write_text(commit+"\n",encoding="ascii")
            return release
        except Exception:
            # A failed dependency install/test must be retryable and must not
            # count as a retained release.
            try: self._git("worktree", "remove", "--force", str(release))
            except Exception: shutil.rmtree(release, ignore_errors=True)
            raise

    def _backup_db(self, job_id: str) -> Path | None:
        source = self.cfg.data_dir / "dsette.db"
        if not source.exists(): return None
        self.cfg.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.cfg.backups / f"dsette-{job_id}.db"
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst: src.backup(dst)
        os.chmod(target, 0o600); return target

    def _switch(self, release: Path) -> Path | None:
        previous = self.cfg.current.resolve() if self.cfg.current.is_symlink() else None
        temp = self.cfg.current.with_name("current.new")
        temp.unlink(missing_ok=True); temp.symlink_to(release, target_is_directory=True); os.replace(temp, self.cfg.current)
        return previous

    def _service(self, action: str, legacy: tuple[str,...]) -> None:
        if action not in {"start","stop","restart"}: raise UpdateError("invalid service-control action")
        if self.cfg.control_dir is None:
            self.cmd(legacy,timeout=60); return
        directory=self.cfg.control_dir
        if not directory.is_dir() or directory.is_symlink(): raise UpdateError("service-control broker directory is unavailable")
        marker=directory/action
        fd=os.open(marker,os.O_CREAT|os.O_WRONLY|getattr(os,"O_NOFOLLOW",0),0o600); os.close(fd)
        deadline=time.monotonic()+self.cfg.control_timeout
        while marker.exists():
            if time.monotonic()>=deadline: raise UpdateError(f"service-control broker timed out during {action}")
            time.sleep(.1)

    def _restart(self) -> None: self._service("restart",self.cfg.restart_command)
    def _stop(self) -> None: self._service("stop",self.cfg.stop_command)
    def _start(self) -> None: self._service("start",self.cfg.start_command)

    def _healthy(self, commit: str) -> bool:
        context = None
        if self.cfg.ca_file:
            import ssl; context = ssl.create_default_context(cafile=str(self.cfg.ca_file))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.cfg.health_url, timeout=3, context=context) as response:
                    body = json.load(response)
                    if response.status == 200 and body.get("status") == "ok" and body.get("git_commit") == commit: return True
            except Exception: pass
            time.sleep(1)
        return False

    def _restore(self, backup: Path | None) -> None:
        if backup:
            temp = self.cfg.data_dir / "dsette.db.rollback"
            # The supervisor is a separate root-owned systemd unit. Stop the
            # app before replacing SQLite and its WAL; copying over a live WAL
            # database can silently restore a mixed point in time.
            original=(self.cfg.data_dir/"dsette.db").stat(); self._stop()
            with sqlite3.connect(backup) as src, sqlite3.connect(temp) as dst: src.backup(dst)
            os.chmod(temp,stat.S_IMODE(original.st_mode))
            if hasattr(os,"chown"): os.chown(temp,original.st_uid,original.st_gid)
            os.replace(temp, self.cfg.data_dir / "dsette.db")
            for suffix in ("-wal", "-shm"):
                Path(str(self.cfg.data_dir / "dsette.db") + suffix).unlink(missing_ok=True)
            self._start()

    def _prune(self) -> None:
        items = sorted((p for p in self.cfg.releases.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in items[3:]:
            if self.cfg.current.is_symlink() and self.cfg.current.resolve() == old.resolve(): continue
            self._git("worktree", "remove", "--force", str(old))

    def execute(self, job_id: str) -> dict:
        self.cfg.validate(); self.job = read_job(job_id, self.cfg); self.job["state"] = "running"; _write_job(self.cfg, job_id, self.job)
        previous = backup = release = None
        owns_maintenance = False
        try:
            with FileLock(self.cfg.lock):
                if not self.cfg.current.is_symlink():
                    raise UpdateError("current release symlink is not initialized")
                self.cfg.maintenance.write_text(job_id, encoding="ascii")
                owns_maintenance = True
                self._wait_for_uploads(bool(self.job.get("force")))
                current,target = self._resolve_target(); self.job["target"] = target; _write_job(self.cfg, job_id, self.job)
                if __import__("hmac").compare_digest(current.lower(),target.lower()):
                    self.job["state"]="complete"; self.log("no update: deployed commit is current"); return self.job
                self._verify_signature(target); release = self._stage(target); backup = self._backup_db(job_id)
                previous = self._switch(release); self._restart()
                if not self._healthy(target): raise UpdateError("new release failed health verification")
                self._prune(); self.job["state"] = "complete"; self.log("update completed")
        except Exception as exc:
            self.log(f"update failed: {exc}")
            try:
                if previous and release and self.cfg.current.is_symlink() and self.cfg.current.resolve() == release.resolve():
                    self._switch(previous); self._restore(backup)
                    if not backup: self._restart()
                    self.log("rollback completed")
            except Exception as rollback:
                self.job["rollback_error"]=redact(str(rollback))[:1000]
                self.log(f"rollback failed: {rollback}")
            self.job["state"] = "failed"; self.job["error"] = redact(str(exc))[:1000]
        finally:
            if owns_maintenance:
                try:
                    if self.cfg.maintenance.read_text(encoding="ascii") == job_id:
                        self.cfg.maintenance.unlink(missing_ok=True)
                except FileNotFoundError: pass
            _write_job(self.cfg, job_id, self.job)
        return self.job


def update_available(cfg: UpdateConfig | None = None) -> str | None:
    """Read-only candidate advertisement; execute still verifies signatures."""
    cfg=cfg or UpdateConfig(); cfg.validate()
    if not cfg.remote_url or cfg.remote_url.startswith("-") or re.search(r"https?://[^/\s]+@",cfg.remote_url): raise UpdateError("DSETTE_UPDATE_REMOTE_URL must be credential-free")
    marker=cfg.current/".dsette-commit"
    current=os.getenv("DSETTE_GIT_COMMIT") or (marker.read_text(encoding="ascii").strip() if marker.is_file() else None)
    if not current or not re.fullmatch(r"[0-9a-fA-F]{40,64}",current): raise UpdateError("deployed commit marker is unavailable")
    ref=f"refs/heads/{cfg.branch}"
    result=subprocess.run(["git","ls-remote","--exit-code","--heads","--",cfg.remote_url,ref],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,check=False)
    if result.returncode or not result.stdout.strip(): raise UpdateError("remote update check failed")
    target=result.stdout.split()[0]
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}",target): raise UpdateError("remote returned invalid commit")
    return target if not __import__("hmac").compare_digest(current.lower(),target.lower()) else None


_check_lock = threading.Lock()
_last_check = 0.0
_last_result: dict = {"update_available": None}


def check_update_on_admin_login(cfg: UpdateConfig | None = None) -> dict:
    """Throttled login hook; call only after authenticating an admin device.

    Failures never break login and never bypass verification. Set
    DSETTE_AUTO_UPDATE_ON_ADMIN_LOGIN=true to launch a verified update job.
    """
    global _last_check, _last_result
    cfg = cfg or UpdateConfig()
    interval = max(60, int(os.getenv("DSETTE_UPDATE_CHECK_INTERVAL", "600")))
    now = time.monotonic()
    with _check_lock:
        if now - _last_check < interval: return dict(_last_result)
        _last_check = now
        try:
            target = update_available(cfg)
            result = {"update_available": target}
            if target and os.getenv("DSETTE_AUTO_UPDATE_ON_ADMIN_LOGIN", "false").lower() in {"1", "true", "yes"}:
                result["update_job_id"] = launch_update(cfg=cfg)
            _last_result = result
        except Exception:
            # Do not expose repository paths, signer identities, or credentials
            # to an API login response. Operational detail stays in admin logs.
            _last_result = {"update_available": None, "update_check_failed": True}
        return dict(_last_result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dsette_server.updater")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run"); run.add_argument("job_id")
    sub.add_parser("run-queued")
    create = sub.add_parser("create"); create.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "create": print(create_job(force=args.force)); return 0
    if args.command == "run-queued":
        cfg = UpdateConfig(); cfg.trigger.unlink(missing_ok=True); failed = False
        while True:
            cfg.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
            queued = []
            for path in cfg.jobs.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if value.get("state") == "queued": queued.append((value.get("created", 0), value["job_id"]))
                except Exception: continue
            if not queued: break
            job = Updater(cfg).execute(min(queued)[1]); failed |= job["state"] != "complete"
        return 1 if failed else 0
    job = Updater().execute(args.job_id); return 0 if job["state"] == "complete" else 1


if __name__ == "__main__": raise SystemExit(main())

