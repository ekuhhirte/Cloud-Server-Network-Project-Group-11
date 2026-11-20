"""What the network layer needs from the backend.

The network layer owns the socket. The backend never touches it, it only
gets names, sizes and chunks of bytes. Anything that goes wrong is raised as
one of the BackendError classes in this file and the dispatcher turns those
into error codes.

Uploads and downloads are begin / chunks / finish (or abort) because files are
too big for one call. begin_* gives back a handle.

Locking the backend has to do:
  begin_upload    write lock, held until finish or abort
  begin_download  read lock, held until end_download
  delete          raises FileBusy if somebody has the file
  abort_upload and end_download must not raise and can be called twice
  (they run in finally blocks)
"""

from abc import ABC, abstractmethod

from .protocol import ErrorCode


class BackendError(Exception):
    """Base class. code is what gets sent to the client."""

    code = ErrorCode.INTERNAL_ERROR
    default_message = "internal error"

    def __init__(self, message=None):
        super().__init__(message or self.default_message)


class FileNotFoundOnServer(BackendError):
    code = ErrorCode.NOT_FOUND
    default_message = "no such file or folder"


class FileExistsOnServer(BackendError):
    code = ErrorCode.FILE_EXISTS
    default_message = "file or folder already exists"


class FileBusy(BackendError):
    code = ErrorCode.FILE_BUSY
    default_message = "file is in use by another client"


class BadPath(BackendError):
    code = ErrorCode.BAD_PATH
    default_message = "illegal path"


class DiskFull(BackendError):
    code = ErrorCode.DISK_FULL
    default_message = "server is out of disk space"


class FolderNotEmpty(BackendError):
    code = ErrorCode.NOT_EMPTY
    default_message = "folder is not empty"


class Backend(ABC):
    """What the network layer calls. Paths are already validated but still use safe_join."""

    # authentication
    @abstractmethod
    def authenticate(self, username, credential):
        """True if the login is good"""

    # upload
    @abstractmethod
    def begin_upload(self, name, size, overwrite):
        """Get ready to receive size bytes for name, returns a handle.

        If the file exists and overwrite is False this raises FileExistsOnServer
        without taking a lock, so someone sitting at the overwrite prompt doesn't
        block anybody. Otherwise it takes a write lock and writes to a temp file.
        """

    @abstractmethod
    def write_chunk(self, handle, data):
        """append data to the upload (can raise DiskFull)"""

    @abstractmethod
    def finish_upload(self, handle):
        """rename the temp file into place, release the lock, return the stored name"""

    @abstractmethod
    def abort_upload(self, handle):
        """throw away the temp file and release the lock. safe to call twice"""

    # download
    @abstractmethod
    def begin_download(self, name):
        """returns (handle, size), takes a read lock"""

    @abstractmethod
    def read_chunk(self, handle, max_bytes):
        """up to max_bytes, empty bytes means end of file"""

    @abstractmethod
    def end_download(self, handle):
        """release the read lock. safe to call twice"""

    # file / folder management
    @abstractmethod
    def delete(self, name):
        """delete a file"""

    @abstractmethod
    def list_dir(self, path):
        """list of {"name", "type", "size"} dicts. path "" is the root"""

    @abstractmethod
    def create_subfolder(self, path):
        """make a folder"""

    @abstractmethod
    def delete_subfolder(self, path):
        """delete a folder"""
