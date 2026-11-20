"""Download (server -> client) and upload (client -> server).

Rules for both:
  - the size goes in the header first, then exactly that many raw bytes
  - raw bytes are never looked at, so a file can't be mistaken for a command
  - locks and temp files get released in finally blocks
  - if something fails once raw bytes are moving we don't know where the
    stream is, so we close instead of sending an error into the middle of
    the file
"""

from .protocol import ok, send_raw
from .session import CloseSession


def handle_download(session, req):
    """Send the header with the size, then exactly size raw bytes."""
    backend = session.backend
    cfg = session.config
    name = req["name"]

    # raises FileNotFoundOnServer / FileBusy / BadPath, the dispatcher turns those into
    # error replies
    handle, size = backend.begin_download(name)
    session.download_handle = handle
    try:
        session.sock.settimeout(cfg.transfer_timeout)
        session.send(ok(name=name, size=size))
        remaining = size
        while remaining > 0:
            # never ask for more than is left, the size in the header has to match what
            # we send
            chunk = backend.read_chunk(handle, min(cfg.chunk_size, remaining))
            if not chunk:
                session.log.error(
                    "%s shrank during download (%d bytes short)", name, remaining
                )
                raise CloseSession()
            send_raw(session.sock, chunk)
            remaining -= len(chunk)
        session.log.info("download complete %s (%d bytes)", name, size)
    except (OSError, CloseSession):
        # client left or timed out (or we're already closing). finally still releases
        # the lock
        raise
    except Exception:
        # backend bug mid-stream. an error reply here would look like file data, so log
        # it and close
        session.log.exception("download of %s failed mid-stream", name)
        raise CloseSession()
    finally:
        try:
            backend.end_download(handle)
        finally:
            session.download_handle = None
            try:
                session.sock.settimeout(cfg.idle_timeout)
            except OSError:
                pass
