"""The same layer over real TCP on localhost, with several clients at once.

The accept loop here is the same idea as the one in backend.py: accept, then
start a thread for each client that runs serve_client.
"""

import hashlib
import json
import os
import socket
import threading

import pytest

import network_layer
from tests.fake_storage import FakeStorage


class TestServer:
    """A tiny server on a free port."""

    __test__ = False  # not a test class, just named like one

    def __init__(self, storage, record=None):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.storage = storage
        self.record = record
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def accept_loop(self):
        while True:
            try:
                client_socket, address = self.listener.accept()
            except OSError:
                return  # the listener was closed
            threading.Thread(
                target=network_layer.serve_client,
                args=(client_socket, address, self.storage, self.record),
                daemon=True,
            ).start()

    def stop(self):
        self.listener.close()


class Client:
    """Talks to the server the way the repo's code does: json + newline out,
    makefile("rb").readline() in, file bytes from the same reader."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.reader = self.sock.makefile("rb")

    def send(self, **header):
        self.sock.sendall((json.dumps(header) + "\n").encode("utf-8"))

    def read(self):
        return json.loads(self.reader.readline())

    def ask(self, **header):
        self.send(**header)
        return self.read()

    def close(self):
        self.reader.close()
        self.sock.close()


@pytest.fixture
def server():
    srv = TestServer(FakeStorage())
    yield srv
    srv.stop()


def login(port):
    c = Client(port)
    assert c.ask(type="Auth Request", username="ty", credential="pw")["type"] == "Auth Response"
    return c


def test_one_client_does_everything(server):
    c = login(server.port)
    data = os.urandom(300_000)

    assert c.ask(type="Upload Request", file_path="f.bin", file_size=len(data))["type"] == "Upload Ready"
    c.sock.sendall(data)
    assert c.read()["type"] == "Upload Complete"

    head = c.ask(type="Download Request", file_path="f.bin")
    assert head["type"] == "Download Response" and head["filename"] == "f.bin"
    assert c.reader.read(head["file_size"]) == data

    assert c.ask(type="Subfolder Create Request", file_path="docs")["type"] == "Subfolder Create Response"
    names = [e["name"] for e in c.ask(type="Directory Request")["entries"]]
    assert names == ["docs", "f.bin"]
    assert c.ask(type="Delete Request", file_path="f.bin")["type"] == "Delete Response"
    assert c.ask(type="Goodbye Request")["type"] == "Goodbye Response"
    c.close()


def test_three_clients_move_big_files_at_the_same_time(server):
    results = {}

    def work(number):
        c = login(server.port)
        data = os.urandom(3 * 1024 * 1024)
        name = f"client{number}.bin"
        c.ask(type="Upload Request", file_path=name, file_size=len(data))
        c.sock.sendall(data)
        assert c.read()["type"] == "Upload Complete"
        head = c.ask(type="Download Request", file_path=name)
        back = c.reader.read(head["file_size"])
        results[number] = hashlib.sha256(back).digest() == hashlib.sha256(data).digest()
        c.close()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == {0: True, 1: True, 2: True}


def test_a_client_that_misbehaves_does_not_hurt_the_others(server):
    bad = Client(server.port)
    bad.sock.sendall(b"\xff\xfe garbage with no newline " * 5000)
    good = login(server.port)
    assert good.ask(type="Directory Request")["type"] == "Directory Response"
    bad.close()
    good.close()


def test_client_that_vanishes_in_the_middle_of_an_upload_leaves_nothing_behind(server):
    c = login(server.port)
    c.ask(type="Upload Request", file_path="half.bin", file_size=1_000_000)
    c.sock.sendall(os.urandom(5000))
    c.close()
    deadline = threading.Event()
    deadline.wait(0.5)
    assert server.storage.open_files == 0
    assert "half.bin" not in server.storage.files
    other = login(server.port)  # the server is still fine
    assert other.ask(type="Directory Request")["type"] == "Directory Response"
    other.close()
