import asyncio
import hashlib

import pytest

@pytest.fixture(autouse=True)
def isolated_relay_storage(tmp_path,monkeypatch):
    from dsette_server.config import config
    monkeypatch.setattr(config,"data_dir",tmp_path)
from fastapi import HTTPException

from dsette_server import live


class Request:
    def __init__(self, data): self.data = data
    async def stream(self):
        yield self.data


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    live.relays.clear()
    monkeypatch.setattr(live, "audit", lambda *args: None)
    yield
    live.relays.clear()


def test_small_file_waits_for_receiver_and_verifies():
    async def run():
        data = b"small live file"
        x = live.LiveAnnounce(name="test.txt", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        tid = (await live.announce(x, {"id": "sender"}))["transfer_id"]
        task = asyncio.create_task(live.send(tid, Request(data), {"id": "sender"}))
        await asyncio.sleep(.01)
        assert not task.done()
        response = await live.receive(tid, {"id": "receiver"})
        output = b"".join([block async for block in response.body_iterator])
        assert output == data
        assert (await task)["status"] == "verified"
    asyncio.run(run())


def test_queue_is_byte_bounded_and_backpressured():
    async def run():
        data = b"x" * live.BLOCK_SIZE * (live.QUEUE_BLOCKS + 3)
        r = live.Relay("sender", "large", len(data), hashlib.sha256(data).hexdigest())
        r.receiver_ready.set()
        live.relays["test"] = r
        task = asyncio.create_task(live.send("test", Request(data), {"id": "sender"}))
        await asyncio.sleep(.03)
        assert not task.done()
        assert r.queue.qsize() == live.QUEUE_BLOCKS
        assert all(len(block) <= live.BLOCK_SIZE for block in r.queue._queue)
        output = b"".join([block async for block in live.receive_blocks(r)])
        assert output == data
        assert (await task)["status"] == "verified"
    asyncio.run(run())


def test_duplicate_sender_rejected_and_disconnect_unblocks_sender():
    async def run():
        data = b"x" * live.BLOCK_SIZE * (live.QUEUE_BLOCKS + 3)
        r = live.Relay("sender", "large", len(data), hashlib.sha256(data).hexdigest())
        r.receiver_ready.set()
        live.relays["test"] = r
        task = asyncio.create_task(live.send("test", Request(data), {"id": "sender"}))
        await asyncio.sleep(.02)
        with pytest.raises(HTTPException) as exc:
            await live.send("test", Request(data), {"id": "sender"})
        assert exc.value.status_code == 409
        r.aborted.set()
        with pytest.raises(HTTPException):
            await asyncio.wait_for(task, .5)
        assert r.finished.is_set()
        assert r.verified is False
    asyncio.run(run())


def test_tampered_stream_and_size_mismatch_rejected():
    async def run():
        for size, expected in ((4, "0" * 64), (8, hashlib.sha256(b"test").hexdigest())):
            r = live.Relay("sender", "test", size, expected)
            r.receiver_ready.set()
            live.relays["test"] = r
            result = await live.send("test", Request(b"test"), {"id": "sender"})
            assert result["status"] == "hash_mismatch"
    asyncio.run(run())


def test_missing_peer_times_out(monkeypatch):
    monkeypatch.setattr(live, "IDLE_SECONDS", .01)
    async def run():
        r = live.Relay("sender", "test", 0, hashlib.sha256(b"").hexdigest())
        live.relays["test"] = r
        with pytest.raises(HTTPException) as exc:
            await live.send("test", Request(b""), {"id": "sender"})
        assert exc.value.status_code == 408
        assert r.aborted.is_set()
    asyncio.run(run())
