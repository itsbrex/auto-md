from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Limits:
    max_job_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 100 * 1024 * 1024
    max_github_bytes: int = 250 * 1024 * 1024
    max_web_bytes: int = 10 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_archive_entries: int = 20_000
    max_archive_depth: int = 2
    max_pdf_pages: int = 500
    max_image_pixels: int = 50_000_000
    max_redirects: int = 5
    remote_timeout_seconds: int = 60


LIMITS = Limits()

DEFAULT_EXCLUDES = (
    ".git/",
    ".hg/",
    ".svn/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    ".cache/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".next/",
    ".nuxt/",
    "coverage/",
    "dist/",
    "build/",
    "target/",
    "*.min.js",
    "*.min.css",
    "*.map",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
)

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}
