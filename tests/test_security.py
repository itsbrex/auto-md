from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

from automd.security import (
    SecurityError,
    extract_archive,
    resolve_inside,
    safe_filename,
    safe_relative_path,
)


@pytest.mark.parametrize(
    "value", ["../secret", "/etc/passwd", "C:\\Windows\\system.ini", "a/../../b"]
)
def test_rejects_unsafe_relative_paths(value: str) -> None:
    with pytest.raises(SecurityError):
        safe_relative_path(value)


def test_resolve_inside_and_filename(tmp_path: Path) -> None:
    assert resolve_inside(tmp_path, "folder/file.txt") == (tmp_path / "folder/file.txt").resolve()
    assert safe_filename("../../My output?.md") == "My-output-.md"


def test_zip_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../outside.txt", "nope")
    with pytest.raises(SecurityError):
        extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


def test_zip_symlink_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, "target")
    with pytest.raises(SecurityError):
        extract_archive(archive, tmp_path / "out")


def test_normal_zip_extracts(tmp_path: Path) -> None:
    archive = tmp_path / "ok.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr("project/readme.md", "hello")
    archive.write_bytes(buffer.getvalue())
    stats = extract_archive(archive, tmp_path / "out")
    assert stats.entries == 1
    assert (tmp_path / "out/project/readme.md").read_text() == "hello"
