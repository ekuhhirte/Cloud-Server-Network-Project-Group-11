import hashlib
import os
import time

import pytest

from server_network.backend_api import DiskFull
from server_network.fake_backend import FakeBackend
from server_network.protocol import ConnectionClosed, ErrorCode, encode_message
from server_network.session import SessionConfig
from tests.harness import Harness

CHUNK = 1024


def make(backend=None, **cfg_kw):
    cfg_kw.setdefault("idle_timeout", 5)
    cfg_kw.setdefault("transfer_timeout", 5)
    cfg_kw.setdefault("chunk_size", CHUNK)
    h = Harness(backend=backend, config=SessionConfig(**cfg_kw))
    h.login()
    return h


def upload(h, name, data):
    """do a full upload from the client side, returns the last reply"""
    r = h.request(cmd="upload", name=name, size=len(data))
    assert r["status"] == "ok" and r["ready"] is True, r
    h.send_raw(data)
    return h.recv()


def sha(b):
    return hashlib.sha256(b).hexdigest()


@pytest.mark.parametrize(
    "size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 10 * CHUNK + 5]
)
def test_new_upload_hash_matches(size):
    be = FakeBackend()
    h = make(be)
    data = os.urandom(size)
    r = upload(h, "f.bin", data)
    assert r["status"] == "ok" and r["name"] == "f.bin" and r["size"] == size
    assert sha(be.files["f.bin"]) == sha(data)
    assert not be.open_uploads
    h.finish()


def test_does_not_over_read_into_next_message():
    # file bytes and the next request arrive together. if the server read past size the
    # dir request would get eaten as file data
    be = FakeBackend()
    h = make(be)
    data = os.urandom(2500)
    r = h.request(cmd="upload", name="f", size=len(data))
    assert r["ready"] is True
    h.send_raw(data + encode_message({"cmd": "dir"}))
    assert h.recv()["status"] == "ok"  # upload result
    assert h.recv()["entries"][0]["name"] == "f"  # dir result
    h.finish()


def test_upload_then_download_round_trip():
    be = FakeBackend()
    h = make(be)
    data = os.urandom(5000)
    upload(h, "r.bin", data)
    r = h.request(cmd="download", name="r.bin")
    assert h.recv_bytes(r["size"]) == data
    h.finish()


# overwrite prompt


def test_existing_file_prompts_then_overwrite_yes():
    be = FakeBackend()
    be.files["a.txt"] = b"old"
    h = make(be)
    r = h.request(cmd="upload", name="a.txt", size=3)
    assert (r["status"], r["code"]) == ("prompt", ErrorCode.FILE_EXISTS)
    assert not be.open_uploads  # no lock held during the prompt
    r = h.request(cmd="overwrite", confirm=True)
    assert r["ready"] is True
    h.send_raw(b"new")
    assert h.recv()["status"] == "ok"
    assert be.files["a.txt"] == b"new"
    h.finish()


def test_existing_file_overwrite_no_keeps_original():
    be = FakeBackend()
    be.files["a.txt"] = b"old"
    h = make(be)
    h.request(cmd="upload", name="a.txt", size=3)
    r = h.request(cmd="overwrite", confirm=False)
    assert r == {"status": "ok", "cancelled": True}
    assert be.files["a.txt"] == b"old"
    assert h.request(cmd="dir")["status"] == "ok"  # back to normal
    h.finish()


def test_other_commands_refused_while_prompt_outstanding():
    be = FakeBackend()
    be.files["a.txt"] = b"old"
    h = make(be)
    h.request(cmd="upload", name="a.txt", size=1)
    assert h.request(cmd="dir")["code"] == ErrorCode.BAD_STATE
    assert h.request(cmd="overwrite", confirm="yes")["code"] == ErrorCode.BAD_REQUEST
    r = h.request(cmd="overwrite", confirm=True)  # still answerable
    assert r["ready"] is True
    h.send_raw(b"x")
    assert h.recv()["status"] == "ok"
    h.finish()


def test_overwrite_hits_busy_file_after_prompt():
    be = FakeBackend()
    be.files["a.txt"] = b"old"
    h = make(be)
    h.request(cmd="upload", name="a.txt", size=1)
    be.busy.add("a.txt")  # someone grabbed it while we sat at the prompt
    r = h.request(cmd="overwrite", confirm=True)
    assert r["code"] == ErrorCode.FILE_BUSY
    assert h.request(cmd="dir")["status"] == "ok"
    h.finish()


def test_disconnect_during_prompt_holds_nothing():
    be = FakeBackend()
    be.files["a.txt"] = b"old"
    h = make(be)
    h.request(cmd="upload", name="a.txt", size=1)
    h.sock.close()
    h.thread.join(5)
    assert not h.thread.is_alive() and not be.open_uploads


# refusals before any bytes move


