"""Real local Git signatures; package installation is isolated from the test env."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dsette_client.selfupdate import update_checkout


def test_real_signed_fast_forward_uses_fetch_head_and_rejects_unsigned(tmp_path,monkeypatch):
    git=shutil.which("git"); keygen=shutil.which("ssh-keygen")
    if not git or not keygen: pytest.skip("Git/OpenSSH signing tools unavailable")
    original_run=subprocess.run
    def command(args,cwd=None):
        result=original_run(args,cwd=cwd,capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stderr
        return result.stdout.strip()
    source=tmp_path/"source"; source.mkdir(); checkout=tmp_path/"checkout"; key=tmp_path/"signing_key"
    command([keygen,"-q","-t","ed25519","-N","","-f",str(key)])
    command([git,"init","-q","-b","main"],source)
    for name,value in (("user.name","Client Update Test"),("user.email","client@example.invalid"),("gpg.format","ssh"),("user.signingkey",str(key)),("commit.gpgsign","true")):
        command([git,"config",name,value],source)
    (source/"requirements.lock").write_text("# test lock; pip is mocked\n")
    (source/"version.txt").write_text("initial")
    command([git,"add","."],source); command([git,"commit","-q","-m","signed initial"],source)
    command([git,"clone","-q","--branch","main",str(source),str(checkout)])
    # Explicit branch fetches may only update FETCH_HEAD. A stale origin/main
    # must not prevent the fetched, signed commit from being installed.
    command([git,"config","--unset-all","remote.origin.fetch"],checkout)
    (source/"version.txt").write_text("signed update")
    command([git,"add","version.txt"],source); command([git,"commit","-q","-m","signed update"],source)
    target=command([git,"rev-parse","HEAD"],source)
    allowed=tmp_path/"allowed_signers"
    allowed.write_text("client@example.invalid "+Path(str(key)+".pub").read_text().strip()+"\n")
    pip_calls=[]
    def run(args,**kwargs):
        if args[:3]==[sys.executable,"-m","pip"]:
            pip_calls.append(args); return subprocess.CompletedProcess(args,0)
        return original_run(args,**kwargs)
    monkeypatch.setattr(subprocess,"run",run)
    assert update_checkout(checkout,trusted_signers=allowed)==target
    assert command([git,"rev-parse","HEAD"],checkout)==target
    assert "--require-hashes" in pip_calls[0]
    assert (checkout/"version.txt").read_text()=="signed update"
    (source/"version.txt").write_text("unsigned update")
    command([git,"add","version.txt"],source)
    command([git,"-c","commit.gpgsign=false","commit","-q","-m","unsigned update"],source)
    with pytest.raises(RuntimeError,match="verification"):
        update_checkout(checkout,trusted_signers=allowed)
    assert command([git,"rev-parse","HEAD"],checkout)==target
    assert len(pip_calls)==2
