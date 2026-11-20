"""One function per command, all of them handler(session, req).

req is already validated. Backend errors aren't caught here, the dispatcher
does that in one place.
"""

from .protocol import PROTOCOL_VERSION, ErrorCode, encode_message, ok
from .session import CloseSession, State
from .transfers import handle_download, handle_overwrite, handle_upload

SERVER_NAME = "cnt3004-file-server"


def handle_hello(session, req):
    if req["version"] != PROTOCOL_VERSION:
        session.send_error(
            ErrorCode.VERSION_MISMATCH,
            f"server speaks protocol version {PROTOCOL_VERSION}",
        )
        raise CloseSession()
    session.send(
        ok(version=PROTOCOL_VERSION, server=SERVER_NAME, auth_required=True)
    )


def handle_auth(session, req):
    cfg = session.config
    if session.failed_logins >= cfg.max_failed_logins:
        session.send_error(ErrorCode.TOO_MANY_FAILURES, "too many failed logins")
        raise CloseSession()
    if session.backend.authenticate(req["username"], req["credential"]):
        session.state = State.AUTHENTICATED
        session.username = req["username"]
        session.failed_logins = 0
        session.log.info("login ok user=%s", session.username)
        session.send(ok(username=session.username))
        return
    session.failed_logins += 1
    # same reply for a bad user or a bad password
    session.log.warning("login failed (%d)", session.failed_logins)
    if session.failed_logins >= cfg.max_failed_logins:
        session.send_error(ErrorCode.TOO_MANY_FAILURES, "too many failed logins")
        raise CloseSession()
    session.send_error(ErrorCode.AUTH_FAILED, "invalid credentials")


def _fit_listing(path, entries, max_entries):
    """cut the listing down so the reply fits in one header"""
    entries = list(entries)
    truncated = len(entries) > max_entries
    entries = entries[:max_entries]
    while entries:
        try:
            encode_message(ok(path=path, entries=entries, truncated=truncated))
            break
        except ValueError:
            entries = entries[: len(entries) // 2]
            truncated = True
    return entries, truncated


def handle_dir(session, req):
    entries = session.backend.list_dir(req["path"])
    entries, truncated = _fit_listing(
        req["path"], entries, session.config.max_dir_entries
    )
    session.send(ok(path=req["path"], entries=entries, truncated=truncated))


def handle_delete(session, req):
    session.backend.delete(req["name"])
    session.log.info("deleted %s", req["name"])
    session.send(ok(name=req["name"]))


def handle_subfolder(session, req):
    if req["action"] == "create":
        session.backend.create_subfolder(req["path"])
    else:
        session.backend.delete_subfolder(req["path"])
    session.log.info("subfolder %s %s", req["action"], req["path"])
    session.send(ok(action=req["action"], path=req["path"]))


def handle_goodbye(session, req):
    session.send(ok(goodbye=True))
    raise CloseSession()


HANDLERS = {
    "hello": handle_hello,
    "auth": handle_auth,
    "dir": handle_dir,
    "delete": handle_delete,
    "subfolder": handle_subfolder,
    "goodbye": handle_goodbye,
    "download": handle_download,
    "upload": handle_upload,
    "overwrite": handle_overwrite,
}
