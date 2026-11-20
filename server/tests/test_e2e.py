"""Exercise actual TCP streaming, which an in-memory ASGI client can buffer."""
import base64
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dsette_server.auth import create_invite
from dsette_server.config import config
from dsette_server.store import init_db


def enroll_and_login(client, name):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    response = client.post("/api/v1/enroll", json={
        "invite_token": create_invite(), "public_key": base64.b64encode(public).decode(),
        "device_name": name,
    })
    response.raise_for_status()
    device_id = response.json()["device_id"]
    response = client.post("/api/v1/auth/challenge", json={"device_id": device_id})
    response.raise_for_status()
    nonce = response.json()["nonce"]
    response = client.post("/api/v1/auth/verify", json={
        "device_id": device_id, "nonce": nonce,
        "signature": base64.b64encode(key.sign(nonce.encode())).decode(),
    })
    response.raise_for_status()
    return {"Authorization": "Bearer " + response.json()["session_token"]}


def test_two_devices_stream_before_upload_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", tmp_path / "data")
    init_db()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DSETTE_DATA_DIR"] = str(config.data_dir)
    command = [sys.executable, "-m", "uvicorn", "dsette_server.app:app", "--host", "127.0.0.1",
               "--port", str(port), "--no-access-log"]
    with (tmp_path / "uvicorn-test.log").open("w+") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get("/api/v1/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError("test server did not start: " + log.read())
                    time.sleep(.05)
                sender = enroll_and_login(client, "sender")
                receiver = enroll_and_login(client, "receiver")
                response = client.put("/api/v1/clipboard", headers=sender, json={"data": "two-device clipboard"})
                response.raise_for_status()
                response = client.get("/api/v1/clipboard/latest", headers=receiver)
                response.raise_for_status()
                assert response.json()["data"] == "two-device clipboard"

                payload = bytes(range(256)) * (1024 * 8)
                expected = hashlib.sha256(payload).hexdigest()
                response = client.post("/api/v1/files/live/announce", headers=sender,
                                       json={"name": "stream.bin", "size": len(payload), "sha256": expected})
                response.raise_for_status()
                tid = response.json()["transfer_id"]
                receiver_has_bytes = threading.Event()
                sender_complete = threading.Event()

                def body():
                    yield payload[:65536]
                    # The sender cannot provide the rest until a receiver has
                    # observed real bytes over TCP. A fully buffered relay fails.
                    assert receiver_has_bytes.wait(8), "receiver got no bytes before sender finished"
                    yield payload[65536:]

                def send():
                    with httpx.Client(base_url=url, timeout=12, trust_env=False) as upload:
                        response = upload.put(f"/api/v1/files/live/{tid}/stream", headers=sender, content=body())
                        response.raise_for_status()
                        sender_complete.set()
                        return response.json()

                with ThreadPoolExecutor(max_workers=1) as pool:
                    sending = pool.submit(send)
                    received = bytearray()
                    with client.stream("GET", f"/api/v1/files/live/{tid}/stream", headers=receiver) as download:
                        download.raise_for_status()
                        assert download.headers["X-Dsette-SHA256"] == expected
                        for block in download.iter_raw():
                            if not received:
                                assert not sender_complete.is_set()
                                receiver_has_bytes.set()
                            received.extend(block)
                    assert sending.result(timeout=15)["status"] == "verified"
                assert len(received) == len(payload)
                assert hashlib.sha256(received).hexdigest() == expected
                response = client.get(f"/api/v1/files/live/{tid}/status", headers=receiver)
                response.raise_for_status()
                assert response.json()["verified"] is True
                assert not list((config.data_dir / "staging").iterdir())
                assert not list((config.data_dir / "inbox").iterdir())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
