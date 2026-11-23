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

Problems are normal Python errors: FileNotFoundError, FileExistsError,
OSError (disk full etc.), or FileBusyError from this file if someone else
has the file.
"""

import errno
import json
import logging
import socket

log = logging.getLogger("network_layer")

MAX_HEADER = 64 * 1024  # longest header line we accept
MAX_LOGIN_TRIES = 5
TIMEOUT_IDLE = 300  # seconds of silence before we drop the client
MAX_DIR_ENTRIES = 500
CHUNK = 64 * 1024  # bytes per read/send while moving a file
TIMEOUT_TRANSFER = 30  # seconds with no data in the middle of a file


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
        c.sock.settimeout(TIMEOUT_TRANSFER)
        try:
            sent = 0
            while sent < size:
                # only read what is left so we send exactly file_size bytes
                chunk = f.read(min(CHUNK, size - sent))
                if not chunk:
                    raise OSError("file got shorter while it was being sent")
                c.sock.sendall(chunk)
                sent += len(chunk)
        except Exception as exc:
            # client expects file bytes here, an error message would look like
            # file data, so just close the connection
            raise BadStream("download stopped part way") from exc


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
