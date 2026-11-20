import struct

import pytest

from server_network.fake_backend import FakeBackend
from server_network.protocol import PROTOCOL_VERSION, ConnectionClosed, ErrorCode
from server_network.session import SessionConfig
from tests.harness import Harness


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.finish()


def expect_error(reply, code, fatal=False):
    assert reply["status"] == "error", reply
    assert reply["code"] == code, reply
    assert reply["fatal"] is fatal


# hello / auth / state


def test_hello_ok(h):
    r = h.request(cmd="hello", version=PROTOCOL_VERSION)
    assert r["status"] == "ok" and r["auth_required"] is True


def test_hello_version_mismatch_is_fatal(h):
    expect_error(
        h.request(cmd="hello", version=999), ErrorCode.VERSION_MISMATCH, fatal=True
    )
    with pytest.raises(ConnectionClosed):
        h.recv()


def test_commands_refused_before_login(h):
    for cmd in ({"cmd": "dir"}, {"cmd": "delete", "name": "a"}):
        expect_error(h.request(**cmd), ErrorCode.NOT_AUTHENTICATED)
    # still alive, can still log in
    h.login()


def test_login_wrong_password_then_right(h):
    expect_error(
        h.request(cmd="auth", username="ty", credential="nope"), ErrorCode.AUTH_FAILED
    )
    h.login()


def test_wrong_user_and_wrong_password_look_identical(h):
    a = h.request(cmd="auth", username="ty", credential="bad")
    b = h.request(cmd="auth", username="nobody", credential="bad")
    assert a == b


def test_too_many_failed_logins_closes():
    cfg = SessionConfig(idle_timeout=5, max_failed_logins=3)
    h = Harness(config=cfg)
    for _ in range(2):
        expect_error(
            h.request(cmd="auth", username="ty", credential="x"), ErrorCode.AUTH_FAILED
        )
    expect_error(
        h.request(cmd="auth", username="ty", credential="x"),
        ErrorCode.TOO_MANY_FAILURES,
        fatal=True,
    )
    h.finish()


def test_state_is_per_connection():
    a, b = Harness(), Harness()
    a.login()
    expect_error(b.request(cmd="dir"), ErrorCode.NOT_AUTHENTICATED)
    a.finish()
    b.finish()


def test_overwrite_without_prompt_is_bad_state(h):
    h.login()
    expect_error(h.request(cmd="overwrite", confirm=True), ErrorCode.BAD_STATE)


# validation errors keep the session alive


def test_unknown_and_malformed_requests_are_recoverable(h):
    h.login()
    expect_error(h.request(cmd="format_disk"), ErrorCode.UNKNOWN_COMMAND)
    expect_error(h.request(cmd="delete"), ErrorCode.BAD_REQUEST)
    expect_error(h.request(cmd="delete", name="../x"), ErrorCode.BAD_PATH)
    assert h.request(cmd="dir")["status"] == "ok"


# dir / delete / subfolder


def test_dir_lists_root_and_folder(h):
    h.backend.files["a.txt"] = b"abc"
    h.backend.dirs.add("docs")
    h.backend.files["docs/b.txt"] = b"z"
    h.login()
    r = h.request(cmd="dir")
    assert [e["name"] for e in r["entries"]] == ["a.txt", "docs"]
    assert r["truncated"] is False
    r = h.request(cmd="dir", path="docs")
    assert r["entries"] == [{"name": "b.txt", "type": "file", "size": 1}]


def test_dir_missing_folder(h):
    h.login()
    expect_error(h.request(cmd="dir", path="nope"), ErrorCode.NOT_FOUND)


def test_dir_truncates_huge_listing():
    be = FakeBackend()
    for i in range(2000):
        be.files[f"file_{i:05d}.txt"] = b""
    h = Harness(backend=be, config=SessionConfig(idle_timeout=5, max_dir_entries=100))
    h.login()
    r = h.request(cmd="dir")
    assert r["truncated"] is True and len(r["entries"]) == 100
    h.finish()


def test_delete_outcomes(h):
    h.backend.files["a.txt"] = b"x"
    h.backend.files["busy.txt"] = b"x"
    h.backend.busy.add("busy.txt")
    h.login()
    assert h.request(cmd="delete", name="a.txt")["status"] == "ok"
    assert "a.txt" not in h.backend.files
    expect_error(h.request(cmd="delete", name="a.txt"), ErrorCode.NOT_FOUND)
    expect_error(h.request(cmd="delete", name="busy.txt"), ErrorCode.FILE_BUSY)
    assert h.request(cmd="dir")["status"] == "ok"  # still alive


def test_subfolder_create_and_delete(h):
    h.login()
    assert h.request(cmd="subfolder", action="create", path="d")["status"] == "ok"
    expect_error(
        h.request(cmd="subfolder", action="create", path="d"), ErrorCode.FILE_EXISTS
    )
    h.backend.files["d/x"] = b"1"
    expect_error(
        h.request(cmd="subfolder", action="delete", path="d"), ErrorCode.NOT_EMPTY
    )
    del h.backend.files["d/x"]
    assert h.request(cmd="subfolder", action="delete", path="d")["status"] == "ok"
    expect_error(
        h.request(cmd="subfolder", action="delete", path="d"), ErrorCode.NOT_FOUND
    )


def test_unexpected_backend_bug_is_internal_error_not_crash(h):
    h.login()
    h.backend.list_dir = lambda path: 1 / 0
    expect_error(h.request(cmd="dir"), ErrorCode.INTERNAL_ERROR)
    assert h.request(cmd="delete", name="zzz")["status"] == "error"  # still alive


# goodbye and framing failures


def test_goodbye_closes(h):
    h.login()
    assert h.request(cmd="goodbye")["status"] == "ok"
    with pytest.raises(ConnectionClosed):
        h.recv()


def test_garbage_header_is_fatal(h):
    h.send_raw(struct.pack("!I", 4_000_000_000))
    expect_error(h.recv(), ErrorCode.PROTOCOL_ERROR, fatal=True)
    with pytest.raises(ConnectionClosed):
        h.recv()


def test_bad_json_is_fatal(h):
    h.send_raw(struct.pack("!I", 8) + b"not json")
    expect_error(h.recv(), ErrorCode.PROTOCOL_ERROR, fatal=True)


def test_abrupt_disconnect_ends_thread_cleanly():
    h = Harness()
    h.login()
    h.sock.close()
    h.thread.join(5)
    assert not h.thread.is_alive()


def test_idle_client_is_dropped():
    cfg = SessionConfig(idle_timeout=0.2)
    h = Harness(config=cfg)
    h.thread.join(3)
    assert not h.thread.is_alive()
    h.sock.close()


def test_second_client_unaffected_by_first_misbehaving():
    bad, good = Harness(), Harness()
    bad.send_raw(b"\xff\xff\xff\xff")
    good.login()
    assert good.request(cmd="dir")["status"] == "ok"
    bad.finish()
    good.finish()
