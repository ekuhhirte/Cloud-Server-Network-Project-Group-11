"""fake client wired to serve_connection over a socketpair"""

import threading

from server_network.fake_backend import FakeBackend
from server_network.protocol import read_exactly, read_message, send_message, send_raw
from server_network.server_loop import serve_connection
from server_network.session import SessionConfig
from tests.helpers import pair


class Harness:
    def __init__(self, backend=None, config=None, on_transfer=None):
        self.backend = backend or FakeBackend()
        self.config = config or SessionConfig(idle_timeout=5, transfer_timeout=5)
        self.sock, server_sock = pair()
        self.thread = threading.Thread(
            target=serve_connection,
            args=(server_sock, self.backend),
            kwargs={
                "config": self.config,
                "addr": ("test", 0),
                "on_transfer": on_transfer,
            },
            daemon=True,
        )
        self.thread.start()

    # raw access
    def send(self, **req):
        send_message(self.sock, req)

    def recv(self):
        return read_message(self.sock)

    def send_raw(self, data):
        send_raw(self.sock, data)

    def recv_bytes(self, n):
        return read_exactly(self.sock, n)

    # convenience
    def request(self, **req):
        self.send(**req)
        return self.recv()

    def login(self, user="ty", credential="pw"):
        reply = self.request(cmd="auth", username=user, credential=credential)
        assert reply["status"] == "ok", reply
        return reply

    def finish(self, timeout=5):
        """close our end and wait for the server thread"""
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "server thread did not exit"
