"""Reading and writing messages on the socket.

A message is a 4 byte length, then that many bytes of JSON, then raw file
bytes if there are any. recv() can return half a message so everything is
built on read_exactly().
"""

import json
import struct

PROTOCOL_VERSION = 1

# 4 byte unsigned length, ! = network byte order
_PREFIX = struct.Struct("!I")
PREFIX_SIZE = _PREFIX.size

# bigger than this and we assume the client is broken or lying
MAX_HEADER_LEN = 64 * 1024

# bytes per chunk when streaming files, can change for experiments
DEFAULT_CHUNK_SIZE = 64 * 1024


class ErrorCode:
    """Error codes sent to the client. Spelling has to match on both sides."""

    BAD_REQUEST = "BAD_REQUEST"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTH_FAILED = "AUTH_FAILED"
    BAD_STATE = "BAD_STATE"
    BAD_PATH = "BAD_PATH"
    NOT_FOUND = "NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    FILE_BUSY = "FILE_BUSY"
    DISK_FULL = "DISK_FULL"
    TOO_LARGE = "TOO_LARGE"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    TOO_MANY_FAILURES = "TOO_MANY_FAILURES"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# after these the connection has to close
FATAL_CODES = frozenset(
    {
        ErrorCode.PROTOCOL_ERROR,
        ErrorCode.VERSION_MISMATCH,
        ErrorCode.TOO_MANY_FAILURES,
    }
)


class ConnectionClosed(Exception):
    """Other side closed the connection. clean=True if it happened between messages."""

    def __init__(self, message="peer closed the connection", clean=False):
        super().__init__(message)
        self.clean = clean


class ProtocolError(Exception):
    """Bytes didn't follow the format so we close the connection."""


def read_exactly(sock, n):
    """Keep calling recv until we have n bytes (one recv can give back less)."""
    if n < 0:
        raise ValueError("n must be >= 0")
    buf = bytearray()
    while len(buf) < n:
        # only ask for what's missing so we don't read into the next message
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionClosed(
                f"peer closed after {len(buf)} of {n} bytes",
                clean=(len(buf) == 0),
            )
        buf.extend(chunk)
    return bytes(buf)


def encode_message(msg):
    """Turn a message dict into bytes we can send."""
    # keep real utf-8 instead of \u escapes
    payload = json.dumps(
        msg, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(payload) > MAX_HEADER_LEN:
        raise ValueError(
            f"header of {len(payload)} bytes exceeds MAX_HEADER_LEN"
        )
    # length of the encoded bytes, not the characters
    return _PREFIX.pack(len(payload)) + payload


def send_message(sock, msg):
    """Send one header. File bytes go separately with send_raw."""
    sock.sendall(encode_message(msg))


def send_raw(sock, data):
    """Send file bytes, sendall so nothing is left half sent."""
    sock.sendall(data)


def read_message(sock, max_len=MAX_HEADER_LEN):
    """Read one header and give back a dict. File bytes after it stay on the socket."""
    (length,) = _PREFIX.unpack(read_exactly(sock, PREFIX_SIZE))
    if length == 0 or length > max_len:
        # don't read it, a client could claim gigabytes
        raise ProtocolError(f"illegal header length {length}")
    raw = read_exactly(sock, length)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("header is not valid UTF-8") from exc
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("header is not valid JSON") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("header JSON must be an object")
    return msg


# response builders
# one place for reply shapes so field names aren't typed all over


def ok(**fields):
    return {"status": "ok", **fields}


def error(code, message, fatal=None):
    if fatal is None:
        fatal = code in FATAL_CODES
    return {"status": "error", "code": code, "message": message, "fatal": fatal}


def prompt(code, message):
    return {"status": "prompt", "code": code, "message": message}
