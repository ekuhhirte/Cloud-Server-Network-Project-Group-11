"""State for one connection.

It lives here and not in globals because threads share globals, so one
client's login would leak into another's.
"""

import enum
import logging
from dataclasses import dataclass, field

from .protocol import DEFAULT_CHUNK_SIZE, ErrorCode, error, send_message
from .validation import DEFAULT_MAX_UPLOAD_SIZE


class State(enum.Enum):
    CONNECTED = "connected"  # TCP up, not logged in
    AUTHENTICATED = "authenticated"  # logged in, idle
    AWAITING_OVERWRITE = "awaiting_overwrite"  # sent FILE_EXISTS prompt
    CLOSED = "closed"


# which commands are allowed in which state
ALLOWED = {
    State.CONNECTED: {"hello", "auth", "goodbye"},
    State.AUTHENTICATED: {
        "upload",
        "download",
        "delete",
        "dir",
        "subfolder",
        "goodbye",
    },
    State.AWAITING_OVERWRITE: {"overwrite", "goodbye"},
    State.CLOSED: set(),
}


@dataclass
class SessionConfig:
    """Settings, change these for experiments."""

    idle_timeout: float = 300.0  # seconds of silence before dropping a client
    transfer_timeout: float = 30.0  # max silence mid-transfer
    chunk_size: int = DEFAULT_CHUNK_SIZE
    max_upload_size: int = DEFAULT_MAX_UPLOAD_SIZE
    max_failed_logins: int = 5
    max_dir_entries: int = 500


class CloseSession(Exception):
    """Raise this from a handler to end the session."""


@dataclass
class Session:
    sock: object
    backend: object
    config: SessionConfig = field(default_factory=SessionConfig)
    addr: object = None
    on_transfer: object = None  # optional callback(TransferRecord)

    state: State = State.CONNECTED
    username: str = None
    failed_logins: int = 0
    pending_upload: dict = None  # {"name":..., "size":...} while prompting
    upload_handle: object = None  # backend handle for an in-flight upload
    download_handle: object = None  # backend handle for an in-flight download

    def __post_init__(self):
        self.log = logging.LoggerAdapter(
            logging.getLogger("server_network.session"), {"addr": self.addr}
        )

    # sending
    def send(self, msg):
        send_message(self.sock, msg)

    def send_error(self, code, message):
        """Send an error reply. Fatal ones close the session too."""
        msg = error(code, message)
        self.send(msg)
        if msg["fatal"]:
            self.state = State.CLOSED

    # cleanup
    def cleanup(self):
        """Release anything still held. Runs in a finally so it can't raise, and it's
        safe to call twice.
        """
        if self.upload_handle is not None:
            try:
                self.backend.abort_upload(self.upload_handle)
            except Exception:
                self.log.exception("abort_upload failed during cleanup")
            self.upload_handle = None
        if self.download_handle is not None:
            try:
                self.backend.end_download(self.download_handle)
            except Exception:
                self.log.exception("end_download failed during cleanup")
            self.download_handle = None
        self.pending_upload = None


def check_state(session, cmd):
    """Returns a (code, message) error if cmd isn't allowed right now."""
    if cmd in ALLOWED[session.state]:
        return None
    if session.state is State.CONNECTED:
        return (ErrorCode.NOT_AUTHENTICATED, "log in first")
    return (ErrorCode.BAD_STATE, f"'{cmd}' is not allowed right now")
