"""checks the fake backend follows the contract"""

import pytest

from server_network.backend_api import (
    BackendError,
    BadPath,
    DiskFull,
    FileBusy,
    FileExistsOnServer,
    FileNotFoundOnServer,
    FolderNotEmpty,
)
from server_network.fake_backend import FakeBackend
from server_network.protocol import ErrorCode


def test_exception_codes_match_protocol():
    assert FileNotFoundOnServer().code == ErrorCode.NOT_FOUND
    assert FileExistsOnServer().code == ErrorCode.FILE_EXISTS
    assert FileBusy().code == ErrorCode.FILE_BUSY
    assert BadPath().code == ErrorCode.BAD_PATH
    assert DiskFull().code == ErrorCode.DISK_FULL
    assert FolderNotEmpty().code == ErrorCode.NOT_EMPTY
    assert issubclass(FileBusy, BackendError)


def test_upload_then_download_round_trip():
    b = FakeBackend()
    h = b.begin_upload("a.txt", 5, overwrite=False)
    b.write_chunk(h, b"he")
    b.write_chunk(h, b"llo")
    assert b.finish_upload(h) == "a.txt"
    assert not b.open_uploads

    h2, size = b.begin_download("a.txt")
    assert size == 5
    assert b.read_chunk(h2, 3) == b"hel"
    assert b.read_chunk(h2, 3) == b"lo"
    assert b.read_chunk(h2, 3) == b""
    b.end_download(h2)
    assert not b.open_downloads


def test_existing_file_needs_overwrite_and_takes_no_handle():
    b = FakeBackend()
    b.files["a.txt"] = b"x"
    with pytest.raises(FileExistsOnServer):
        b.begin_upload("a.txt", 1, overwrite=False)
    assert not b.open_uploads
    b.begin_upload("a.txt", 1, overwrite=True)


def test_busy_and_missing():
    b = FakeBackend()
    b.files["a"] = b"x"
    b.busy.add("a")
    with pytest.raises(FileBusy):
        b.delete("a")
    with pytest.raises(FileBusy):
        b.begin_download("a")
    with pytest.raises(FileNotFoundOnServer):
        b.delete("zzz")


def test_abort_and_end_are_idempotent():
    b = FakeBackend()
    h = b.begin_upload("a", 1, overwrite=False)
    b.abort_upload(h)
    b.abort_upload(h)  # second call must not raise
    assert not b.open_uploads


def test_disk_limit():
    b = FakeBackend()
    b.disk_limit = 10
    with pytest.raises(DiskFull):
        b.begin_upload("big", 11, overwrite=False)


def test_folders_and_listing():
    b = FakeBackend()
    b.create_subfolder("docs")
    with pytest.raises(FileExistsOnServer):
        b.create_subfolder("docs")
    with pytest.raises(BadPath):
        b.create_subfolder("nope/child")
    b.files["docs/a.txt"] = b"abc"
    b.files["root.txt"] = b"z"
    assert [e["name"] for e in b.list_dir("")] == ["docs", "root.txt"]
    assert b.list_dir("docs") == [{"name": "a.txt", "type": "file", "size": 3}]
    with pytest.raises(FolderNotEmpty):
        b.delete_subfolder("docs")
    del b.files["docs/a.txt"]
    b.delete_subfolder("docs")
    with pytest.raises(FileNotFoundOnServer):
        b.list_dir("docs")


def test_authenticate():
    b = FakeBackend({"ty": "pw"})
    assert b.authenticate("ty", "pw") is True
    assert b.authenticate("ty", "bad") is False
    assert b.authenticate("nobody", "pw") is False
