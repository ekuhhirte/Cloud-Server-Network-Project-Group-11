"""Checks on requests. Nothing here touches sockets or files.

Anything from the network is untrusted until validate_request() passes it.
The backend should still use safe_join on paths.
"""

from pathlib import Path

from .protocol import ErrorCode

# biggest upload allowed (2 GB videos in the assignment)
DEFAULT_MAX_UPLOAD_SIZE = 4 * 1024**3
MAX_COMPONENT_LEN = 255
MAX_PATH_LEN = 1024
MAX_USERNAME_LEN = 64
MAX_CREDENTIAL_LEN = 1024


class ValidationError(Exception):
    """Request was fine as a message but not acceptable. Has an error code so we can
    reply right away. The connection stays open."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_int(value):
    # True is an int in python so isinstance() lets it through
    return isinstance(value, int) and not isinstance(value, bool)


def _has_bad_chars(text):
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def validate_path(raw, allow_empty=False):
    """Check a path from the client and return it cleaned up. Empty string means the
    storage root, only allowed if allow_empty is True."""
    if not isinstance(raw, str):
        raise ValidationError(ErrorCode.BAD_REQUEST, "path must be a string")
    if raw == "":
        if allow_empty:
            return ""
        raise ValidationError(ErrorCode.BAD_PATH, "path must not be empty")
    if len(raw) > MAX_PATH_LEN:
        raise ValidationError(ErrorCode.BAD_PATH, "path is too long")
    if _has_bad_chars(raw):
        raise ValidationError(
            ErrorCode.BAD_PATH, "path contains control characters"
        )
    if "\\" in raw:
        raise ValidationError(
            ErrorCode.BAD_PATH, "use '/' as the path separator"
        )
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValidationError(ErrorCode.BAD_PATH, "absolute paths are not allowed")
    parts = raw.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValidationError(ErrorCode.BAD_PATH, "illegal path component")
        if len(part) > MAX_COMPONENT_LEN:
            raise ValidationError(ErrorCode.BAD_PATH, "path component too long")
    return "/".join(parts)


def safe_join(root, rel):
    """Join rel onto root and make sure the result is still inside root
    (resolves .. and symlinks first)."""
    root_path = Path(root).resolve()
    candidate = (root_path / rel).resolve()
    if not candidate.is_relative_to(root_path):
        raise ValidationError(ErrorCode.BAD_PATH, "path escapes storage root")
    return candidate


def _require(req, field, kind):
    if field not in req:
        raise ValidationError(ErrorCode.BAD_REQUEST, f"missing field '{field}'")
    value = req[field]
    if kind is int:
        ok = _is_int(value)
    else:
        ok = isinstance(value, kind)
    if not ok:
        raise ValidationError(
            ErrorCode.BAD_REQUEST, f"field '{field}' must be {kind.__name__}"
        )
    return value


# each command and its validator


def _v_hello(req, limits):
    version = _require(req, "version", int)
    return {"cmd": "hello", "version": version}


def _v_auth(req, limits):
    user = _require(req, "username", str)
    cred = _require(req, "credential", str)
    if not user or len(user) > MAX_USERNAME_LEN or _has_bad_chars(user):
        raise ValidationError(ErrorCode.BAD_REQUEST, "invalid username")
    if len(cred) > MAX_CREDENTIAL_LEN:
        raise ValidationError(ErrorCode.BAD_REQUEST, "credential too long")
    return {"cmd": "auth", "username": user, "credential": cred}


def _v_upload(req, limits):
    name = validate_path(_require(req, "name", str))
    size = _require(req, "size", int)
    if size < 0:
        raise ValidationError(ErrorCode.BAD_REQUEST, "size must be >= 0")
    if size > limits["max_upload_size"]:
        raise ValidationError(ErrorCode.TOO_LARGE, "file exceeds the size limit")
    return {"cmd": "upload", "name": name, "size": size}


def _v_overwrite(req, limits):
    return {"cmd": "overwrite", "confirm": _require(req, "confirm", bool)}


def _v_download(req, limits):
    return {"cmd": "download", "name": validate_path(_require(req, "name", str))}


def _v_delete(req, limits):
    return {"cmd": "delete", "name": validate_path(_require(req, "name", str))}


def _v_dir(req, limits):
    path = req.get("path", "")
    return {"cmd": "dir", "path": validate_path(path, allow_empty=True)}


def _v_subfolder(req, limits):
    action = _require(req, "action", str)
    if action not in ("create", "delete"):
        raise ValidationError(
            ErrorCode.BAD_REQUEST, "action must be 'create' or 'delete'"
        )
    path = validate_path(_require(req, "path", str))
    return {"cmd": "subfolder", "action": action, "path": path}


def _v_goodbye(req, limits):
    return {"cmd": "goodbye"}


VALIDATORS = {
    "hello": _v_hello,
    "auth": _v_auth,
    "upload": _v_upload,
    "overwrite": _v_overwrite,
    "download": _v_download,
    "delete": _v_delete,
    "dir": _v_dir,
    "subfolder": _v_subfolder,
    "goodbye": _v_goodbye,
}


def validate_request(req, max_upload_size=DEFAULT_MAX_UPLOAD_SIZE):
    """Check one request dict and return a cleaned copy. Extra fields are ignored."""
    if not isinstance(req, dict):
        raise ValidationError(ErrorCode.BAD_REQUEST, "request must be an object")
    cmd = req.get("cmd")
    if not isinstance(cmd, str):
        raise ValidationError(ErrorCode.BAD_REQUEST, "missing or invalid 'cmd'")
    validator = VALIDATORS.get(cmd)
    if validator is None:
        raise ValidationError(ErrorCode.UNKNOWN_COMMAND, f"unknown command '{cmd}'")
    return validator(req, {"max_upload_size": max_upload_size})
