"""A pretend backend that keeps everything in memory, for the tests.

It has the same methods the real backend needs (see network_layer.py). Set
busy, disk_limit or commit_error to make things fail, and look at open_files
afterwards to check nothing was left locked.
"""

import errno
from contextlib import contextmanager
import io

from network_layer import FileBusyError


class Writer:
    """What upload() gives to the network layer."""

    def __init__(self, storage):
        self.storage = storage
        self.data = bytearray()

    def write(self, chunk):
        used = sum(len(v) for v in self.storage.files.values()) + len(self.data)
        limit = self.storage.disk_limit
        if limit is not None and used + len(chunk) > limit:
            raise OSError(errno.ENOSPC, "no space left")
        self.data.extend(chunk)


class FakeStorage:
    def __init__(self, users=None):
        self.users = users or {"ty": "pw"}
        self.files = {}  # path -> bytes
        self.dirs = {""}  # "" is the root folder
        self.busy = set()  # paths that act like another client has them
        self.disk_limit = None  # most bytes the "disk" can hold
        self.commit_error = None  # raised when an upload tries to save
        self.open_files = 0  # downloads and uploads open right now

    def check_login(self, username, credential):
        return self.users.get(username) == credential

    def exists(self, path):
        return path in self.files or path in self.dirs

    def list_dir(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = path + "/" if path else ""
        entries = {}
        for name, data in self.files.items():
            rest = name[len(prefix):]
            if name.startswith(prefix) and "/" not in rest:
                entries[rest] = {"name": rest, "type": "file", "size": len(data)}
        for d in self.dirs:
            rest = d[len(prefix):]
            if d and d.startswith(prefix) and rest and "/" not in rest:
                entries[rest] = {"name": rest, "type": "dir", "size": 0}
        return sorted(entries.values(), key=lambda e: e["name"])

    def delete(self, path):
        if path in self.busy:
            raise FileBusyError()
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def create_dir(self, path):
        if path in self.dirs:
            raise FileExistsError(path)
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent not in self.dirs:
            raise FileNotFoundError(parent)
        self.dirs.add(path)

    def delete_dir(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        prefix = path + "/"
        if any(n.startswith(prefix) for n in self.files) or any(
            d.startswith(prefix) for d in self.dirs
        ):
            raise OSError(errno.ENOTEMPTY, "folder is not empty")
        self.dirs.discard(path)

    @contextmanager
    def download(self, path):
        if path in self.busy:
            raise FileBusyError()
        if path not in self.files:
            raise FileNotFoundError(path)
        self.open_files += 1
        try:
            yield io.BytesIO(self.files[path]), len(self.files[path])
        finally:
            self.open_files -= 1

    @contextmanager
    def upload(self, path):
        if path in self.busy:
            raise FileBusyError()
        writer = Writer(self)
        self.open_files += 1
        try:
            yield writer
            # only gets here if the with block finished without an error
            if self.commit_error is not None:
                raise self.commit_error
            self.files[path] = bytes(writer.data)
        finally:
            self.open_files -= 1
