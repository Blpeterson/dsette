"""Explicit, signed fast-forward client updates for source checkouts."""
import re
import subprocess
import sys
from pathlib import Path

from . import __version__


def update_required(minimum,current=__version__):
    def parts(value):
        match=re.match(r"^(\d+)\.(\d+)\.(\d+)",value)
        if not match: raise ValueError("Invalid version string")
        return tuple(map(int,match.groups()))
    return parts(current)<parts(minimum)


def update_checkout(checkout,*,branch="main",remote="origin",trusted_signers=None):
    """Refuse unsigned/untrusted revisions; never invoke a shell or force Git."""
    root=Path(checkout).expanduser().resolve()
    if not (root/".git").exists(): raise ValueError("Client self-update requires a Git source checkout")
    if not trusted_signers or not Path(trusted_signers).is_file():
        raise ValueError("Configure trusted_signers with an SSH allowed-signers file before updating")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*",branch) or ".." in branch or branch.endswith("/"):
        raise ValueError("Invalid update branch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*",remote): raise ValueError("Invalid update remote")
    def git(*args):
        result=subprocess.run(["git","-c",f"safe.directory={root}",*args],cwd=root,capture_output=True,text=True,timeout=120)
        if result.returncode: raise RuntimeError("Client update verification failed during git "+args[0])
        return result.stdout.strip()
    if git("status","--porcelain"): raise ValueError("Commit or save local changes before updating")
    git("fetch","--no-tags",remote,branch)
    target=git("rev-parse","--verify","FETCH_HEAD^{commit}")
    git("merge-base","--is-ancestor","HEAD",target)
    git("-c","gpg.format=ssh","-c",f"gpg.ssh.allowedSignersFile={Path(trusted_signers).resolve()}","verify-commit",target)
    # Verify the dependency lock exists in the target before changing HEAD.
    git("cat-file","-e",f"{target}:requirements.lock")
    git("merge","--ff-only",target)
    subprocess.run([sys.executable,"-m","pip","install","--require-hashes","-r",str(root/"requirements.lock")],cwd=root,check=True)
    subprocess.run([sys.executable,"-m","pip","install","--no-deps","."],cwd=root,check=True)
    return target
