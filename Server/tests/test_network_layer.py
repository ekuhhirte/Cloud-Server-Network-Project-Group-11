"""Tests for network_layer.py.

Each test starts serve_client in a thread and talks to it over a socket pair,
the same way a real client would: write a header line, read a header line, and
read file bytes from the same buffered reader.
"""

import hashlib
import io
import json
import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest

import network_layer
from network_layer import BadRequest, check_path
from tests.fake_storage import FakeStorage

CHUNK = 1024  # small chunks so even small files take several reads


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(network_layer, "CHUNK", CHUNK)




def wait_until(check, timeout=3):
    """Keep checking until check() is true. The server finishes its cleanup
    just after the client has seen the last byte."""
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    return check()


class Conn:
    """A fake client connected to serve_client through a socket pair."""

    def __init__(self, storage=None, record=None):
        self.storage = storage or FakeStorage()
        self.sock, server_sock = socket.socketpair()
        self.sock.settimeout(5)
        self.reader = self.sock.makefile("rb")
        self.thread = threading.Thread(
            target=network_layer.serve_client,
            args=(server_sock, ("test", 0), self.storage, record),
            daemon=True,
        )
        self.thread.start()

    def send(self, header):
        self.sock.sendall((json.dumps(header) + "\n").encode("utf-8"))

    def read(self):
        return json.loads(self.reader.readline())

    def ask(self, header):
        self.send(header)
        return self.read()

    def login(self, user="ty", credential="pw"):
        reply = self.ask({"type": "Auth Request", "username": user, "credential": credential})
        assert reply["type"] == "Auth Response", reply
        return reply

    def closed_by_server(self):
        return self.reader.readline() == b""

    def disconnect(self):
        """Hang up. The reader has to be closed too, or the socket stays open."""
        self.reader.close()
        self.sock.close()

    def finish(self):
        self.disconnect()
        self.thread.join(5)
        assert not self.thread.is_alive(), "server thread did not end"


@pytest.fixture
def conn():
    c = Conn()
    yield c
    c.finish()


def logged_in(storage=None, record=None):
    c = Conn(storage, record)
    c.login()
    return c


def sha(data):
    return hashlib.sha256(data).hexdigest()


def is_error(reply, text):
    return reply == {"type": "error", "error": text}


# login, goodbye and bad input


def test_login_works(conn):
    assert conn.login() == {"type": "Auth Response", "username": "ty"}


def test_wrong_password_then_right_password(conn):
    reply = conn.ask({"type": "Auth Request", "username": "ty", "credential": "nope"})
    assert is_error(reply, "Invalid Login")
    conn.login()


def test_wrong_user_and_wrong_password_look_the_same(conn):
    a = conn.ask({"type": "Auth Request", "username": "ty", "credential": "bad"})
    b = conn.ask({"type": "Auth Request", "username": "nobody", "credential": "bad"})
    assert a == b


def test_too_many_failed_logins_closes(conn):
    for _ in range(network_layer.MAX_LOGIN_TRIES - 1):
        reply = conn.ask({"type": "Auth Request", "username": "ty", "credential": "x"})
        assert is_error(reply, "Invalid Login")
    reply = conn.ask({"type": "Auth Request", "username": "ty", "credential": "x"})
    assert is_error(reply, "Too Many Failed Logins")
    assert conn.closed_by_server()


def test_commands_are_refused_before_login(conn):
    assert is_error(conn.ask({"type": "Delete Request", "file_path": "a"}), "Not Authenticated")
    conn.login()  # and the connection still works


def test_login_needs_both_fields(conn):
    assert is_error(conn.ask({"type": "Auth Request", "username": "ty"}), "Malformed Header")


def test_each_connection_has_its_own_login():
    a, b = Conn(), Conn()
    a.login()
    assert is_error(b.ask({"type": "Delete Request", "file_path": "x"}), "Not Authenticated")
    a.finish()
    b.finish()


def test_goodbye_closes_the_connection(conn):
    conn.login()
    assert conn.ask({"type": "Goodbye Request"}) == {"type": "Goodbye Response"}
    assert conn.closed_by_server()


