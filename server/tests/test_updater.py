import json, os, shutil, subprocess, threading, time
from pathlib import Path
import pytest
from dsette_server.updater import FileLock, UpdateConfig, UpdateError, Updater, _write_job, create_job, launch_update, read_job, redact, update_available

def cfg(tmp_path, **changes):
    values=dict(repo=tmp_path/"repo",data_dir=tmp_path/"data",remote="origin",branch="main",trusted_signers=("ABC123",),signature_format="gpg",wait_seconds=0,health_url="http://127.0.0.1:1",ca_file=None,control_dir=None,restart_command=("true",))
    values.update(changes); return UpdateConfig(**values)

def test_config_fails_closed_without_signers(tmp_path):
    with pytest.raises(UpdateError,match="TRUSTED_SIGNERS"): cfg(tmp_path,trusted_signers=()).validate()

@pytest.mark.parametrize("branch", ["--upload-pack=evil", "../main", "main;sh"])
def test_rejects_unsafe_branch(tmp_path, branch):
    with pytest.raises(UpdateError): cfg(tmp_path,branch=branch).validate()

def test_job_ids_and_secret_redaction(tmp_path):
    c=cfg(tmp_path); job=create_job(cfg=c); assert read_job(job,c)["state"]=="queued"
    assert "hunter2" not in redact("password=hunter2")
    assert "secret" not in redact("https://user:secret@example.com/repo.git")
    assert "abc.def" not in redact("Authorization: Bearer abc.def")
    with pytest.raises(UpdateError): read_job("../job",c)

def test_command_uses_argv_and_never_shell(tmp_path):
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs)); return subprocess.CompletedProcess(args,0,"ok")
    u=Updater(cfg(tmp_path),run); u.job={"job_id":create_job(cfg=cfg(tmp_path)),"log":[]}
    u.cmd(("git","status"))
    assert calls[0][0]==["git","status"] and "shell" not in calls[0][1]

def test_update_available_has_no_job_context(tmp_path, monkeypatch):
    c=cfg(tmp_path,remote_url="https://example.com/owner/repo.git")
    monkeypatch.setenv("DSETTE_GIT_COMMIT","a"*40)
    monkeypatch.setattr(subprocess,"run",lambda *a,**k:subprocess.CompletedProcess(a[0],0,"b"*40+"\trefs/heads/main\n"))
    assert update_available(c)=="b"*40

def test_signer_must_match_allowlist(tmp_path):
    outputs=iter(["verified", "G;NOT-TRUSTED"])
    def run(args,**kwargs): return subprocess.CompletedProcess(args,0,next(outputs))
    c=cfg(tmp_path); u=Updater(c,run); u.job={"job_id":create_job(cfg=c),"log":[]}
    with pytest.raises(UpdateError,match="not trusted"): u._verify_signature("a"*40)

def test_fast_forward_compares_deployed_worktree(tmp_path):
    c=cfg(tmp_path); deployed=tmp_path/"release"; deployed.mkdir(); c.data_dir.mkdir()
    try: c.current.symlink_to(deployed, target_is_directory=True)
    except OSError: pytest.skip("symlinks unavailable")
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        if "FETCH_HEAD^{commit}" in args: out="b"*40
        elif "HEAD^{commit}" in args: out="a"*40
        else: out=""
        return subprocess.CompletedProcess(args,0,out)
    u=Updater(c,run); u.job={"job_id":create_job(cfg=c),"log":[]}; u._resolve_target()
    head_call=next(x for x in calls if "HEAD^{commit}" in x)
    assert str(deployed) in head_call

def test_active_upload_timeout(tmp_path):
    c=cfg(tmp_path); c.data_dir.mkdir(); import sqlite3
    with sqlite3.connect(c.data_dir/"dsette.db") as db:
        db.execute("CREATE TABLE transfers(status TEXT)"); db.execute("INSERT INTO transfers VALUES('uploading')")
    u=Updater(c)
    with pytest.raises(UpdateError,match="active uploads"): u._wait_for_uploads(False)

def test_live_marker_counts_as_active_transfer(tmp_path):
    c=cfg(tmp_path); c.data_dir.mkdir(); (c.data_dir/"active-files-live123").touch()
    assert Updater(c)._active_uploads()==1

