#!/usr/bin/env python3
"""Portable bootstrap and transactional, signed client updates (Python 3.9+)."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".dsette-runtime"
TRUSTED = "SHA256:CIBS0oiBbBuEm2QnkUPNQDSH6KfOj4vFgcMJJ42YeM0"
INTERVAL = 600


class LaunchError(RuntimeError): pass


def run(args, *, cwd=None, capture=False, timeout=600):
    result=subprocess.run([str(a) for a in args],cwd=cwd or ROOT,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          text=True,timeout=timeout)
    if result.returncode:
        # Never echo credentials from Git/network command output.
        raise LaunchError("Command failed: "+Path(str(args[0])).name)
    return result.stdout.strip() if capture else ""


def git(*args):
    return run(["git","-c",f"safe.directory={ROOT}",*args],capture=True,timeout=30)


def metadata_home():
    override=os.getenv("DSETTE_LAUNCHER_HOME")
    if override: return Path(override).expanduser().resolve()
    if os.name=="nt": return Path(os.environ.get("LOCALAPPDATA",Path.home()/"AppData/Local"))/"dsette-launcher"
    if sys.platform=="darwin": return Path.home()/"Library/Application Support/dsette-launcher"
    return Path(os.getenv("XDG_CONFIG_HOME",Path.home()/".config"))/"dsette-launcher"


def state_path():
    identity=hashlib.sha256(str(ROOT).encode()).hexdigest()[:20]
    return metadata_home()/(identity+".json")


def load_state():
    try: return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError,ValueError): return {}


def save_state(state):
    target=state_path(); target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
    with temporary.open("x",encoding="utf-8") as out: json.dump(state,out)
    if os.name!="nt": temporary.chmod(0o600)
    os.replace(temporary,target)


def python_in(directory):
    return Path(directory)/("Scripts/python.exe" if os.name=="nt" else "bin/python")


def probe_python(path,package=False):
    if not Path(path).is_file(): return None
    script="import json,sys;print(json.dumps(list(sys.version_info[:2])))"
    if package: script="from dsette_client import __version__;print(__version__)"
    try: return run([path,"-c",script],capture=True,timeout=15)
    except (LaunchError,OSError,subprocess.TimeoutExpired): return None


def find_environment(state):
    candidates=[]
    remembered=state.get("python")
    if remembered:
        candidate=Path(remembered)
        # A venv interpreter itself may legitimately be a symlink to Python.
        try: candidate.parent.parent.resolve().relative_to(CACHE.resolve()); candidates.append(candidate)
        except ValueError: pass
    candidates += [python_in(ROOT/name) for name in (".venv312",".venv")]
    for candidate in candidates:
        version=probe_python(candidate)
        if version and tuple(json.loads(version))>=(3,11) and probe_python(candidate,package=True):
            return candidate
    return None


def uv_command():
    executable=shutil.which("uv")
    if executable: return [executable]
    for python in (python_in(ROOT/".venv"),python_in(ROOT/".venv312"),Path(sys.executable),python_in(ROOT/".dsette-bootstrap")):
        if python.is_file():
            try:
                run([python,"-m","uv","--version"],capture=True,timeout=10)
                return [str(python),"-m","uv"]
            except (LaunchError,OSError,subprocess.TimeoutExpired): pass
    bootstrap=ROOT/".dsette-bootstrap"
    python=python_in(bootstrap)
    pip_ready=False
    if python.is_file():
        try: run([python,"-m","pip","--version"],capture=True,timeout=10); pip_ready=True
        except (LaunchError,OSError,subprocess.TimeoutExpired): pass
    if not pip_ready:
        print("Preparing an isolated bootstrap environment…",flush=True)
        try: run([sys.executable,"-m","venv",bootstrap])
        except LaunchError:
            raise LaunchError("Python venv/pip support is required. On Ubuntu/Debian install python3-venv, then run the launcher again") from None
    python=python_in(bootstrap)
    if not python.is_file(): raise LaunchError("Bootstrap folder is incomplete; use a fresh checkout")
    run([python,"-m","pip","install","--require-hashes","-r",ROOT/"scripts/bootstrap-requirements.txt"])
    return [str(python),"-m","uv"]


def pin_signer():
    destination=metadata_home()/"release-signers"
    if destination.is_file(): return destination
    source=ROOT/"deploy/release-signers"
    selected=[]
    for line in source.read_text(encoding="utf-8").splitlines():
        fields=line.split()
        if len(fields)<3 or fields[1]!="ssh-ed25519": continue
        fingerprint="SHA256:"+base64.b64encode(hashlib.sha256(base64.b64decode(fields[2],validate=True)).digest()).decode().rstrip("=")
        if fingerprint==TRUSTED: selected.append(line)
    if not selected: raise LaunchError("Bundled release signer does not match the trusted key")
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try:
        with destination.open("x",encoding="utf-8") as out: out.write("\n".join(selected)+"\n")
    except FileExistsError: pass
    if os.name!="nt": destination.chmod(0o600)
    return destination


def verify_commit(target,signers):
    try: git("-c","gpg.format=ssh","-c",f"gpg.ssh.allowedSignersFile={signers}","verify-commit",target)
    except LaunchError:
        raise LaunchError("Release signature verification failed. Git 2.34+ and OpenSSH are required; no update applied") from None


@contextlib.contextmanager
def update_lock():
    CACHE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (CACHE/"update.lock").open("a+b") as lock:
        try:
            if os.name=="nt":
                import msvcrt
                if lock.tell()==0: lock.write(b"0"); lock.flush()
                lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError: raise LaunchError("Another update is in progress; try again shortly")
        yield


def install_release(target):
    """Prepare and smoke-test a new environment before touching current code."""
    location=CACHE/(target[:12]+"-"+uuid.uuid4().hex[:8])
    source=location/"source"; environment=location/"env"
    location.mkdir(parents=True,mode=0o700)
    git("worktree","add","--detach",str(source),target)
    uv=uv_command()
    print("Preparing Python 3.12 and the verified release (first install may download dependencies)…",flush=True)
    run([*uv,"venv","--seed","--python","3.12",environment])
    python=python_in(environment)
    run([*uv,"pip","install","--python",python,"--require-hashes","-r",source/"requirements.lock"])
    run([*uv,"pip","install","--python",python,"--no-deps",source])
    run([python,"-m","dsette_client.main","--version"],capture=True,timeout=30)
    return python


def perform_update(state,fetch=True):
    with update_lock():
        if git("status","--porcelain"):
            raise LaunchError("Local changes found; save/commit them before updating. Nothing was overwritten")
        if git("symbolic-ref","--short","HEAD")!="main":
            raise LaunchError("Updates require the main branch; your branch was left untouched")
        signers=pin_signer()
        before=git("rev-parse","HEAD")
        if fetch:
            print("Checking for a signed update…",flush=True)
            git("fetch","--no-tags","origin","main")
            target=git("rev-parse","--verify","FETCH_HEAD^{commit}")
            git("merge-base","--is-ancestor",before,target)
        else: target=before
        verify_commit(target,signers)
        existing=find_environment(state)
        if target==before and existing and state.get("installed_head")==target:
            state["checked_at"]=time.time(); save_state(state)
            print("Already up to date.",flush=True)
            return existing
        python=install_release(target)
        # Git rechecks local changes and FF safety after downloads complete.
        if git("rev-parse","HEAD")!=before: raise LaunchError("Checkout changed during update; retry")
        if target!=before: git("merge","--ff-only",target)
        state.update(python=str(python),installed_head=target,checked_at=time.time())
        save_state(state)
        print("Verified update installed. Device keys and enrollment were preserved.",flush=True)
        return python


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("run","update"))
    parser.add_argument("--no-update",action="store_true",help="Launch installed version without checking online")
    parser.add_argument("--launcher-update",action="store_true",help=argparse.SUPPRESS)
    options,client_args=parser.parse_known_args(argv)
    if client_args[:1]==["--"]: client_args=client_args[1:]
    if sys.version_info<(3,9): raise LaunchError("Install Python 3.9+ to bootstrap Datasette")
    state=load_state(); python=find_environment(state)
    manual=options.mode=="update" or options.launcher_update
    automatic=not options.no_update and os.getenv("DSETTE_AUTO_UPDATE","1").lower() not in {"0","false","no"}
    changed=False
    if automatic and state.get("installed_head"):
        try: changed=git("rev-parse","HEAD")!=state["installed_head"]
        except LaunchError: pass
    automatic=automatic and (changed or time.time()-state.get("checked_at",0)>=INTERVAL)
    if any(arg in {"--version","--help"} for arg in client_args): automatic=False
    if manual or automatic or python is None:
        try: python=perform_update(state,fetch=manual or automatic)
        except (LaunchError,OSError,subprocess.SubprocessError) as error:
            if manual or python is None: raise LaunchError(str(error)) from None
            print("Update skipped: "+str(error)+". Starting the installed version.",flush=True)
            state["checked_at"]=time.time(); save_state(state)
    if manual: return 0
    return subprocess.call([str(python),"-m","dsette_client.main",*client_args],cwd=ROOT)


if __name__=="__main__":
    try: raise SystemExit(main())
    except (LaunchError,OSError,subprocess.SubprocessError) as error:
        print("Datasette launcher: "+str(error),file=sys.stderr)
        raise SystemExit(1)