def test_unknown_type_is_an_error_but_not_fatal(conn):
    conn.login()
    assert is_error(conn.ask({"type": "Format Disk"}), "Unknown Command")
    assert is_error(conn.ask({}), "Unknown Command")  # no type at all
    assert is_error(conn.ask({"type": 5}), "Unknown Command")
    conn.login()


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2, 3]\n", b"5\n", b'"hi"\n', b"\xff\xfe\n"])
def test_unusable_lines_get_an_error_and_the_connection_continues(conn, line):
    conn.sock.sendall(line)
    assert is_error(conn.read(), "Malformed Header")
    conn.login()


def test_header_that_never_ends_closes_the_connection(conn):
    conn.sock.sendall(b"A" * (network_layer.MAX_HEADER + 10))
    assert is_error(conn.read(), "Header Too Long")
    assert conn.closed_by_server()


def test_a_long_line_does_not_turn_into_a_second_command(conn):
    # the old readline(4096) cut long lines in two and ran the second half
    conn.login()
    junk = b'{"type": "Delete Request", "file_path": "' + b"x" * 70_000
    conn.sock.sendall(junk + b'"}\n{"type": "Goodbye Request"}\n')
    assert is_error(conn.read(), "Header Too Long")
    assert conn.closed_by_server()


def test_client_leaving_ends_the_thread():
    c = logged_in()
    c.disconnect()
    c.thread.join(5)
    assert not c.thread.is_alive()


def test_quiet_client_is_dropped(monkeypatch):
    monkeypatch.setattr(network_layer, "TIMEOUT_IDLE", 0.3)
    c = Conn()
    assert c.closed_by_server()
    c.finish()


# paths

