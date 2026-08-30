from __future__ import annotations

import re
import tarfile
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import LIMITS
from .extraction import Extractor, UnsupportedFormat
from .filtering import SourceFilter
from .models import (
    Artifact,
    Document,
    ProcessingOptions,
    SkipRecord,
    SourceSpec,
    StagedFile,
)
from .rendering import build_artifacts
from .security import (
    SecurityError,
    download_url,
    ensure_regular_file,
    extract_archive,
    github_archive_url,
    safe_filename,
)

ProgressCallback = Callable[[str, str, int, int, str, str | None], None]


class JobCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class PipelineResult:
    artifacts: list[Artifact]
    documents: list[Document]
    skipped: list[SkipRecord]
    warnings: list[str]


class Pipeline:
    def __init__(
        self,
        workspace: Path,
        cancel_event: threading.Event,
        progress: ProgressCallback,
    ) -> None:
        self.workspace = workspace
        self.cancel_event = cancel_event
        self.progress = progress
        self.extractor = Extractor()
        self.skipped: list[SkipRecord] = []
        self.warnings: list[str] = []
        self._expanded_bytes = 0
        self._expanded_entries = 0
        self._expanded_count = 0

    def run(self, sources: list[SourceSpec], options: ProcessingOptions) -> PipelineResult:
        staged: list[StagedFile] = []
        for index, source in enumerate(sources, start=1):
            self._check_cancelled()
            self.progress(
                "progress",
                "acquiring",
                index - 1,
                len(sources),
                f"Preparing {source.label}",
                source.id,
            )
            try:
                staged.extend(self._acquire_source(source, options))
            except Exception as exc:
                self.skipped.append(SkipRecord(source.id, source.label, str(exc)))
                self.warnings.append(f"{source.label}: {exc}")
            self.progress(
                "progress", "acquiring", index, len(sources), f"Prepared {source.label}", source.id
            )

        staged.sort(
            key=lambda item: (
                item.source_label.casefold(),
                item.relative_path.casefold(),
                item.relative_path,
            )
        )
        documents: list[Document] = []
        total = len(staged)
        for index, item in enumerate(staged, start=1):
            self._check_cancelled()
            self.progress(
                "progress",
                "extracting",
                index - 1,
                total,
                f"Extracting {item.relative_path}",
                item.source_id,
            )
            try:
                document = self.extractor.extract(item, options)
                if not document.content.strip():
                    raise ValueError("No readable content")
                documents.append(document)
                self.warnings.extend(
                    f"{item.relative_path}: {warning}" for warning in document.warnings
                )
            except (UnsupportedFormat, ValueError, OSError, SecurityError) as exc:
                self.skipped.append(SkipRecord(item.source_id, item.relative_path, str(exc)))
            except Exception as exc:
                self.skipped.append(
                    SkipRecord(
                        item.source_id,
                        item.relative_path,
                        f"Extractor error: {type(exc).__name__}: {exc}",
                    )
                )
            self.progress(
                "progress",
                "extracting",
                index,
                total,
                f"Processed {item.relative_path}",
                item.source_id,
            )

        if not documents:
            reasons = "; ".join(record.reason for record in self.skipped[:5])
            raise ValueError(f"No supported readable documents were found. {reasons}".strip())

        self._check_cancelled()
        self.progress("progress", "packaging", 0, 1, "Building output", None)
        artifacts = build_artifacts(
            self.workspace / "output",
            requested_name=options.output_name,
            documents=documents,
            skipped=self.skipped,
            warnings=self.warnings,
            sources=sources,
            options=options,
        )
        self.progress("progress", "packaging", 1, 1, "Output ready", None)
        return PipelineResult(artifacts, documents, self.skipped, self.warnings)

    def _acquire_source(self, source: SourceSpec, options: ProcessingOptions) -> list[StagedFile]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source.id):
            raise SecurityError("Invalid source identifier")
        if source.kind == "upload":
            root = self.workspace / "uploads" / source.id
            if not root.is_dir():
                raise ValueError("Uploaded source has no files")
            return self._scan(root, source, options, prefix="", depth=0)
        if source.kind == "github":
            if not source.url:
                raise ValueError("GitHub source is missing its URL")
            remote_dir = self.workspace / "remote" / source.id
            archive = remote_dir / "repository.zip"
            url = github_archive_url(source.url, source.ref)
            download_url(url, archive, max_bytes=LIMITS.max_github_bytes)
            extracted = self.workspace / "expanded" / source.id / "github"
            stats = extract_archive(archive, extracted)
            self._account_archive(stats.entries, stats.expanded_bytes)
            root = _collapse_single_root(extracted)
            return self._scan(root, source, options, prefix="", depth=1)
        if source.kind == "web":
            if not source.url:
                raise ValueError("Web source is missing its URL")
            parsed = urlsplit(source.url)
            candidate = safe_filename(unquote(Path(parsed.path).name), "web-page")
            if not Path(candidate).suffix:
                candidate += ".html"
            destination = self.workspace / "remote" / source.id / candidate
            result = download_url(source.url, destination, max_bytes=LIMITS.max_web_bytes)
            if not (
                result.content_type.startswith("text/")
                or result.content_type in {"application/xhtml+xml", "application/xml"}
            ):
                raise ValueError(f"Web URL returned unsupported content type {result.content_type}")
            if result.content_type == "text/plain" and destination.suffix.lower() not in {
                ".txt",
                ".md",
            }:
                renamed = destination.with_suffix(".txt")
                destination.rename(renamed)
                destination = renamed
            elif "html" in result.content_type and destination.suffix.lower() not in {
                ".html",
                ".htm",
            }:
                renamed = destination.with_suffix(".html")
                destination.rename(renamed)
                destination = renamed
            return [
                StagedFile(
                    source.id,
                    source.label,
                    destination,
                    destination.name,
                    source_url=result.final_url,
                )
            ]
        raise ValueError(f"Unsupported source kind: {source.kind}")

    def _scan(
        self,
        root: Path,
        source: SourceSpec,
        options: ProcessingOptions,
        *,
        prefix: str,
        depth: int,
    ) -> list[StagedFile]:
        source_filter = SourceFilter(
            root,
            mode=options.filter_mode,
            includes=options.include_patterns,
            excludes=options.exclude_patterns,
        )
        files: list[StagedFile] = []
        for path in sorted(root.rglob("*"), key=lambda value: (str(value).casefold(), str(value))):
            self._check_cancelled()
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            display_path = f"{prefix}/{relative}".strip("/")
            if not source_filter.allows(relative):
                continue
            try:
                ensure_regular_file(path)
            except SecurityError as exc:
                self.skipped.append(SkipRecord(source.id, display_path, str(exc)))
                continue
            size = path.stat().st_size
            if size > LIMITS.max_file_bytes:
                self.skipped.append(
                    SkipRecord(source.id, display_path, "File exceeds the size limit")
                )
                continue
            if _is_archive(path):
                if depth >= LIMITS.max_archive_depth:
                    self.skipped.append(
                        SkipRecord(source.id, display_path, "Archive nesting limit reached")
                    )
                    continue
                self._expanded_count += 1
                extracted = (
                    self.workspace / "expanded" / source.id / f"archive-{self._expanded_count}"
                )
                try:
                    stats = extract_archive(path, extracted)
                    self._account_archive(stats.entries, stats.expanded_bytes)
                    archive_prefix = f"{display_path}"
                    files.extend(
                        self._scan(
                            _collapse_single_root(extracted),
                            source,
                            options,
                            prefix=archive_prefix,
                            depth=depth + 1,
                        )
                    )
                except (ValueError, OSError, SecurityError) as exc:
                    self.skipped.append(SkipRecord(source.id, display_path, str(exc)))
                continue
            files.append(
                StagedFile(
                    source.id,
                    source.label,
                    path,
                    display_path,
                    source_url=source.url if source.kind in {"github", "web"} else None,
                    archive_depth=depth,
                )
            )
        return files

    def _account_archive(self, entries: int, expanded_bytes: int) -> None:
        self._expanded_entries += entries
        if self._expanded_entries > LIMITS.max_archive_entries:
            raise SecurityError("Combined archive entry count exceeds the job limit")
        self._expanded_bytes += expanded_bytes
        if self._expanded_bytes > LIMITS.max_expanded_bytes:
            raise SecurityError("Combined archive expansion exceeds the job limit")

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled("Job cancelled")


def _is_archive(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path) or tarfile.is_tarfile(path)
    except (OSError, tarfile.TarError):
        return False


def _collapse_single_root(root: Path) -> Path:
    children = [child for child in root.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir() and not children[0].is_symlink():
        return children[0]
    return root
