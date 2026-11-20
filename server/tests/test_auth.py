import base64
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

from dsette_server import auth
from dsette_server.config import config
from dsette_server.store import connect, init_db


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", tmp_path / "private")
    init_db()


def keypair():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return key, public


def device():
    key, public = keypair()
    device_id, _ = auth.enroll(auth.create_invite(), public, "test")
    return device_id, key


def sign(key, nonce):
    return base64.b64encode(key.sign(nonce.encode())).decode()


def test_enroll_login_and_invite_single_use():
    key, public = keypair()
    invite = auth.create_invite()
    did, admin = auth.enroll(invite, public, "first")
    assert admin
    with pytest.raises(ValueError):
        auth.enroll(invite, public, "again")
    nonce = auth.challenge(did)
    token = auth.verify(did, nonce, sign(key, nonce))
    assert auth.current_device("Bearer " + token)["id"] == did


def test_expired_invite_rejected():
    _, public = keypair()
    invite = auth.create_invite()
    with connect() as db:
        db.execute("UPDATE invites SET expires=0")
    with pytest.raises(ValueError):
        auth.enroll(invite, public, "expired")


def test_nonce_replay_rejected():
    did, key = device()
    nonce = auth.challenge(did)
    sig = sign(key, nonce)
    auth.verify(did, nonce, sig)
    with pytest.raises(ValueError):
        auth.verify(did, nonce, sig)


def test_bad_signature_burns_nonce():
    did, key = device()
    wrong_key, _ = keypair()
    nonce = auth.challenge(did)
    with pytest.raises(ValueError):
        auth.verify(did, nonce, sign(wrong_key, nonce))
    with pytest.raises(ValueError):
        auth.verify(did, nonce, sign(key, nonce))


def test_expired_nonce_rejected():
    did, key = device()
    nonce = auth.challenge(did)
    with connect() as db:
        db.execute("UPDATE nonces SET expires=0")
    with pytest.raises(ValueError):
        auth.verify(did, nonce, sign(key, nonce))


def test_revocation_invalidates_existing_session():
    did, key = device()
    nonce = auth.challenge(did)
    token = auth.verify(did, nonce, sign(key, nonce))
    with connect() as db:
        db.execute("UPDATE devices SET revoked=? WHERE id=?", (time.time(), did))
    with pytest.raises(HTTPException) as error:
        auth.current_device("Bearer " + token)
    assert error.value.status_code == 401
    with pytest.raises(ValueError):
        auth.challenge(did)


def test_expired_session_rejected():
    did, key = device()
    nonce = auth.challenge(did)
    token = auth.verify(did, nonce, sign(key, nonce))
    with connect() as db:
        db.execute("UPDATE sessions SET expires=0")
    with pytest.raises(HTTPException):
        auth.current_device("Bearer " + token)


def test_concurrent_invite_cannot_be_reused():
    _, public = keypair()
    invite = auth.create_invite()
    def attempt(_):
        try:
            return auth.enroll(invite, public, "racer")
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1


def test_only_one_concurrent_first_device_is_admin():
    _, public = keypair()
    invites = [auth.create_invite(), auth.create_invite()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda invite: auth.enroll(invite, public, "racer"), invites))
    assert sum(result[1] for result in results) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode check")
def test_database_is_private():
    assert stat.S_IMODE(config.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.db.stat().st_mode) == 0o600