ATTACKS = [
    "../x", "a/../../x", "..", ".", "/etc/passwd", "C:\\x", "C:/x", "..\\x",
    "a\\b", "a//b", "a/", "a/./b", "bad\x00name", "tab\tname", "x" * 1100,
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_bad_paths_are_refused(attack):
    with pytest.raises(BadRequest, match="Bad Path"):
        check_path(attack)


def test_good_paths_pass():
    for good in ["a.txt", "dir/sub/file.mp4", "r\u00e9sum\u00e9.txt", "a b/c d"]:
        assert check_path(good) == good


def test_empty_path_only_when_allowed():
    assert check_path("", allow_empty=True) == ""
    with pytest.raises(BadRequest):
        check_path("")


def test_path_must_be_text():
    for bad in (5, None, ["a"], {"a": 1}):
        with pytest.raises(BadRequest, match="Malformed Header"):
            check_path(bad)


# dir, delete and subfolder


def test_dir_lists_the_root_and_a_folder():
    s = FakeStorage()
    s.files["a.txt"] = b"abc"
    s.dirs.add("docs")
    s.files["docs/b.txt"] = b"z"
    c = logged_in(s)
    reply = c.ask({"type": "Dir Request"})
    assert reply["type"] == "Dir Response"
    assert [e["name"] for e in reply["entries"]] == ["a.txt", "docs"]
    reply = c.ask({"type": "Dir Request", "dir_path": "docs"})
    assert reply["entries"] == [{"name": "b.txt", "type": "file", "size": 1}]
    c.finish()


def test_dir_of_missing_folder(conn):
    conn.login()
    assert is_error(conn.ask({"type": "Dir Request", "dir_path": "nope"}), "File Not Found")


def test_huge_listing_is_cut_short(monkeypatch):
    monkeypatch.setattr(network_layer, "MAX_DIR_ENTRIES", 5)
    s = FakeStorage()
    for i in range(20):
        s.files[f"f{i:02}.txt"] = b""
    c = logged_in(s)
    reply = c.ask({"type": "Dir Request"})
    assert len(reply["entries"]) == 5 and reply["truncated"] is True
    c.finish()


def test_delete_a_file_and_the_ways_it_can_fail():
    s = FakeStorage()
    s.files["a.txt"] = b"x"
    s.files["busy.txt"] = b"x"
    s.busy.add("busy.txt")
    c = logged_in(s)
    assert c.ask({"type": "Delete Request", "file_path": "a.txt"})["type"] == "Delete Response"
    assert "a.txt" not in s.files
    assert is_error(c.ask({"type": "Delete Request", "file_path": "a.txt"}), "File Not Found")
    assert is_error(c.ask({"type": "Delete Request", "file_path": "busy.txt"}), "File Is Busy")
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"  # still fine
    c.finish()


def test_subfolders_create_and_delete():
    s = FakeStorage()
    c = logged_in(s)

    def sub(action, path):
        return c.ask({"type": "Subfolder Request", "action": action, "dir_path": path})

    assert sub("create", "d")["type"] == "Subfolder Response"
    assert is_error(sub("create", "d"), "Already Exists")
    assert is_error(sub("create", "nope/child"), "File Not Found")
    s.files["d/x"] = b"1"
    assert is_error(sub("delete", "d"), "Folder Not Empty")
    del s.files["d/x"]
    assert sub("delete", "d")["type"] == "Subfolder Response"
    assert is_error(sub("delete", "d"), "File Not Found")
    c.finish()


def test_subfolder_needs_a_real_action(conn):
    conn.login()
    reply = conn.ask({"type": "Subfolder Request", "action": "rename", "dir_path": "x"})
    assert is_error(reply, "Malformed Header")
    reply = conn.ask({"type": "Subfolder Request", "dir_path": "x"})
    assert is_error(reply, "Malformed Header")


@pytest.mark.parametrize(
    "header",
    [
        {"type": "Delete Request", "file_path": "../x"},
        {"type": "Delete Request", "file_path": "/etc/passwd"},
        {"type": "Dir Request", "dir_path": "../.."},
        {"type": "Subfolder Request", "action": "create", "dir_path": "a/../../x"},
    ],
)
def test_bad_paths_are_refused_by_every_command(conn, header):
    conn.login()
    assert is_error(conn.ask(header), "Bad Path")
    assert conn.ask({"type": "Dir Request"})["type"] == "Dir Response"


def test_missing_fields_are_refused(conn):
    conn.login()
    assert is_error(conn.ask({"type": "Delete Request"}), "Malformed Header")
    assert is_error(conn.ask({"type": "Dir Request", "dir_path": 5}), "Malformed Header")


def test_a_backend_bug_is_an_error_not_a_crash():
    s = FakeStorage()

    def boom(path):
        raise ZeroDivisionError("bug")

    s.list_dir = boom
    c = logged_in(s)
    assert is_error(c.ask({"type": "Dir Request"}), "Internal Server Error")
    assert is_error(c.ask({"type": "Delete Request", "file_path": "zzz"}), "File Not Found")
    c.finish()


# download


def download(c, path):
    """Ask for a file. Returns the header, and the file bytes if it was ok."""
    reply = c.ask({"type": "Download Request", "file_path": path})
    if reply["type"] != "Download Response":
        return reply, None
    return reply, c.reader.read(reply["file_size"])


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 10 * CHUNK + 5])
def test_download_gives_back_exactly_the_same_bytes(size):
    data = os.urandom(size)
    s = FakeStorage()
    s.files["f.bin"] = data
    c = logged_in(s)
    reply, got = download(c, "f.bin")
    assert reply["file_size"] == size and reply["filename"] == "f.bin"
    assert sha(got) == sha(data)
    assert wait_until(lambda: s.open_files == 0)  # the read lock was released
    c.finish()


def test_filename_in_the_reply_is_just_the_name():
    s = FakeStorage()
    s.files["docs/deep/a.txt"] = b"hi"
    c = logged_in(s)
    reply, got = download(c, "docs/deep/a.txt")
    assert reply["filename"] == "a.txt" and reply["file_path"] == "docs/deep/a.txt"
    c.finish()


def test_connection_stays_in_sync_after_downloads():
    s = FakeStorage()
    s.files["a"] = os.urandom(2500)
    s.files["b"] = os.urandom(10)
    c = logged_in(s)
    for name in ("a", "b", "a"):
        reply, got = download(c, name)
        assert got == s.files[name]
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"
    c.finish()


