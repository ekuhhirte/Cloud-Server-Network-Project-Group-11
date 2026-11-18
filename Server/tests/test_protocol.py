import json
import struct

import pytest

from server_network import protocol as p
from tests.helpers import dribble, pair


def test_read_exactly_normal():
    a, b = pair()
    a.sendall(b"0123456789")
    assert p.read_exactly(b, 10) == b"0123456789"


def test_read_exactly_zero_bytes():
    a, b = pair()
    assert p.read_exactly(b, 0) == b""


def test_read_exactly_survives_dribbled_bytes():
    # a single recv() would fail here, bytes come in one at a time
    a, b = pair()
    t = dribble(a, b"0123456789")
    assert p.read_exactly(b, 10) == b"0123456789"
    t.join()


def test_read_exactly_does_not_over_read():
    a, b = pair()
    a.sendall(bytes(range(20)))
    assert p.read_exactly(b, 10) == bytes(range(10))
    assert p.read_exactly(b, 10) == bytes(range(10, 20))


def test_read_exactly_peer_closes_midway():
    a, b = pair()
    a.sendall(b"abcd")
    a.close()
    with pytest.raises(p.ConnectionClosed) as info:
        p.read_exactly(b, 10)
    assert info.value.clean is False


def test_read_exactly_clean_close_flag():
    a, b = pair()
    a.close()
    with pytest.raises(p.ConnectionClosed) as info:
        p.read_exactly(b, 4)
    assert info.value.clean is True


def test_round_trip():
    a, b = pair()
    msg = {"cmd": "upload", "name": "a.txt", "size": 12}
    p.send_message(a, msg)
    assert p.read_message(b) == msg


def test_two_messages_back_to_back():
    a, b = pair()
    p.send_message(a, {"cmd": "dir"})
    p.send_message(a, {"cmd": "goodbye"})
    assert p.read_message(b) == {"cmd": "dir"}
    assert p.read_message(b) == {"cmd": "goodbye"}


def test_non_ascii_round_trip():
    a, b = pair()
    msg = {"cmd": "delete", "name": "résumé.txt"}
    p.send_message(a, msg)
    assert p.read_message(b) == msg


def test_prefix_counts_bytes_not_characters():
    msg = {"name": "ééé"}
    wire = p.encode_message(msg)
    (n,) = struct.unpack("!I", wire[:4])
    assert n == len(wire) - 4
    text = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    assert n == len(text.encode("utf-8"))
    assert n > len(text)  # é is 2 bytes but 1 character


def test_raw_bytes_after_header_are_left_on_socket():
    a, b = pair()
    p.send_message(a, {"cmd": "upload", "size": 5})
    p.send_raw(a, b"HELLO")
    assert p.read_message(b)["size"] == 5
    assert p.read_exactly(b, 5) == b"HELLO"


def test_oversized_length_rejected_without_reading():
    a, b = pair()
    a.sendall(struct.pack("!I", 4_000_000_000))
    with pytest.raises(p.ProtocolError):
        p.read_message(b)


def test_zero_length_rejected():
    a, b = pair()
    a.sendall(struct.pack("!I", 0))
    with pytest.raises(p.ProtocolError):
        p.read_message(b)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1,2,3]", b"5", b'"hi"', b"\xff\xfe\xfd"],
)
def test_bad_bodies_are_protocol_errors(body):
    a, b = pair()
    a.sendall(struct.pack("!I", len(body)) + body)
    with pytest.raises(p.ProtocolError):
        p.read_message(b)


def test_dribbled_message():
    a, b = pair()
    t = dribble(a, p.encode_message({"cmd": "dir", "path": "x"}))
    assert p.read_message(b) == {"cmd": "dir", "path": "x"}
    t.join()


def test_disconnect_in_header_and_body():
    a, b = pair()
    a.sendall(b"\x00\x00")
    a.close()
    with pytest.raises(p.ConnectionClosed):
        p.read_message(b)

    a, b = pair()
    a.sendall(struct.pack("!I", 50) + b'{"cmd"')
    a.close()
    with pytest.raises(p.ConnectionClosed):
        p.read_message(b)


def test_encode_rejects_huge_header():
    with pytest.raises(ValueError):
        p.encode_message({"x": "a" * (p.MAX_HEADER_LEN + 1)})


def test_builders():
    assert p.ok(size=3) == {"status": "ok", "size": 3}
    e = p.error(p.ErrorCode.NOT_FOUND, "nope")
    assert e["status"] == "error" and e["fatal"] is False
    assert p.error(p.ErrorCode.PROTOCOL_ERROR, "x")["fatal"] is True
    assert p.prompt("FILE_EXISTS", "?")["status"] == "prompt"
