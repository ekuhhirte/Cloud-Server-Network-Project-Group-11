import os

import pytest

from server_network.protocol import ErrorCode
from server_network.validation import (
    ValidationError,
    safe_join,
    validate_path,
    validate_request,
)


def code_of(req, **kw):
    with pytest.raises(ValidationError) as info:
        validate_request(req, **kw)
    return info.value.code


# shape / command


@pytest.mark.parametrize("req", [[1, 2], 5, "x", None])
def test_non_dict_request(req):
    assert code_of(req) == ErrorCode.BAD_REQUEST


@pytest.mark.parametrize("req", [{}, {"cmd": 5}, {"cmd": None}, {"cmd": ["a"]}])
def test_missing_or_bad_cmd(req):
    assert code_of(req) == ErrorCode.BAD_REQUEST


def test_unknown_command():
    assert code_of({"cmd": "format_disk"}) == ErrorCode.UNKNOWN_COMMAND


def test_extra_fields_ignored_and_stripped():
    out = validate_request({"cmd": "goodbye", "junk": 1})
    assert out == {"cmd": "goodbye"}


# upload


def test_upload_ok():
    out = validate_request({"cmd": "upload", "name": "docs/a.txt", "size": 12})
    assert out == {"cmd": "upload", "name": "docs/a.txt", "size": 12}


@pytest.mark.parametrize(
    "req, expected",
    [
        ({"cmd": "upload", "name": "a.txt"}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "size": 3}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": "a", "size": "12"}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": "a", "size": True}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": "a", "size": 1.5}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": "a", "size": -5}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": 7, "size": 5}, ErrorCode.BAD_REQUEST),
        ({"cmd": "upload", "name": "", "size": 5}, ErrorCode.BAD_PATH),
    ],
)
def test_upload_bad_fields(req, expected):
    assert code_of(req) == expected


def test_upload_zero_size_allowed():
    assert validate_request({"cmd": "upload", "name": "e", "size": 0})["size"] == 0


def test_upload_too_large():
    req = {"cmd": "upload", "name": "big", "size": 101}
    assert code_of(req, max_upload_size=100) == ErrorCode.TOO_LARGE
    req["size"] = 100
    assert validate_request(req, max_upload_size=100)["size"] == 100


# other commands


def test_hello_and_auth():
    assert validate_request({"cmd": "hello", "version": 1})["version"] == 1
    assert code_of({"cmd": "hello", "version": "1"}) == ErrorCode.BAD_REQUEST
    out = validate_request({"cmd": "auth", "username": "ty", "credential": "x"})
    assert out["username"] == "ty"
    assert code_of({"cmd": "auth", "username": "", "credential": "x"}) == (
        ErrorCode.BAD_REQUEST
    )
    assert code_of({"cmd": "auth", "username": "ty"}) == ErrorCode.BAD_REQUEST
    assert code_of(
        {"cmd": "auth", "username": "a\x00b", "credential": "x"}
    ) == ErrorCode.BAD_REQUEST


def test_overwrite_requires_real_bool():
    assert validate_request({"cmd": "overwrite", "confirm": True})["confirm"] is True
    assert code_of({"cmd": "overwrite", "confirm": 1}) == ErrorCode.BAD_REQUEST
    assert code_of({"cmd": "overwrite", "confirm": "yes"}) == ErrorCode.BAD_REQUEST


def test_dir_defaults_to_root():
    assert validate_request({"cmd": "dir"})["path"] == ""
    assert validate_request({"cmd": "dir", "path": "a/b"})["path"] == "a/b"


def test_subfolder_actions():
    ok = validate_request({"cmd": "subfolder", "action": "create", "path": "x"})
    assert ok["action"] == "create"
    assert code_of({"cmd": "subfolder", "action": "rename", "path": "x"}) == (
        ErrorCode.BAD_REQUEST
    )
    assert code_of({"cmd": "subfolder", "action": "create", "path": ""}) == (
        ErrorCode.BAD_PATH
    )


# path attacks

ATTACKS = [
    "../x",
    "a/../../x",
    "..",
    ".",
    "/etc/passwd",
    "C:\\x",
    "C:/x",
    "..\\x",
    "a\\b",
    "a//b",
    "a/",
    "a/./b",
    "bad\x00name",
    "tab\tname",
    "x" * 300,
    "a/" * 600 + "b",
]


@pytest.mark.parametrize("attack", ATTACKS)
def test_path_attacks_rejected(attack):
    with pytest.raises(ValidationError) as info:
        validate_path(attack)
    assert info.value.code == ErrorCode.BAD_PATH


@pytest.mark.parametrize("attack", ["../x", "/etc/passwd", "a/../b"])
@pytest.mark.parametrize("cmd", ["download", "delete", "upload"])
def test_attacks_rejected_through_commands(cmd, attack):
    req = {"cmd": cmd, "name": attack, "size": 1}
    assert code_of(req) == ErrorCode.BAD_PATH


def test_good_paths_pass():
    for good in ["a.txt", "dir/sub/file.mp4", "r\u00e9sum\u00e9.txt", "a b/c d"]:
        assert validate_path(good) == good


def test_empty_path_only_when_allowed():
    assert validate_path("", allow_empty=True) == ""
    with pytest.raises(ValidationError):
        validate_path("")


def test_path_must_be_string():
    with pytest.raises(ValidationError) as info:
        validate_path(5)
    assert info.value.code == ErrorCode.BAD_REQUEST


# safe_join (defense in depth)


def test_safe_join_inside(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    assert safe_join(root, "a/b.txt") == (root / "a" / "b.txt").resolve()


def test_safe_join_escape(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    with pytest.raises(ValidationError):
        safe_join(root, "../outside.txt")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_safe_join_blocks_symlink_escape(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / "link")
    except OSError:
        pytest.skip("symlinks not permitted here")
    with pytest.raises(ValidationError):
        safe_join(root, "link/secret.txt")