def test_download_of_missing_or_busy_file_is_an_error():
    s = FakeStorage()
    s.files["busy.txt"] = b"x"
    s.busy.add("busy.txt")
    c = logged_in(s)
    assert is_error(download(c, "nope.txt")[0], "File Not Found")
    assert is_error(download(c, "busy.txt")[0], "File Is Busy")
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"
    assert s.open_files == 0
    c.finish()


def test_download_needs_login(conn):
    assert is_error(conn.ask({"type": "Download Request", "file_path": "a"}), "Not Authenticated")


def test_download_refuses_traversal(conn):
    conn.login()
    reply = conn.ask({"type": "Download Request", "file_path": "../../etc/passwd"})
    assert is_error(reply, "Bad Path")


def test_client_leaving_during_a_download_releases_the_lock():
    s = FakeStorage()
    s.files["big.bin"] = os.urandom(6 * 1024 * 1024)
    c = logged_in(s)
    reply = c.ask({"type": "Download Request", "file_path": "big.bin"})
    c.reader.read(1000)  # read a little, then vanish
    c.disconnect()
    c.thread.join(5)
    assert not c.thread.is_alive()
    assert s.open_files == 0


def test_file_getting_shorter_closes_the_connection():
    s = FakeStorage()
    s.files["f"] = os.urandom(5000)
    real_open = s.download

    @contextmanager
    def shrinking(path):
        with real_open(path) as (f, size):
            yield io.BytesIO(f.read(1500)), size  # promises 5000, only has 1500

    s.download = shrinking
    c = logged_in(s)
    reply = c.ask({"type": "Download Request", "file_path": "f"})
    assert len(c.reader.read(10_000)) == 1500  # no error message mixed into the file
    assert c.closed_by_server()
    assert wait_until(lambda: s.open_files == 0)


def test_backend_bug_in_the_middle_closes_the_connection():
    s = FakeStorage()

    class Broken:
        def read(self, n):
            raise RuntimeError("disk exploded")

    @contextmanager
    def broken(path):
        yield Broken(), 5000

    s.download = broken
    c = logged_in(s)
    assert c.ask({"type": "Download Request", "file_path": "f"})["type"] == "Download Response"
    assert c.closed_by_server()


def test_two_clients_download_the_same_file_at_once():
    s = FakeStorage()
    data = os.urandom(200_000)
    s.files["shared"] = data
    a, b = logged_in(s), logged_in(s)
    ra = a.ask({"type": "Download Request", "file_path": "shared"})
    rb = b.ask({"type": "Download Request", "file_path": "shared"})
    assert a.reader.read(ra["file_size"]) == data
    assert b.reader.read(rb["file_size"]) == data
    a.finish()
    b.finish()


# upload


def upload(c, path, data):
    """Do a whole upload from the client side and return the last reply."""
    reply = c.ask({"type": "Upload Request", "file_path": path, "file_size": len(data)})
    assert reply["type"] == "Upload Ready", reply
    c.sock.sendall(data)
    return c.read()


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 10 * CHUNK + 5])
def test_upload_saves_exactly_the_same_bytes(size):
    s = FakeStorage()
    c = logged_in(s)
    data = os.urandom(size)
    reply = upload(c, "f.bin", data)
    assert reply == {"type": "Upload Complete", "file_path": "f.bin", "file_size": size}
    assert sha(s.files["f.bin"]) == sha(data)
    assert wait_until(lambda: s.open_files == 0)  # the write lock was released
    c.finish()


def test_upload_does_not_read_into_the_next_request():
    # the file bytes and the next request arrive together. If the server read
    # past the file size, the Dir Request would get eaten as file data.
    s = FakeStorage()
    c = logged_in(s)
    data = os.urandom(2500)
    c.send({"type": "Upload Request", "file_path": "f", "file_size": len(data)})
    assert c.read()["type"] == "Upload Ready"
    c.sock.sendall(data + b'{"type": "Dir Request"}\n')
    assert c.read()["type"] == "Upload Complete"
    assert c.read()["entries"][0]["name"] == "f"
    c.finish()


