import os
import stat
import uuid

import pytest

from dsette_server.admin import place
from dsette_server.config import config
from dsette_server.store import connect, init_db


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    data=tmp_path/"data"; root=tmp_path/"allowed"
    monkeypatch.setattr(config,"data_dir",data)
    monkeypatch.setattr(config,"allowed_roots",(root.resolve(),))
    init_db(); (data/"inbox").mkdir(); root.mkdir()
    tid=str(uuid.uuid4())
    (data/"inbox"/tid).write_bytes(b"verified file")
    with connect() as db:
        db.execute("INSERT INTO transfers(id,device_id,name,size,sha256,status,created) VALUES(?,?,?,?,?,'verified',0)",(tid,"sender","file.txt",13,"0"*64))
    return tid,root,data,tmp_path


def test_place_copies_private_file_and_records_audit(inbox):
    tid,root,data,_=inbox
    place(tid,root)
    assert (root/"file.txt").read_bytes()==b"verified file"
    assert not (data/"inbox"/tid).exists()
    if os.name != "nt": assert stat.S_IMODE((root/"file.txt").stat().st_mode)==0o644
    with connect() as db:
        assert db.execute("SELECT status FROM transfers WHERE id=?",(tid,)).fetchone()[0]=="placed"
        assert db.execute("SELECT event FROM audit_log").fetchone()[0]=="place"


@pytest.mark.parametrize("name",["../escape","sub/file","sub\\file","/absolute","C:drive","..","","bad\nname"])
def test_rename_rejects_paths_and_controls(inbox,name):
    tid,root,data,_=inbox
    with pytest.raises(SystemExit): place(tid,root,name)
    assert (data/"inbox"/tid).exists()


def test_outside_root_rejected(inbox):
    tid,root,data,tmp=inbox
    with pytest.raises(SystemExit): place(tid,tmp/"outside")
    assert (data/"inbox"/tid).exists()


def test_existing_file_requires_force(inbox):
    tid,root,data,_=inbox
    (root/"file.txt").write_bytes(b"keep")
    with pytest.raises(SystemExit): place(tid,root)
    assert (root/"file.txt").read_bytes()==b"keep"
    assert (data/"inbox"/tid).exists()
    place(tid,root,force=True)
    assert (root/"file.txt").read_bytes()==b"verified file"


@pytest.mark.skipif(os.name=="nt",reason="POSIX symlinks")
def test_symlink_directory_outside_root_rejected(inbox):
    tid,root,data,tmp=inbox
    outside=tmp/"outside"; outside.mkdir()
    (root/"link").symlink_to(outside,target_is_directory=True)
    with pytest.raises(SystemExit): place(tid,root/"link")
    assert not (outside/"file.txt").exists()


@pytest.mark.skipif(os.name=="nt",reason="POSIX symlinks")
def test_force_refuses_symlink_target(inbox):
    tid,root,data,tmp=inbox
    outside=tmp/"outside.txt"; outside.write_bytes(b"keep")
    (root/"file.txt").symlink_to(outside)
    with pytest.raises(SystemExit): place(tid,root,force=True)
    assert outside.read_bytes()==b"keep"
