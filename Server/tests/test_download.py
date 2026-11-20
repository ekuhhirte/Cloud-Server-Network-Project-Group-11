import hashlib
import os

import pytest

from server_network.fake_backend import FakeBackend
from server_network.protocol import ConnectionClosed, ErrorCode
from server_network.session import SessionConfig
from tests.harness import Harness
from tests.helpers import wait_until

CHUNK = 1024


def make(backend=None, chunk=CHUNK):
    cfg = SessionConfig(idle_timeout=5, transfer_timeout=5, chunk_size=chunk)
    h = Harness(backend=backend, config=cfg)
    h.login()
    return h


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "size",
    [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 10 * CHUNK + 5],
)
def test_download_hash_matches(size):
    data = os.urandom(size)
    be = FakeBackend()
    be.files["f.bin"] = data
    h = make(be)
    reply = h.request(cmd="download", name="f.bin")
    assert reply["status"] == "ok" and reply["size"] == size
    got = h.recv_bytes(size)
    assert sha(got) == sha(data)
    assert wait_until(lambda: not be.open_downloads)  # read lock released
    h.finish()


def test_connection_stays_in_sync_after_download():
    be = FakeBackend()
    be.files["a"] = os.urandom(2500)
    be.files["b"] = os.urandom(10)
    h = make(be)
    for name in ("a", "b", "a"):
        r = h.request(cmd="download", name=name)
        assert h.recv_bytes(r["size"]) == be.files[name]
    assert h.request(cmd="dir")["status"] == "ok"  # next header parses fine
    h.finish()


def test_download_not_found_and_busy_keep_connection():
    be = FakeBackend()
    be.files["busy.txt"] = b"x"
    be.busy.add("busy.txt")
    h = make(be)
    r = h.request(cmd="download", name="nope.txt")
    assert (r["status"], r["code"]) == ("error", ErrorCode.NOT_FOUND)
    r = h.request(cmd="download", name="busy.txt")
    assert r["code"] == ErrorCode.FILE_BUSY
    assert h.request(cmd="dir")["status"] == "ok"
    assert not be.open_downloads
    h.finish()


def test_download_requires_login():
    h = Harness()
    r = h.request(cmd="download", name="a")
    assert r["code"] == ErrorCode.NOT_AUTHENTICATED
    h.finish()


def test_download_rejects_traversal():
    h = make()
    r = h.request(cmd="download", name="../../etc/passwd")
    assert r["code"] == ErrorCode.BAD_PATH
    h.finish()


def test_client_disconnect_mid_download_releases_lock():
    be = FakeBackend()
    be.files["big.bin"] = os.urandom(6 * 1024 * 1024)
    h = make(be, chunk=64 * 1024)
    r = h.request(cmd="download", name="big.bin")
    h.recv_bytes(1000)  # read a little, then vanish
    assert r["size"] == 6 * 1024 * 1024
    h.sock.close()
    h.thread.join(5)
    assert not h.thread.is_alive()
    assert not be.open_downloads


def test_file_shrinking_mid_download_closes_connection():
    be = FakeBackend()
    be.files["f"] = os.urandom(5000)
    real_read = be.read_chunk
    calls = {"n": 0}

    def flaky(handle, max_bytes):
        calls["n"] += 1
        return real_read(handle, max_bytes) if calls["n"] == 1 else b""

    be.read_chunk = flaky
    h = make(be)
    h.request(cmd="download", name="f")
    h.recv_bytes(CHUNK)
    with pytest.raises(ConnectionClosed):
        h.recv_bytes(10_000)  # server closes; no bogus error reply mixed in
    h.thread.join(5)
    assert not be.open_downloads


def test_backend_bug_mid_download_closes_not_garbles():
    be = FakeBackend()
    be.files["f"] = os.urandom(5000)

    def boom(handle, max_bytes):
        raise RuntimeError("disk exploded")

    be.read_chunk = boom
    h = make(be)
    r = h.request(cmd="download", name="f")
    assert r["status"] == "ok"
    with pytest.raises(ConnectionClosed):
        h.recv_bytes(1)
    h.thread.join(5)
    assert not be.open_downloads


def test_two_clients_download_same_file_concurrently():
    be = FakeBackend()
    data = os.urandom(200_000)
    be.files["shared"] = data
    a, b = make(be), make(be)
    ra = a.request(cmd="download", name="shared")
    rb = b.request(cmd="download", name="shared")
    assert a.recv_bytes(ra["size"]) == data
    assert b.recv_bytes(rb["size"]) == data
    a.finish()
    b.finish()
