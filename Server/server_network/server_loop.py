"""The loop each client thread runs: read, validate, check state, dispatch.
"""

import logging
import socket

from .backend_api import BackendError
from .handlers import HANDLERS
from .protocol import (
    ConnectionClosed,
    ErrorCode,
    ProtocolError,
    error,
    read_message,
    send_message,
)
from .session import CloseSession, Session, SessionConfig, State, check_state
from .validation import ValidationError, validate_request

log = logging.getLogger("server_network.loop")


def dispatch(session, raw):
    """Handle one request. Normal errors become error replies, CloseSession and
    fatal stream errors go up and end the session."""
    try:
        req = validate_request(raw, session.config.max_upload_size)
        problem = check_state(session, req["cmd"])
        if problem is not None:
            session.send_error(*problem)
            return
        HANDLERS[req["cmd"]](session, req)
    except ValidationError as exc:
        session.send_error(exc.code, exc.message)
    except BackendError as exc:
        # normal no's from the backend: busy, not found, ...
        session.send_error(exc.code, str(exc))


def serve_connection(sock, backend, config=None, addr=None, on_transfer=None):
    """Run a client until goodbye, disconnect or a fatal error. Never raises and
    always closes the socket."""
    config = config or SessionConfig()
    session = Session(
        sock=sock,
        backend=backend,
        config=config,
        addr=addr,
        on_transfer=on_transfer,
    )
    session.log.info("session start")
    try:
        sock.settimeout(config.idle_timeout)
        while session.state is not State.CLOSED:
            try:
                raw = _read(session)
            except ConnectionClosed:
                session.log.info("client disconnected")
                break
            except ProtocolError as exc:
                session.log.warning("protocol error: %s", exc)
                _best_effort_send(
                    sock, error(ErrorCode.PROTOCOL_ERROR, "malformed message")
                )
                break
            try:
                dispatch(session, raw)
            except CloseSession:
                break
            except (ConnectionError, socket.timeout):
                raise
            except Exception:
                session.log.exception("unexpected error handling %r", raw.get("cmd"))
                session.send_error(ErrorCode.INTERNAL_ERROR, "internal server error")
    except socket.timeout:
        session.log.info("timed out waiting for client")
    except (ConnectionError, OSError) as exc:
        session.log.info("connection error: %s", exc)
    except Exception:
        session.log.exception("session crashed")
    finally:
        session.state = State.CLOSED
        session.cleanup()
        try:
            sock.close()
        except OSError:
            pass
        session.log.info("session end")


def _read(session):
    return read_message(session.sock)


def _best_effort_send(sock, msg):
    try:
        send_message(sock, msg)
    except (OSError, ValueError):
        pass