def test_header_and_file_bytes_in_one_packet():
    # a client that does not wait for Upload Ready still works for a new file
    s = FakeStorage()
    c = logged_in(s)
    data = os.urandom(3000)
    header = {"type": "Upload Request", "file_path": "f", "file_size": len(data)}
    c.sock.sendall((json.dumps(header) + "\n").encode() + data)
    assert c.read()["type"] == "Upload Ready"
    assert c.read()["type"] == "Upload Complete"
    assert s.files["f"] == data
    c.finish()


def test_upload_then_download_round_trip():
    s = FakeStorage()
    c = logged_in(s)
    data = os.urandom(5000)
    upload(c, "r.bin", data)
    reply, got = download(c, "r.bin")
    assert got == data
    c.finish()


# the overwrite prompt


def existing_file_storage():
    s = FakeStorage()
    s.files["a.txt"] = b"old"
    return s


def start_upload_of_existing_file(c, size=3):
    return c.ask({"type": "Upload Request", "file_path": "a.txt", "file_size": size})


def test_existing_file_asks_before_overwriting_and_yes_replaces_it():
    s = existing_file_storage()
    c = logged_in(s)
    prompt = start_upload_of_existing_file(c)
    assert prompt["type"] == "Overwrite Prompt" and prompt["file_path"] == "a.txt"
    assert s.open_files == 0  # no lock is held while the client decides
    assert c.ask({"type": "Overwrite Reply", "confirm": True})["type"] == "Upload Ready"
    c.sock.sendall(b"new")
    assert c.read()["type"] == "Upload Complete"
    assert s.files["a.txt"] == b"new"
    c.finish()


def test_saying_no_keeps_the_old_file():
    s = existing_file_storage()
    c = logged_in(s)
    start_upload_of_existing_file(c)
    assert c.ask({"type": "Overwrite Reply", "confirm": False})["type"] == "Upload Cancelled"
    assert s.files["a.txt"] == b"old"
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"  # back to normal
    c.finish()


def test_anything_but_true_counts_as_no():
    s = existing_file_storage()
    c = logged_in(s)
    start_upload_of_existing_file(c)
    assert c.ask({"type": "Overwrite Reply", "confirm": "yes"})["type"] == "Upload Cancelled"
    assert s.files["a.txt"] == b"old"
    c.finish()


def test_answering_the_prompt_with_something_else_closes_the_connection():
    # a client that ignores the prompt might already be sending file bytes
    s = existing_file_storage()
    c = logged_in(s)
    start_upload_of_existing_file(c)
    c.send({"type": "Dir Request"})
    assert is_error(c.read(), "Expected Overwrite Reply")
    assert c.closed_by_server()
    assert s.files["a.txt"] == b"old"


def test_file_that_becomes_busy_while_the_prompt_is_open():
    s = existing_file_storage()
    c = logged_in(s)
    start_upload_of_existing_file(c)
    s.busy.add("a.txt")  # somebody grabbed it while we waited
    assert is_error(c.ask({"type": "Overwrite Reply", "confirm": True}), "File Is Busy")
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"
    c.finish()


def test_leaving_at_the_prompt_holds_nothing():
    s = existing_file_storage()
    c = logged_in(s)
    start_upload_of_existing_file(c)
    c.disconnect()
    c.thread.join(5)
    assert not c.thread.is_alive() and s.open_files == 0


# refused before any file bytes move


def test_uploads_that_are_refused_leave_the_connection_open():
    s = FakeStorage()
    s.busy.add("busy.txt")
    s.disk_limit = 100
    c = logged_in(s)

    def up(path, size):
        return c.ask({"type": "Upload Request", "file_path": path, "file_size": size})

    assert is_error(up("busy.txt", 1), "File Is Busy")
    assert is_error(up("../x", 1), "Bad Path")
    assert is_error(up("x", -1), "Malformed Header")
    assert is_error(up("x", "12"), "Malformed Header")
    assert is_error(up("x", True), "Malformed Header")
    assert is_error(up("x", 1.5), "Malformed Header")
    assert is_error(up("x", network_layer.MAX_UPLOAD + 1), "File Too Large")
    assert is_error(c.ask({"type": "Upload Request", "file_size": 1}), "Malformed Header")
    assert is_error(c.ask({"type": "Upload Request", "file_path": "x"}), "Malformed Header")
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"
    assert s.open_files == 0
    c.finish()