def test_lock_failure_preserves_other_updaters_maintenance(tmp_path):
    c=cfg(tmp_path); c.data_dir.mkdir(); c.maintenance.write_text("other")
    job=create_job(cfg=c)
    with FileLock(c.lock): result=Updater(c).execute(job)
    assert result["state"]=="failed" and c.maintenance.read_text()=="other"

def test_launch_queues_systemd_trigger_without_child(tmp_path,monkeypatch):
    c=cfg(tmp_path)
    monkeypatch.setattr(subprocess,"Popen",lambda *a,**k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    job=launch_update(cfg=c)
    assert read_job(job,c)["state"]=="queued" and c.trigger.exists()

def test_job_symlink_cannot_redirect_supervisor_write(tmp_path):
    c=cfg(tmp_path); c.jobs.mkdir(parents=True); outside=tmp_path/"outside"; outside.write_text("sentinel")
    job="a"*32
    try: (c.jobs/(job+".json")).symlink_to(outside)
    except OSError: pytest.skip("symlinks unavailable")
    with pytest.raises(UpdateError,match="unsafe"): _write_job(c,job,{"job_id":job,"state":"running"})
    assert outside.read_text()=="sentinel"

def test_sqlite_backup_uses_online_backup_api(tmp_path):
    c=cfg(tmp_path); c.data_dir.mkdir(); import sqlite3
    with sqlite3.connect(c.data_dir/"dsette.db") as db: db.execute("CREATE TABLE x(v)"); db.execute("INSERT INTO x VALUES(7)")
    out=Updater(c)._backup_db("abc")
    with sqlite3.connect(out) as db: assert db.execute("SELECT v FROM x").fetchone()[0]==7

def test_real_ssh_signed_commit_is_verified(tmp_path):
    git=shutil.which("git"); ssh_keygen=shutil.which("ssh-keygen")
    if not git or not ssh_keygen: pytest.skip("git/ssh-keygen unavailable")
    repo=tmp_path/"repo"; repo.mkdir(); key=tmp_path/"signing_key"
    def command(args,cwd=None):
        return subprocess.run(args,cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=True,timeout=30).stdout.strip()
    try:
        command([ssh_keygen,"-q","-t","ed25519","-N","","-f",str(key)])
        command([git,"init","-q"],repo)
        for name,value in (("user.name","Updater Test"),("user.email","updater@example.invalid"),("gpg.format","ssh"),("user.signingkey",str(key)),("commit.gpgsign","true")):
            command([git,"config",name,value],repo)
        (repo/"file").write_text("signed",encoding="utf-8")
        command([git,"add","file"],repo); command([git,"commit","-q","-m","signed"],repo)
    except subprocess.CalledProcessError as exc: pytest.skip("Git SSH signing unavailable: "+exc.stdout[-200:])
    pub=Path(str(key)+".pub").read_text(encoding="ascii").strip()
    allowed=tmp_path/"allowed_signers"; allowed.write_text("updater@example.invalid "+pub+"\n",encoding="ascii")
    fingerprint=command([ssh_keygen,"-lf",str(key)+".pub","-E","sha256"]).split()[1]
    c=cfg(tmp_path,repo=repo,trusted_signers=(fingerprint,),allowed_signers_file=allowed,signature_format="ssh")
    commit=command([git,"rev-parse","HEAD"],repo)
    Updater(c)._verify_signature(commit)

@pytest.mark.skipif(os.name=="nt",reason="Server updater requires Linux/systemd and POSIX atomic directory-symlink replacement")
def test_failed_health_rolls_back_symlink_and_database(tmp_path,monkeypatch):
    c=cfg(tmp_path); c.data_dir.mkdir(); c.releases.mkdir()
    previous=tmp_path/"previous"; release=c.releases/("b"*40); previous.mkdir(); release.mkdir()
    try: c.current.symlink_to(previous,target_is_directory=True)
    except OSError: pytest.skip("symlinks unavailable")
    import sqlite3
    with sqlite3.connect(c.data_dir/"dsette.db") as db:
        db.execute("CREATE TABLE state(value TEXT)"); db.execute("INSERT INTO state VALUES('before')")
    original_mode=(c.data_dir/"dsette.db").stat().st_mode & 0o777
    job=create_job(cfg=c); updater=Updater(c)
    monkeypatch.setattr(updater,"_wait_for_uploads",lambda force:None)
    monkeypatch.setattr(updater,"_resolve_target",lambda:("a"*40,"b"*40))
    monkeypatch.setattr(updater,"_verify_signature",lambda commit:None)
    monkeypatch.setattr(updater,"_stage",lambda commit:release)
    restarts=[]
    def restart():
        restarts.append(True)
        with sqlite3.connect(c.data_dir/"dsette.db") as db: db.execute("UPDATE state SET value='migrated'")
    monkeypatch.setattr(updater,"_restart",restart)
    monkeypatch.setattr(updater,"_healthy",lambda commit:False)
    monkeypatch.setattr(updater,"_stop",lambda:None); monkeypatch.setattr(updater,"_start",lambda:None)
    result=updater.execute(job)
    assert result["state"]=="failed" and c.current.resolve()==previous.resolve()
    with sqlite3.connect(c.data_dir/"dsette.db") as db:
        assert db.execute("SELECT value FROM state").fetchone()[0]=="before"
    assert (c.data_dir/"dsette.db").stat().st_mode & 0o777 == original_mode
    assert restarts

def test_execute_current_target_completes_without_staging(tmp_path,monkeypatch):
    c=cfg(tmp_path); c.data_dir.mkdir(); deployed=tmp_path/"deployed"; deployed.mkdir()
    try: c.current.symlink_to(deployed,target_is_directory=True)
    except OSError: pytest.skip("symlinks unavailable")
    job=create_job(cfg=c); updater=Updater(c); commit="a"*40
    monkeypatch.setattr(updater,"_wait_for_uploads",lambda force:None)
    monkeypatch.setattr(updater,"_resolve_target",lambda:(commit,commit))
    monkeypatch.setattr(updater,"_stage",lambda commit:(_ for _ in ()).throw(AssertionError("must not stage")))
    result=updater.execute(job)
    assert result["state"]=="complete" and "no update" in result["log"][-1]

def test_service_control_broker_waits_for_ack(tmp_path):
    control=tmp_path/"control"; control.mkdir(); c=cfg(tmp_path,control_dir=control,control_timeout=2)
    seen=[]
    def broker():
        marker=control/"restart"; deadline=time.monotonic()+1
        while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)
        seen.append(marker.exists()); marker.unlink()
    thread=threading.Thread(target=broker); thread.start(); Updater(c)._restart(); thread.join()
    assert seen==[True] and not (control/"restart").exists()

