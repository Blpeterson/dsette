import importlib.util
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/"dsette_launcher.py"
    spec=importlib.util.spec_from_file_location("dsette_launcher_test",path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    root=tmp_path/"checkout"; root.mkdir(); cache=root/".dsette-runtime"
    monkeypatch.setattr(module,"ROOT",root); monkeypatch.setattr(module,"CACHE",cache)
    monkeypatch.setenv("DSETTE_LAUNCHER_HOME",str(tmp_path/"metadata"))
    return module


def test_state_round_trip_is_private_and_checkout_scoped(launcher):
    launcher.save_state({"installed_head":"abc"})
    assert launcher.load_state()=={"installed_head":"abc"}
    assert launcher.state_path().parent==launcher.metadata_home()
    if os.name!="nt": assert stat.S_IMODE(launcher.state_path().stat().st_mode)==0o600


def test_managed_runtime_rejects_remembered_python_outside_cache(launcher,monkeypatch,tmp_path):
    outside=tmp_path/"outside"/"bin"/"python"; outside.parent.mkdir(parents=True); outside.touch()
    monkeypatch.setattr(launcher,"probe_python",lambda path,package=False:("0.2.3" if package else "[3,12]") if Path(path).is_file() else None)
    assert launcher.find_environment({"python":str(outside)}) is None


def test_managed_runtime_accepts_python_inside_cache(launcher,monkeypatch):
    managed=launcher.CACHE/"release"/"env"/"bin"/"python"; managed.parent.mkdir(parents=True); managed.touch()
    monkeypatch.setattr(launcher,"probe_python",lambda path,package=False:("0.2.3" if package else "[3,12]") if Path(path).is_file() else None)
    assert launcher.find_environment({"python":str(managed)})==managed


def test_pin_signer_rejects_untrusted_bundled_key(launcher):
    deploy=launcher.ROOT/"deploy"; deploy.mkdir()
    deploy.joinpath("release-signers").write_text("release ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
    with pytest.raises(launcher.LaunchError,match="trusted key"): launcher.pin_signer()


def test_dirty_checkout_refuses_before_signature_or_install(launcher,monkeypatch):
    calls=[]
    monkeypatch.setattr(launcher,"git",lambda *args:(calls.append(args) or " M changed"))
    monkeypatch.setattr(launcher,"install_release",lambda target:pytest.fail("must not install"))
    with pytest.raises(launcher.LaunchError,match="Local changes"): launcher.perform_update({},fetch=True)
    assert calls==[("status","--porcelain")]


def test_non_main_branch_refuses_before_fetch(launcher,monkeypatch):
    calls=[]
    def git(*args):
        calls.append(args); return "" if args[0]=="status" else "feature"
    monkeypatch.setattr(launcher,"git",git)
    with pytest.raises(launcher.LaunchError,match="main branch"): launcher.perform_update({},fetch=True)
    assert not any(args[0]=="fetch" for args in calls)


def update_git(launcher,*,fail_nonff=False):
    calls=[]; before="a"*40; target="b"*40
    def git(*args):
        calls.append(args)
        if args[:2]==("status","--porcelain"): return ""
        if args[:2]==("symbolic-ref","--short"): return "main"
        if args[:2]==("rev-parse","HEAD"): return before
        if args[:2]==("rev-parse","--verify"): return target
        if args[0]=="merge-base" and fail_nonff: raise launcher.LaunchError("not ancestor")
        return ""
    return git,calls,before,target


def test_non_fast_forward_refuses_without_install_or_merge(launcher,monkeypatch):
    git,calls,_,_=update_git(launcher,fail_nonff=True); monkeypatch.setattr(launcher,"git",git)
    monkeypatch.setattr(launcher,"pin_signer",lambda:Path("signers"))
    monkeypatch.setattr(launcher,"install_release",lambda target:pytest.fail("must not install"))
    with pytest.raises(launcher.LaunchError,match="ancestor"): launcher.perform_update({},fetch=True)
    assert not any(args[0]=="merge" for args in calls)


def test_unsigned_target_refuses_without_install_or_merge(launcher,monkeypatch):
    git,calls,_,_=update_git(launcher); monkeypatch.setattr(launcher,"git",git)
    monkeypatch.setattr(launcher,"pin_signer",lambda:Path("signers"))
    monkeypatch.setattr(launcher,"verify_commit",lambda *a:(_ for _ in ()).throw(launcher.LaunchError("unsigned")))
    monkeypatch.setattr(launcher,"install_release",lambda target:pytest.fail("must not install"))
    with pytest.raises(launcher.LaunchError,match="unsigned"): launcher.perform_update({},fetch=True)
    assert not any(args[0]=="merge" for args in calls)


def test_failed_staged_install_leaves_head_and_state_unchanged(launcher,monkeypatch):
    git,calls,before,_=update_git(launcher); monkeypatch.setattr(launcher,"git",git)
    monkeypatch.setattr(launcher,"pin_signer",lambda:Path("signers")); monkeypatch.setattr(launcher,"verify_commit",lambda *a:None)
    monkeypatch.setattr(launcher,"find_environment",lambda state:None)
    monkeypatch.setattr(launcher,"install_release",lambda target:(_ for _ in ()).throw(launcher.LaunchError("install failed")))
    saved=[]; monkeypatch.setattr(launcher,"save_state",lambda state:saved.append(dict(state)))
    state={"installed_head":before}
    with pytest.raises(launcher.LaunchError,match="install failed"): launcher.perform_update(state,fetch=True)
    assert state=={"installed_head":before} and not saved and not any(args[0]=="merge" for args in calls)


def main_setup(launcher,monkeypatch,state,python=None):
    python=python or launcher.ROOT/"managed-python"
    monkeypatch.setattr(launcher,"load_state",lambda:state)
    monkeypatch.setattr(launcher,"find_environment",lambda value:python)
    calls=[]; monkeypatch.setattr(launcher.subprocess,"call",lambda args,cwd=None:(calls.append((args,cwd)) or 0))
    return python,calls


def test_automatic_interval_skips_update_and_launches(launcher,monkeypatch):
    python,calls=main_setup(launcher,monkeypatch,{"checked_at":time.time()})
    monkeypatch.setattr(launcher,"perform_update",lambda *a,**k:pytest.fail("interval must skip"))
    assert launcher.main(["run","devices"])==0 and calls[0][0]==[str(python),"-m","dsette_client.main","devices"]


def test_no_update_flag_skips_online_check(launcher,monkeypatch):
    _,calls=main_setup(launcher,monkeypatch,{"checked_at":0})
    monkeypatch.setattr(launcher,"perform_update",lambda *a,**k:pytest.fail("--no-update must skip"))
    assert launcher.main(["run","--no-update","--version"])==0 and calls


def test_version_flag_disables_automatic_check(launcher,monkeypatch):
    _,calls=main_setup(launcher,monkeypatch,{"checked_at":0})
    monkeypatch.setattr(launcher,"perform_update",lambda *a,**k:pytest.fail("--version must skip"))
    assert launcher.main(["run","--version"])==0 and calls


def test_offline_automatic_check_falls_back_to_installed_runtime(launcher,monkeypatch):
    python,calls=main_setup(launcher,monkeypatch,{"checked_at":0})
    monkeypatch.setattr(launcher,"perform_update",lambda *a,**k:(_ for _ in ()).throw(launcher.LaunchError("offline")))
    saved=[]; monkeypatch.setattr(launcher,"save_state",lambda state:saved.append(dict(state)))
    assert launcher.main(["run","inbox"])==0
    assert calls[0][0][0]==str(python) and saved[0]["checked_at"]>0


def test_manual_update_failure_does_not_fall_back(launcher,monkeypatch):
    main_setup(launcher,monkeypatch,{"checked_at":0})
    monkeypatch.setattr(launcher,"perform_update",lambda *a,**k:(_ for _ in ()).throw(launcher.LaunchError("unsigned")))
    with pytest.raises(launcher.LaunchError,match="unsigned"): launcher.main(["update"])
