import os
from contextlib import contextmanager
import shutil
import errno
from network_layer import FileBusyError


class Storage:
    def __init__(self, root, users, lock_manager):
        self.root = os.path.realpath(root)
        self.users = users
        self.lm = lock_manager

    def real(self, path):
        full = os.path.realpath(os.path.join(self.root, path))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise PermissionError(path)
        return full

    def check_login(self, username, credential):
        return self.users.get(username) == credential

    def exists(self, path):
        return os.path.exists(self.real(path))

    def list_dir(self, path):
        d = self.real(path)
        if not os.path.isdir(d):
            raise FileNotFoundError(path)
        out = []
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            isdir = os.path.isdir(p)
            out.append({"name": name, "type": "dir" if isdir else "file",
                        "size": 0 if isdir else os.path.getsize(p)})
        return out

    def delete(self, path):
        real = self.real(path)
        if not os.path.exists(real):
            raise FileNotFoundError(path)
        if os.path.isdir(real):
            raise IsADirectoryError(path)
        lock = self.lm.buildOrAcquireLock(real)
        # try, don't wait: if anyone is reading or writing the file, say it is busy
        if not lock.sectionLock.acquire(blocking=False):
            raise FileBusyError()
        if not lock.writerLock.acquire(blocking=False):
            lock.sectionLock.release()
            raise FileBusyError()
        try:
            os.remove(real)
            self.lm.deleteLock(real)  # marks the lock retired, we still hold both locks
        finally:
            lock.release_write()

    def create_dir(self, path):
        os.mkdir(self.real(path))

    def delete_dir(self, path):
        os.rmdir(self.real(path))

    @contextmanager
    def download(self, path):
        real = self.real(path)
        if os.path.isdir(real):
            raise IsADirectoryError(path)
        if not os.path.exists(real):
            raise FileNotFoundError(path)
        lock = self.lm.buildOrAcquireLock(real)
        if not lock.acquire_read():
            raise FileNotFoundError(path)
        try:
            with open(real, "rb") as f:
                yield f, os.path.getsize(real)
        finally:
            lock.release_read()

    @contextmanager
    def upload(self, path):
        real = self.real(path)
        if os.path.isdir(real):
            raise IsADirectoryError(path)
        lock = self.lm.buildOrAcquireLock(real)
        if not lock.acquire_write():
            raise FileBusyError()
        temp = real + ".part"
        try:
            try:
                with open(temp, "wb") as f:
                    yield f
                os.replace(temp, real)
            except BaseException:
                if os.path.exists(temp):
                    os.remove(temp)
                raise
        finally:
            lock.release_write()
