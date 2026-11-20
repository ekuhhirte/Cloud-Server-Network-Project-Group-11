import os
import threading
import time

from server_network.fake_backend import FakeBackend
from server_network.metrics import TransferRecord
from server_network.session import SessionConfig
from tests.harness import Harness
from tests.helpers import wait_until

CHUNK = 1024


class Collector:
    """list of records that works as an on_transfer callback (has a lock)"""

    def __init__(self):
        self.records = []
        self._lock = threading.Lock()

    def __call__(self, record):
        with self._lock:
            self.records.append(record)


def make(backend=None, on_transfer=None, **kw):
    cfg = SessionConfig(idle_timeout=5, transfer_timeout=5, chunk_size=CHUNK)
    h = Harness(backend=backend, config=cfg, on_transfer=on_transfer, **kw)
    h.login()
    return h


def test_rate_math():
    r = TransferRecord("download", "f", 10_000_000, 10_000_000, 2.0, "ok")
    assert r.rate_mb_s == 5.0
    assert TransferRecord("download", "f", 5, 5, 0.0, "ok").rate_mb_s == 0.0
    d = r.to_dict()
    assert d["rate_mb_s"] == 5.0 and d["operation"] == "download"


def test_download_record():
    be = FakeBackend()
    be.files["f.bin"] = os.urandom(5000)
    c = Collector()
    h = make(be, c)
    r = h.request(cmd="download", name="f.bin")
    h.recv_bytes(r["size"])
    assert wait_until(lambda: len(c.records) == 1)
    rec = c.records[0]
    assert (rec.operation, rec.name, rec.outcome) == ("download", "f.bin", "ok")
    assert rec.size_bytes == rec.bytes_transferred == 5000
    assert rec.duration_s >= 0 and rec.username == "ty"
    h.finish()


def test_upload_record():
    be = FakeBackend()
    c = Collector()
    h = make(be, c)
    data = os.urandom(4321)
    h.request(cmd="upload", name="u.bin", size=len(data))
    h.send_raw(data)
    assert h.recv()["status"] == "ok"
    assert wait_until(lambda: len(c.records) == 1)
    rec = c.records[0]
    assert (rec.operation, rec.outcome) == ("upload", "ok")
    assert rec.bytes_transferred == 4321
    h.finish()


def test_aborted_upload_is_recorded_with_partial_bytes():
    c = Collector()
    h = make(FakeBackend(), c)
    h.request(cmd="upload", name="x", size=10_000)
    h.send_raw(os.urandom(2048))
    h.sock.close()
    h.thread.join(5)
    assert wait_until(lambda: len(c.records) == 1)
    rec = c.records[0]
    assert rec.outcome == "aborted" and 0 < rec.bytes_transferred < 10_000


def test_aborted_download_is_recorded():
    be = FakeBackend()
    be.files["big"] = os.urandom(6 * 1024 * 1024)
    c = Collector()
    h = make(be, c)
    h.request(cmd="download", name="big")
    h.recv_bytes(100)
    h.sock.close()
    h.thread.join(5)
    assert wait_until(lambda: len(c.records) == 1)
    assert c.records[0].outcome == "aborted"
    assert c.records[0].bytes_transferred < 6 * 1024 * 1024


def test_refused_transfers_produce_no_record():
    c = Collector()
    h = make(FakeBackend(), c)
    h.request(cmd="download", name="missing")
    h.request(cmd="upload", name="../x", size=1)
    h.request(cmd="dir")
    time.sleep(0.1)
    assert c.records == []
    h.finish()


def test_crashing_callback_does_not_break_transfer():
    def boom(record):
        raise RuntimeError("analysis is broken")

    be = FakeBackend()
    be.files["f"] = b"hello"
    h = make(be, boom)
    r = h.request(cmd="download", name="f")
    assert h.recv_bytes(r["size"]) == b"hello"
    assert h.request(cmd="dir")["status"] == "ok"  # session still healthy
    h.finish()


def test_slow_callback_does_not_inflate_measured_duration():
    got = []

    def slow(record):
        got.append(record)
        time.sleep(0.4)  # e.g. a slow database insert

    be = FakeBackend()
    be.files["f"] = os.urandom(3000)
    h = make(be, slow)
    r = h.request(cmd="download", name="f")
    h.recv_bytes(r["size"])
    assert wait_until(lambda: len(got) == 1)
    assert got[0].duration_s < 0.3  # callback ran after the timed region
    h.finish()


def test_concurrent_clients_probe():
    be = FakeBackend()
    be.files["f"] = b"abc"
    c = Collector()
    h = make(be, c, active_clients=lambda: 3)
    r = h.request(cmd="download", name="f")
    h.recv_bytes(r["size"])
    assert wait_until(lambda: len(c.records) == 1)
    assert c.records[0].concurrent_clients == 3
    h.finish()


def test_broken_probe_is_ignored():
    def bad():
        raise RuntimeError("x")

    be = FakeBackend()
    be.files["f"] = b"abc"
    c = Collector()
    h = make(be, c, active_clients=bad)
    r = h.request(cmd="download", name="f")
    h.recv_bytes(r["size"])
    assert wait_until(lambda: len(c.records) == 1)
    assert c.records[0].concurrent_clients is None
    h.finish()