def test_service_control_broker_timeout(tmp_path):
    control=tmp_path/"control"; control.mkdir(); c=cfg(tmp_path,control_dir=control,control_timeout=.05)
    with pytest.raises(UpdateError,match="timed out"): Updater(c)._restart()

@pytest.mark.skipif(os.name=="nt",reason="POSIX symlink rollback")
def test_rollback_exception_still_terminal(tmp_path,monkeypatch):
    c=cfg(tmp_path); c.data_dir.mkdir(); previous=tmp_path/"previous"; release=c.releases/("b"*40)
    previous.mkdir(); release.mkdir(parents=True); c.current.symlink_to(previous,target_is_directory=True)
    job=create_job(cfg=c); updater=Updater(c)
    monkeypatch.setattr(updater,"_wait_for_uploads",lambda force:None)
    monkeypatch.setattr(updater,"_resolve_target",lambda:("a"*40,"b"*40))
    monkeypatch.setattr(updater,"_verify_signature",lambda commit:None)
    monkeypatch.setattr(updater,"_stage",lambda commit:release)
    monkeypatch.setattr(updater,"_backup_db",lambda job:tmp_path/"backup")
    monkeypatch.setattr(updater,"_restart",lambda:None); monkeypatch.setattr(updater,"_healthy",lambda commit:False)
    monkeypatch.setattr(updater,"_restore",lambda backup:(_ for _ in ()).throw(UpdateError("restore exploded")))
    result=updater.execute(job)
    assert result["state"]=="failed" and "health" in result["error"] and "restore exploded" in result["rollback_error"]

