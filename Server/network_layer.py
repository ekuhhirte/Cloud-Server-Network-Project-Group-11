"""Network layer for the server: talks to one client's socket.

Read a request, check it, have the backend do the work, send the reply.
Files and locks are the backend's problem, not ours. Each client thread
just calls serve_client(). Message format is in PROTOCOL.md.

The backend needs these methods (no base class to inherit):

    check_login(username, credential)   True or False
    list_dir(path)                      list of {"name", "type", "size"} dicts
    delete(path)                        delete a file
    create_dir(path)                    make a folder
    delete_dir(path)                    remove a folder
    download(path)                      "with" block giving (file, size),
                                        holds a read lock while open
    exists(path)                        True or False
    upload(path)                        "with" block giving something with
                                        .write(), holds a write lock. If the
                                        block ends in an error, throw away
                                        the half written file.

Problems are normal Python errors: FileNotFoundError, FileExistsError,
OSError (disk full etc.), or FileBusyError from this file if someone else
has the file.
"""

import errno
import json
import logging
import socket
import threading
import time

log = logging.getLogger("network_layer")

MAX_HEADER = 64 * 1024  # longest header line we accept
MAX_LOGIN_TRIES = 5
TIMEOUT_IDLE = 300  # seconds of silence before we drop the client
MAX_DIR_ENTRIES = 500
CHUNK = 64 * 1024  # bytes per read/send while moving a file
TIMEOUT_TRANSFER = 30  # seconds with no data in the middle of a file
MAX_UPLOAD = 4 * 1024**3


class FileBusyError(Exception):
    """Another client is using the file right now."""


class BadRequest(Exception):
    """Bad request. The message is what the client gets back."""


class BadStream(Exception):
    """We lost track of the stream, so the connection has to close.
    If reply is set we try to send it first."""

    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply = reply


class Client:
    """State for the one client this thread is serving."""

    def __init__(self, sock, sock_file, address, backend, record_transfer):
        self.sock = sock
        self.sock_file = sock_file  # used for headers and file bytes
        self.address = address
        self.backend = backend
        self.record_transfer = record_transfer
        self.username = None  # None until they log in
        self.failed_logins = 0


# reading and sending headers


def read_header(sock_file):
    # returns the header as a dict, or None if the line is junk
    line = sock_file.readline(MAX_HEADER + 1)
    if len(line) > MAX_HEADER:
        raise BadStream("header line is too long", reply="Header Too Long")
    if not line.endswith(b"\n"):
        raise ConnectionError("client closed the connection")
    try:
        header = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # bad json or bad utf-8
        return None
    return header if isinstance(header, dict) else None


def send_header(sock, header):
    sock.sendall((json.dumps(header) + "\n").encode("utf-8"))


def send_error(sock, text):
    send_header(sock, {"type": "error", "error": text})


# checking what the client sent