def test_busy_too_large_diskfull_and_bad_path_keep_connection():
    be = FakeBackend()
    be.busy.add("busy.txt")
    be.disk_limit = 100
    h = make(be, max_upload_size=1000)
    assert h.request(cmd="upload", name="busy.txt", size=1)["code"] == (
        ErrorCode.FILE_BUSY
    )
    assert h.request(cmd="upload", name="big", size=5000)["code"] == ErrorCode.TOO_LARGE
    assert h.request(cmd="upload", name="x", size=500)["code"] == ErrorCode.DISK_FULL
    assert h.request(cmd="upload", name="../x", size=1)["code"] == ErrorCode.BAD_PATH
    assert h.request(cmd="upload", name="x", size=-1)["code"] == ErrorCode.BAD_REQUEST
    assert h.request(cmd="dir")["status"] == "ok"
    assert not be.open_uploads
    h.finish()


def test_upload_requires_login():
    h = Harness()
    assert h.request(cmd="upload", name="a", size=1)["code"] == (
        ErrorCode.NOT_AUTHENTICATED
    )
    h.finish()


# failures mid-stream: nothing may leak


def test_client_disconnects_mid_upload_aborts_and_publishes_nothing():
    be = FakeBackend()
    h = make(be)
    h.request(cmd="upload", name="half.bin", size=10_000)
    h.send_raw(os.urandom(3000))
    h.sock.close()
    h.thread.join(5)
    assert not h.thread.is_alive()
    assert not be.open_uploads
    assert "half.bin" not in be.files
    assert ("abort_upload", ("half.bin",)) in be.calls


def test_client_disconnects_right_after_request():
    be = FakeBackend()
    h = make(be)
    h.send(cmd="upload", name="x", size=5000)
    h.sock.close()
    h.thread.join(5)
    assert not h.thread.is_alive() and not be.open_uploads


def test_client_stalls_mid_upload_times_out():
    be = FakeBackend()
    h = make(be, transfer_timeout=0.3)
    h.request(cmd="upload", name="slow", size=5000)
    h.send_raw(b"abc")  # ...then silence
    start = time.monotonic()
    h.thread.join(5)
    assert not h.thread.is_alive()
    assert time.monotonic() - start < 3
    assert not be.open_uploads and "slow" not in be.files
    h.sock.close()


def test_disk_full_mid_stream_errors_closes_and_aborts():
    be = FakeBackend()
    be.disk_limit = 2000
    h = make(be)
    r = h.request(cmd="upload", name="f", size=1500)  # fits at begin
    assert r["ready"] is True
    be.files["other"] = b"x" * 1500  # disk fills up while we upload
    h.send_raw(os.urandom(1500))
    err = h.recv()
    assert err["code"] == ErrorCode.DISK_FULL and err["fatal"] is True
    with pytest.raises(ConnectionClosed):
        h.recv()
    h.thread.join(5)
    assert not be.open_uploads and "f" not in be.files


def test_backend_bug_mid_stream_closes_and_aborts():
    be = FakeBackend()
    be.write_error = RuntimeError("boom")
    h = make(be)
    h.request(cmd="upload", name="f", size=3000)
    h.send_raw(os.urandom(1500))
    err = h.recv()
    assert err["code"] == ErrorCode.INTERNAL_ERROR and err["fatal"] is True
    h.thread.join(5)
    assert not be.open_uploads


def test_failed_upload_does_not_block_a_retry():
    be = FakeBackend()
    h1 = make(be)
    h1.request(cmd="upload", name="f.bin", size=4000)
    h1.send_raw(os.urandom(100))
    h1.sock.close()
    h1.thread.join(5)
    # a new client can upload the same name right away
    h2 = make(be)
    data = os.urandom(4000)
    assert upload(h2, "f.bin", data)["status"] == "ok"
    assert be.files["f.bin"] == data
    h2.finish()


def test_finish_failure_is_recoverable_error():
    be = FakeBackend()

    def bad_finish(handle):
        raise DiskFull("rename failed")

    be.finish_upload = bad_finish
    h = make(be)
    h.request(cmd="upload", name="f", size=3)
    h.send_raw(b"abc")
    r = h.recv()
    assert r["code"] == ErrorCode.DISK_FULL and r["fatal"] is False
    assert h.request(cmd="dir")["status"] == "ok"  # stream was in sync
    assert not be.open_uploads
    h.finish()


def test_concurrent_uploads_from_two_clients():
    be = FakeBackend()
    a, b = make(be), make(be)
    da, db = os.urandom(50_000), os.urandom(70_000)
    ra = a.request(cmd="upload", name="a.bin", size=len(da))
    rb = b.request(cmd="upload", name="b.bin", size=len(db))
    assert ra["ready"] and rb["ready"]
    a.send_raw(da)
    b.send_raw(db)
    assert a.recv()["status"] == "ok" and b.recv()["status"] == "ok"
    assert be.files["a.bin"] == da and be.files["b.bin"] == db
    a.finish()
    b.finish()
