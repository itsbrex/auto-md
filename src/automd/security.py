from __future__ import annotations

import ipaddress
import os
import re
import socket
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import LIMITS, Limits


class SecurityError(ValueError):
    """Raised when untrusted input violates a safety boundary."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    final_url: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    entries: int
    expanded_bytes: int


def safe_filename(value: str, default: str = "automd-output") -> str:
    value = Path(value.replace("\\", "/")).name
    value = re.sub(r"[\x00-\x1f\x7f]+", "", value)
    value = re.sub(r"[^\w. -]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"[ -]+", "-", value).strip("-.")
    return value[:120] or default


def safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    if "\x00" in normalized or re.match(r"^[A-Za-z]:", normalized):
        raise SecurityError("Invalid file path")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        raise SecurityError("Absolute or empty file paths are not allowed")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise SecurityError("Path traversal is not allowed")
    if any(any(ord(char) < 32 for char in part) for part in path.parts):
        raise SecurityError("Control characters are not allowed in paths")
    return path


def resolve_inside(root: Path, relative: str | PurePosixPath) -> Path:
    path = safe_relative_path(str(relative)) if isinstance(relative, str) else relative
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*path.parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SecurityError("Resolved path leaves the job workspace")
    return candidate


def validate_public_url(value: str, *, github_only: bool = False) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError as exc:
        raise SecurityError("Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SecurityError("Only HTTP(S) URLs are supported")
    if parsed.username or parsed.password:
        raise SecurityError("Credentials are not accepted in URLs")
    hostname = parsed.hostname.rstrip(".").lower()
    if github_only and hostname != "github.com":
        raise SecurityError("Only public github.com repositories are supported")
    _assert_public_host(hostname, parsed.port)
    return parsed


def parse_github_repo(value: str) -> tuple[str, str]:
    parsed = validate_public_url(value, github_only=True)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise SecurityError("Use a repository root URL such as https://github.com/owner/repo")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not owner or not repo or not pattern.fullmatch(owner) or not pattern.fullmatch(repo):
        raise SecurityError("Invalid GitHub owner or repository name")
    return owner, repo


def github_archive_url(value: str, ref: str | None) -> str:
    owner, repo = parse_github_repo(value)
    base = f"https://api.github.com/repos/{owner}/{repo}/zipball"
    if ref:
        if len(ref) > 200 or any(ord(char) < 32 for char in ref):
            raise SecurityError("Invalid GitHub ref")
        return f"{base}/{urllib.parse.quote(ref, safe='')}"
    return base


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects: int) -> None:
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        count = int(req.headers.get("X-AutoMD-Redirects", "0"))
        if count >= self.max_redirects:
            raise SecurityError("Too many redirects")
        validate_public_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.add_header("X-AutoMD-Redirects", str(count + 1))
        return redirected


def download_url(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    limits: Limits = LIMITS,
    accept: str = "text/html,text/plain,application/zip,application/octet-stream;q=0.8",
) -> DownloadResult:
    validate_public_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "Auto-MD/2.0 (+https://github.com/toolworks-dev/auto-md)",
        },
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(limits.max_redirects))
    try:
        with opener.open(request, timeout=limits.remote_timeout_seconds) as response:
            final_url = response.geturl()
            validate_public_url(final_url)
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise SecurityError("Remote source exceeds the configured size limit")
            size = 0
            with destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise SecurityError("Remote source exceeds the configured size limit")
                    output.write(chunk)
            content_type = response.headers.get_content_type()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ValueError(f"Unable to download {url}: {exc}") from exc
    return DownloadResult(destination, final_url, content_type, size)


def extract_archive(
    archive: Path,
    destination: Path,
    *,
    limits: Limits = LIMITS,
) -> ArchiveStats:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        return _extract_zip(archive, destination, limits)
    if tarfile.is_tarfile(archive):
        return _extract_tar(archive, destination, limits)
    raise ValueError(f"Unsupported or corrupt archive: {archive.name}")


def _extract_zip(archive: Path, destination: Path, limits: Limits) -> ArchiveStats:
    count = 0
    expanded = 0
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            count += 1
            expanded += info.file_size
            if count > limits.max_archive_entries or expanded > limits.max_expanded_bytes:
                raise SecurityError("Archive exceeds extraction limits")
            relative = safe_relative_path(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise SecurityError("Archive symlinks are not allowed")
            target = resolve_inside(destination, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with handle.open(info) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > limits.max_file_bytes:
                        raise SecurityError("Archive member exceeds the per-file limit")
                    output.write(chunk)
    return ArchiveStats(count, expanded)


def _extract_tar(archive: Path, destination: Path, limits: Limits) -> ArchiveStats:
    count = 0
    expanded = 0
    with tarfile.open(archive, mode="r:*") as handle:
        for member in handle:
            count += 1
            expanded += max(member.size, 0)
            if count > limits.max_archive_entries or expanded > limits.max_expanded_bytes:
                raise SecurityError("Archive exceeds extraction limits")
            if member.issym() or member.islnk() or member.isdev():
                raise SecurityError("Archive links and device files are not allowed")
            relative = safe_relative_path(member.name)
            target = resolve_inside(destination, relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                continue
            written = 0
            with source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    written += len(chunk)
                    if written > limits.max_file_bytes:
                        raise SecurityError("Archive member exceeds the per-file limit")
                    output.write(chunk)
    return ArchiveStats(count, expanded)


def _assert_public_host(hostname: str, port: int | None) -> None:
    try:
        records = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SecurityError(f"Unable to resolve host: {hostname}") from exc
    if not records:
        raise SecurityError(f"Unable to resolve host: {hostname}")
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise SecurityError("Private, loopback, and link-local network targets are blocked")


def is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def ensure_regular_file(path: Path) -> None:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or os.path.islink(path):
        raise SecurityError("Only regular files are supported")