def check_path(path, allow_empty=False):
    # keeps things like ../../etc/passwd away from the backend
    if not isinstance(path, str):
        raise BadRequest("Malformed Header")
    if path == "":
        if allow_empty:
            return path
        raise BadRequest("Bad Path")
    if len(path) > 1024 or "\\" in path or path.startswith("/") or path[1:2] == ":":
        raise BadRequest("Bad Path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        raise BadRequest("Bad Path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise BadRequest("Bad Path")
    return path


def get(header, field, kind):
    # field from the header, with a type check
    value = header.get(field)
    if not isinstance(value, kind):
        raise BadRequest("Malformed Header")
    if kind is int and isinstance(value, bool):  # python treats True as an int
        raise BadRequest("Malformed Header")
    return value


def error_text(exc):
    if isinstance(exc, BadRequest):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return "File Not Found"
    if isinstance(exc, FileExistsError):
        return "Already Exists"
    if isinstance(exc, FileBusyError):
        return "File Is Busy"
    if isinstance(exc, IsADirectoryError):
        return "Is A Folder"
    if isinstance(exc, NotADirectoryError):
        return "Not A Folder"
    if isinstance(exc, TimeoutError):
        return "Transfer Timed Out"
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "Disk Full"
    if isinstance(exc, OSError) and exc.errno == errno.ENOTEMPTY:
        return "Folder Not Empty"
    return "Internal Server Error"


# one function per request type


def do_auth(c, header):
    username = get(header, "username", str)
    credential = get(header, "credential", str)
    if c.backend.check_login(username, credential):
        c.username = username
        c.failed_logins = 0
        send_header(c.sock, {"type": "Auth Response", "username": username})
        return True
    c.failed_logins += 1
    if c.failed_logins >= MAX_LOGIN_TRIES:
        send_error(c.sock, "Too Many Failed Logins")
        return False
    # same message whether the user or the password was wrong
    send_error(c.sock, "Invalid Login")
    return True


def do_dir(c, header):
    path = check_path(header.get("dir_path", ""), allow_empty=True)
    entries = c.backend.list_dir(path)
    send_header(
        c.sock,
        {
            "type": "Dir Response",
            "dir_path": path,
            "entries": entries[:MAX_DIR_ENTRIES],
            "truncated": len(entries) > MAX_DIR_ENTRIES,
        },
    )


def do_delete(c, header):
    path = check_path(header.get("file_path"))
    c.backend.delete(path)
    send_header(c.sock, {"type": "Delete Response", "file_path": path})


def do_subfolder(c, header):
    action = get(header, "action", str)
    path = check_path(header.get("dir_path"))
    if action == "create":
        c.backend.create_dir(path)
    elif action == "delete":
        c.backend.delete_dir(path)
    else:
        raise BadRequest("Malformed Header")
    send_header(c.sock, {"type": "Subfolder Response", "action": action, "dir_path": path})


def do_download(c, header):
    got_request = time.perf_counter()
    path = check_path(header.get("file_path"))
    with c.backend.download(path) as (f, size):
        send_header(
            c.sock,
            {
                "type": "Download Response",
                "filename": path.split("/")[-1],
                "file_path": path,
                "file_size": size,
            },
        )
        response_time = time.perf_counter() - got_request
        c.sock.settimeout(TIMEOUT_TRANSFER)
        try:
            start = time.perf_counter()  # only times the file bytes
            sent = 0
            while sent < size:
                # only read what is left so we send exactly file_size bytes
                chunk = f.read(min(CHUNK, size - sent))
                if not chunk:
                    raise OSError("file got shorter while it was being sent")
                c.sock.sendall(chunk)
                sent += len(chunk)
            seconds = time.perf_counter() - start
        except Exception as exc:
            # client expects file bytes here, an error message would look like
            # file data, so just close the connection
            raise BadStream("download stopped part way") from exc
    # lock is released and the timer is stopped by now
    report_transfer(c, "download", path, size, seconds, response_time)


def ask_overwrite(c, path):
    # file exists already, ask if they want to replace it
    send_header(
        c.sock,
        {
            "type": "Overwrite Prompt",
            "file_path": path,
            "message": f"'{path}' already exists. Overwrite?",
        },
    )
    reply = read_header(c.sock_file)
    if reply is None or reply.get("type") != "Overwrite Reply":
        # they might already be sending file bytes, no way to resync
        raise BadStream("expected an Overwrite Reply", reply="Expected Overwrite Reply")
    return reply.get("confirm") is True


def do_upload(c, header):
    path = check_path(header.get("file_path"))
    size = get(header, "file_size", int)
    if size < 0:
        raise BadRequest("Malformed Header")
    if size > MAX_UPLOAD:
        raise BadRequest("File Too Large")
    if c.backend.exists(path) and not ask_overwrite(c, path):
        send_header(c.sock, {"type": "Upload Cancelled", "file_path": path})
        return
    with c.backend.upload(path) as f:
        send_header(c.sock, {"type": "Upload Ready", "file_path": path, "file_size": size})
        c.sock.settimeout(TIMEOUT_TRANSFER)
        try:
            remaining = size
            while remaining > 0:
                # stop at file_size, whatever comes after is the next request
                chunk = c.sock_file.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("client left in the middle of the upload")
                f.write(chunk)
                remaining -= len(chunk)
        except Exception as exc:
            # leaving the with block discards the partial file
            raise BadStream("upload stopped part way", reply=error_text(exc)) from exc
    # we read every byte, so if saving fails the stream is still fine and
    # the client can just get an error message
    send_header(c.sock, {"type": "Upload Complete", "file_path": path, "file_size": size})


def do_upload(c, header):
    path = check_path(header.get("file_path"))
    size = get(header, "file_size", int)
    if size < 0:
        raise BadRequest("Malformed Header")
    if size > MAX_UPLOAD:
        raise BadRequest("File Too Large")
    if c.backend.exists(path) and not ask_overwrite(c, path):
        send_header(c.sock, {"type": "Upload Cancelled", "file_path": path})
        return
    asked = time.perf_counter()  # after the overwrite answer, so their think time is not counted
    with c.backend.upload(path) as f:
        send_header(c.sock, {"type": "Upload Ready", "file_path": path, "file_size": size})
        response_time = time.perf_counter() - asked
        c.sock.settimeout(TIMEOUT_TRANSFER)
        try:
            start = time.perf_counter()  # only times the file bytes
            remaining = size
            while remaining > 0:
                # stop at file_size, whatever comes after is the next request
                chunk = c.sock_file.read(min(CHUNK, remaining))
                if not chunk:
                    raise ConnectionError("client left in the middle of the upload")
                f.write(chunk)
                remaining -= len(chunk)
            seconds = time.perf_counter() - start
        except Exception as exc:
            # leaving the with block discards the partial file
            raise BadStream("upload stopped part way", reply=error_text(exc)) from exc
    # we read every byte, so if saving fails the stream is still fine and
    # the client can just get an error message
    send_header(c.sock, {"type": "Upload Complete", "file_path": path, "file_size": size})
    report_transfer(c, "upload", path, size, seconds, response_time)


def report_transfer(c, operation, path, size, seconds, response_time):
    # hands the numbers to the analysis code. Failed transfers are skipped.
    if c.record_transfer is None:
        return
    record = {
        "operation": operation,
        "file": path,
        "size_bytes": size,
        "seconds": seconds,
        "mb_per_s": size / seconds / 1_000_000 if seconds > 0 else 0.0,
        "response_time_s": response_time,  # time until we were ready, before file bytes
        "clients": threading.active_count() - 1,  # rough count of busy clients
        "user": c.username,
        "client": str(c.address),
        "time": time.time(),
    }
    try:
        c.record_transfer(record)
    except Exception:
        log.exception("record_transfer failed")  # a bug in analysis shouldn't break the transfer


def handle_request(c, header):
    # False means the connection is done
    kind = header.get("type")
    if kind == "Goodbye Request":
        send_header(c.sock, {"type": "Goodbye Response"})
        return False
    if kind == "Auth Request":
        return do_auth(c, header)
    if c.username is None:
        raise BadRequest("Not Authenticated")
    if kind == "Dir Request":
        do_dir(c, header)
        return True
    if kind == "Delete Request":
        do_delete(c, header)
        return True
    if kind == "Subfolder Request":
        do_subfolder(c, header)
        return True
    if kind == "Download Request":
        do_download(c, header)
        return True
    if kind == "Upload Request":
        do_upload(c, header)
        return True
    raise BadRequest("Unknown Command")


def hang_up(sock):
    # Closing with unread data makes TCP reset the connection and the client can
    # lose our last message. So: stop sending, read and drop what is left, then close.
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(0.2)
        drained = 0
        while drained < (1 << 20):
            data = sock.recv(65536)
            if not data:
                break
            drained += len(data)
    except OSError:
        pass


def serve_client(client_socket, client_address, backend, record_transfer=None):
    # runs until goodbye, disconnect or a protocol break. Never raises, always closes.
    log.info("new connection from %s", client_address)
    try:
        with client_socket.makefile("rb") as sock_file:
            c = Client(client_socket, sock_file, client_address, backend, record_transfer)
            keep_going = True
            while keep_going:
                client_socket.settimeout(TIMEOUT_IDLE)
                header = read_header(sock_file)
                if header is None:
                    send_error(client_socket, "Malformed Header")
                    continue
                try:
                    keep_going = handle_request(c, header)
                except (BadStream, ConnectionError, TimeoutError):
                    raise  # socket problem, nothing to reply to
                except Exception as exc:
                    text = error_text(exc)
                    if text == "Internal Server Error":
                        log.exception("request failed: %r", header.get("type"))
                    send_error(client_socket, text)
    except BadStream as exc:
        log.warning("closing %s: %s", client_address, exc)
        try:
            if exc.reply:
                send_error(client_socket, exc.reply)
        except OSError:
            pass
        hang_up(client_socket)
    except (OSError, ValueError) as exc:  # client left, timed out or the socket broke
        log.info("connection to %s ended: %s", client_address, exc)
    except Exception:
        log.exception("unexpected error with %s", client_address)
    finally:
        client_socket.close()
