"""Download (server -> client) and upload (client -> server).

Rules for both:
  - the size goes in the header first, then exactly that many raw bytes
  - raw bytes are never looked at, so a file can't be mistaken for a command
  - locks and temp files get released in finally blocks
  - if something fails once raw bytes are moving we don't know where the
    stream is, so we close instead of sending an error into the middle of
    the file
"""

import socket

from .backend_api import BackendError, FileExistsOnServer
from .protocol import ErrorCode, error, ok, prompt, send_raw
from .session import CloseSession, State


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


# Upload


def handle_upload(session, req):
    """Start an upload. Replies ready, or prompts if the file already exists."""
    name, size = req["name"], req["size"]
    try:
        handle = session.backend.begin_upload(name, size, overwrite=False)
    except FileExistsOnServer:
        # no lock while the client decides, so walking away blocks nobody. remember the
        # request and wait for overwrite
        session.pending_upload = {"name": name, "size": size}
        session.state = State.AWAITING_OVERWRITE
        session.send(prompt(ErrorCode.FILE_EXISTS, f"'{name}' exists; overwrite?"))
        return
    # other BackendErrors (busy, bad path, disk full) go to the dispatcher: error reply,
    # connection stays open
    _receive_upload(session, handle, name, size)


def handle_overwrite(session, req):
    """Client's answer to the overwrite prompt."""
    pending = session.pending_upload
    session.pending_upload = None
    session.state = State.AUTHENTICATED
    if pending is None:  # shouldn't happen, the state machine prevents it
        session.send_error(ErrorCode.BAD_STATE, "no upload is pending")
        return
    if not req["confirm"]:
        session.send(ok(cancelled=True))
        return
    handle = session.backend.begin_upload(
        pending["name"], pending["size"], overwrite=True
    )
    _receive_upload(session, handle, pending["name"], pending["size"])


def _linger_and_drain(sock, limit=1 << 20, wait=0.2):
    """Tell the client we're done sending, then read and throw away whatever they
    are still sending (with a limit). If we close with unread data TCP sends a
    reset and the client can lose the error we just sent."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(wait)
        drained = 0
        while drained < limit:
            data = sock.recv(64 * 1024)
            if not data:
                break
            drained += len(data)
    except OSError:
        pass


def _fail_stream(session, code, message):
    """Raw bytes were in flight so we don't know where the stream is. Tell the
    client if we can and close."""
    try:
        session.send(error(code, message, fatal=True))
        _linger_and_drain(session.sock)
    except OSError:
        pass
    raise CloseSession()


def _receive_upload(session, handle, name, size):
    backend = session.backend
    cfg = session.config
    sock = session.sock
    session.upload_handle = handle  # write lock held now
    try:
        sock.settimeout(cfg.transfer_timeout)

        # stream phase: failures here close the connection
        try:
            session.send(ok(ready=True, name=name, size=size))
            remaining = size
            while remaining > 0:
                # min() so we never read past the size, the bytes after the file are the
                # next message
                chunk = sock.recv(min(cfg.chunk_size, remaining))
                if not chunk:
                    session.log.warning(
                        "client left mid-upload of %s (%d bytes short)",
                        name,
                        remaining,
                    )
                    raise CloseSession()
                backend.write_chunk(handle, chunk)
                remaining -= len(chunk)
        except (OSError, CloseSession):
            raise  # disconnect or timeout, finally aborts the upload
        except BackendError as exc:
            session.log.warning("upload of %s aborted: %s", name, exc)
            _fail_stream(session, exc.code, str(exc))
        except Exception:
            session.log.exception("upload of %s failed mid-stream", name)
            _fail_stream(session, ErrorCode.INTERNAL_ERROR, "internal server error")

        # finish phase: every byte was consumed, stream is in sync
        stored = backend.finish_upload(handle)
        session.upload_handle = None
        session.log.info("upload complete %s (%d bytes)", name, size)
        session.send(ok(name=stored, size=size))
    finally:
        if session.upload_handle is not None:
            backend.abort_upload(handle)  # safe to call twice
            session.upload_handle = None
        try:
            sock.settimeout(cfg.idle_timeout)
        except OSError:
            pass