def test_upload_needs_login(conn):
    reply = conn.ask({"type": "Upload Request", "file_path": "a", "file_size": 1})
    assert is_error(reply, "Not Authenticated")


# things going wrong in the middle of the file


def test_client_leaving_in_the_middle_saves_nothing():
    s = FakeStorage()
    c = logged_in(s)
    c.ask({"type": "Upload Request", "file_path": "half.bin", "file_size": 10_000})
    c.sock.sendall(os.urandom(3000))
    c.disconnect()
    c.thread.join(5)
    assert not c.thread.is_alive()
    assert s.open_files == 0 and "half.bin" not in s.files


def test_client_that_stops_sending_times_out(monkeypatch):
    monkeypatch.setattr(network_layer, "TIMEOUT_TRANSFER", 0.3)
    s = FakeStorage()
    c = logged_in(s)
    c.ask({"type": "Upload Request", "file_path": "slow", "file_size": 5000})
    c.sock.sendall(b"abc")  # ...then silence
    start = time.time()
    c.thread.join(5)
    assert not c.thread.is_alive() and time.time() - start < 3
    assert s.open_files == 0 and "slow" not in s.files
    c.disconnect()


def test_disk_filling_up_in_the_middle_tells_the_client_and_closes():
    s = FakeStorage()
    s.disk_limit = 2000
    c = logged_in(s)
    assert c.ask({"type": "Upload Request", "file_path": "f", "file_size": 1500})["type"] == "Upload Ready"
    s.files["other"] = b"x" * 1500  # the disk fills up while we upload
    c.sock.sendall(os.urandom(1500))
    assert is_error(c.read(), "Disk Full")
    assert c.closed_by_server()
    c.thread.join(5)
    assert s.open_files == 0 and "f" not in s.files


def test_backend_bug_while_writing_closes_the_connection():
    class Broken:
        def write(self, chunk):
            raise RuntimeError("boom")

    @contextmanager
    def broken(path):
        yield Broken()

    s = FakeStorage()
    s.upload = broken
    c = logged_in(s)
    c.ask({"type": "Upload Request", "file_path": "f", "file_size": 3000})
    c.sock.sendall(os.urandom(1500))
    assert is_error(c.read(), "Internal Server Error")
    assert c.closed_by_server()


def test_failed_upload_does_not_block_a_retry():
    s = FakeStorage()
    c1 = logged_in(s)
    c1.ask({"type": "Upload Request", "file_path": "f.bin", "file_size": 4000})
    c1.sock.sendall(os.urandom(100))
    c1.disconnect()
    c1.thread.join(5)
    c2 = logged_in(s)
    data = os.urandom(4000)
    assert upload(c2, "f.bin", data)["type"] == "Upload Complete"
    assert s.files["f.bin"] == data
    c2.finish()


def test_failing_to_save_at_the_end_is_an_error_but_the_connection_lives():
    s = FakeStorage()
    s.commit_error = OSError(28, "disk full while saving")
    c = logged_in(s)
    c.ask({"type": "Upload Request", "file_path": "f", "file_size": 3})
    c.sock.sendall(b"abc")
    assert is_error(c.read(), "Disk Full")
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"  # still in sync
    assert s.open_files == 0 and "f" not in s.files
    c.finish()


def test_two_clients_upload_at_the_same_time():
    s = FakeStorage()
    a, b = logged_in(s), logged_in(s)
    da, db = os.urandom(50_000), os.urandom(70_000)
    ra = a.ask({"type": "Upload Request", "file_path": "a.bin", "file_size": len(da)})
    rb = b.ask({"type": "Upload Request", "file_path": "b.bin", "file_size": len(db)})
    assert ra["type"] == rb["type"] == "Upload Ready"
    a.sock.sendall(da)
    b.sock.sendall(db)
    assert a.read()["type"] == "Upload Complete" and b.read()["type"] == "Upload Complete"
    assert s.files["a.bin"] == da and s.files["b.bin"] == db
    a.finish()
    b.finish()


