"""In-memory backend for tests.

Set busy, disk_limit or write_error to make things fail, and look at
open_uploads / open_downloads / calls afterwards to check nothing leaked.
"""

from .backend_api import (
    Backend,
    BadPath,
    DiskFull,
    FileBusy,
    FileExistsOnServer,
    FileNotFoundOnServer,
    FolderNotEmpty,
)


class _Upload:
    def __init__(self, name, overwrite):
        self.name = name
        self.overwrite = overwrite
        self.data = bytearray()


class _Download:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.pos = 0


class FakeBackend(Backend):
    def __init__(self, users=None):
        self.users = dict(users or {"ty": "pw"})
        self.files = {}  # name -> bytes
        self.dirs = {""}  # folder paths; "" is the root
        self.busy = set()  # names that behave as locked
        self.disk_limit = None  # max total bytes before DiskFull
        self.write_error = None  # exception instance raised by write_chunk
        self.open_uploads = set()  # handles not yet finished/aborted
        self.open_downloads = set()  # handles not yet ended
        self.calls = []  # (method, args) log

    # helpers
    def _log(self, method, *args):
        self.calls.append((method, args))

    def _total(self):
        return sum(len(v) for v in self.files.values())

    # auth
    def authenticate(self, username, credential):
        self._log("authenticate", username)
        return self.users.get(username) == credential

    # upload
    def begin_upload(self, name, size, overwrite):
        self._log("begin_upload", name, size, overwrite)
        if name in self.busy:
            raise FileBusy()
        if name in self.files and not overwrite:
            raise FileExistsOnServer()
        if self.disk_limit is not None and self._total() + size > self.disk_limit:
            raise DiskFull()
        handle = _Upload(name, overwrite)
        self.open_uploads.add(handle)
        return handle

    def write_chunk(self, handle, data):
        self._log("write_chunk", len(data))
        if self.write_error is not None:
            raise self.write_error
        if self.disk_limit is not None and (
            self._total() + len(handle.data) + len(data) > self.disk_limit
        ):
            raise DiskFull()
        handle.data.extend(data)

    def finish_upload(self, handle):
        self._log("finish_upload", handle.name)
        self.files[handle.name] = bytes(handle.data)
        self.open_uploads.discard(handle)
        return handle.name

    def abort_upload(self, handle):
        self._log("abort_upload", handle.name)
        self.open_uploads.discard(handle)

    # download
    def begin_download(self, name):
        self._log("begin_download", name)
        if name in self.busy:
            raise FileBusy()
        if name not in self.files:
            raise FileNotFoundOnServer()
        handle = _Download(name, self.files[name])
        self.open_downloads.add(handle)
        return handle, len(handle.data)

    def read_chunk(self, handle, max_bytes):
        chunk = handle.data[handle.pos : handle.pos + max_bytes]
        handle.pos += len(chunk)
        return bytes(chunk)

    def end_download(self, handle):
        self._log("end_download", handle.name)
        self.open_downloads.discard(handle)

    # management
    def delete(self, name):
        self._log("delete", name)
        if name in self.busy:
            raise FileBusy()
        if name not in self.files:
            raise FileNotFoundOnServer()
        del self.files[name]

    def list_dir(self, path):
        self._log("list_dir", path)
        if path not in self.dirs:
            raise FileNotFoundOnServer()
        prefix = path + "/" if path else ""
        entries = {}
        for name, data in self.files.items():
            if name.startswith(prefix):
                rest = name[len(prefix) :]
                if "/" not in rest:
                    entries[rest] = {"name": rest, "type": "file", "size": len(data)}
        for d in self.dirs:
            if d and d.startswith(prefix):
                rest = d[len(prefix) :]
                if rest and "/" not in rest:
                    entries[rest] = {"name": rest, "type": "dir", "size": 0}
        return sorted(entries.values(), key=lambda e: e["name"])

    def create_subfolder(self, path):
        self._log("create_subfolder", path)
        if path in self.dirs:
            raise FileExistsOnServer()
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent not in self.dirs:
            raise BadPath("parent folder does not exist")
        self.dirs.add(path)

    def delete_subfolder(self, path):
        self._log("delete_subfolder", path)
        if path not in self.dirs:
            raise FileNotFoundOnServer()
        prefix = path + "/"
        if any(n.startswith(prefix) for n in self.files) or any(
            d.startswith(prefix) for d in self.dirs
        ):
            raise FolderNotEmpty()
        self.dirs.discard(path)