# numbers for the analysis module


class Collector:
    """A list of records that works as the record_transfer function."""

    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def __call__(self, record):
        with self.lock:
            self.records.append(record)


def test_download_is_recorded():
    s = FakeStorage()
    s.files["f.bin"] = os.urandom(5000)
    got = Collector()
    c = logged_in(s, got)
    download(c, "f.bin")
    assert wait_until(lambda: len(got.records) == 1)
    r = got.records[0]
    assert (r["operation"], r["file"], r["size_bytes"], r["user"]) == ("download", "f.bin", 5000, "ty")
    assert r["seconds"] >= 0 and r["response_time_s"] >= 0 and r["clients"] >= 1
    c.finish()


def test_upload_is_recorded():
    got = Collector()
    c = logged_in(FakeStorage(), got)
    upload(c, "u.bin", os.urandom(4321))
    assert wait_until(lambda: len(got.records) == 1)
    r = got.records[0]
    assert (r["operation"], r["file"], r["size_bytes"]) == ("upload", "u.bin", 4321)
    c.finish()


def test_rate_is_megabytes_per_second():
    s = FakeStorage()
    s.files["f"] = b"x" * 10
    got = Collector()
    c = logged_in(s, got)
    download(c, "f")
    assert wait_until(lambda: len(got.records) == 1)
    r = got.records[0]
    if r["seconds"] > 0:
        assert r["mb_per_s"] == pytest.approx(10 / r["seconds"] / 1_000_000)
    c.finish()


def test_records_have_everything_the_analysis_needs():
    s = FakeStorage()
    s.files["f"] = b"abc"
    got = Collector()
    c = logged_in(s, got)
    download(c, "f")
    assert wait_until(lambda: len(got.records) == 1)
    expected = {
        "operation", "file", "size_bytes", "seconds", "mb_per_s",
        "response_time_s", "clients", "user", "client", "time",
    }
    assert set(got.records[0]) == expected
    c.finish()


def test_transfers_that_fail_or_are_refused_are_not_recorded():
    s = FakeStorage()
    s.files["big"] = os.urandom(3 * 1024 * 1024)
    got = Collector()
    c = logged_in(s, got)
    c.ask({"type": "Download Request", "file_path": "missing"})
    c.ask({"type": "Upload Request", "file_path": "../x", "file_size": 1})
    c.ask({"type": "Dir Request"})
    c.ask({"type": "Download Request", "file_path": "big"})
    c.reader.read(100)
    c.disconnect()  # leaves in the middle of the download
    c.thread.join(5)
    time.sleep(0.1)
    assert got.records == []


def test_a_crashing_recorder_does_not_break_the_transfer():
    def boom(record):
        raise RuntimeError("analysis is broken")

    s = FakeStorage()
    s.files["f"] = b"hello"
    c = logged_in(s, boom)
    reply, data = download(c, "f")
    assert data == b"hello"
    assert c.ask({"type": "Dir Request"})["type"] == "Dir Response"
    c.finish()


def test_a_slow_recorder_does_not_inflate_the_measured_time():
    got = []

    def slow(record):
        got.append(record)
        time.sleep(0.4)  # like a slow database insert

    s = FakeStorage()
    s.files["f"] = os.urandom(3000)
    c = logged_in(s, slow)
    download(c, "f")
    assert wait_until(lambda: len(got) == 1)
    assert got[0]["seconds"] < 0.3  # the timer had stopped before the recorder ran
    c.finish()


def test_clients_counts_running_threads():
    s = FakeStorage()
    s.files["f"] = b"abc"
    got = Collector()
    a, b = logged_in(s, got), logged_in(s, got)
    download(a, "f")
    assert wait_until(lambda: len(got.records) == 1)
    assert got.records[0]["clients"] >= 2
    a.finish()
    b.finish()


