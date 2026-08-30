from __future__ import annotations

from pathlib import Path

import pathspec

from .config import DEFAULT_EXCLUDES


class SourceFilter:
    def __init__(
        self,
        root: Path,
        *,
        mode: str,
        includes: list[str],
        excludes: list[str],
    ) -> None:
        patterns: list[str] = []
        if mode == "smart":
            patterns.extend(DEFAULT_EXCLUDES)
            patterns.extend([".*", "**/.*"])
        if mode in {"smart", "project"}:
            for name in (".gitignore", ".automdignore"):
                ignore_file = root / name
                if ignore_file.is_file():
                    patterns.extend(
                        ignore_file.read_text(encoding="utf-8", errors="replace").splitlines()
                    )
        patterns.extend(excludes)
        self._excludes = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        self._includes = (
            pathspec.PathSpec.from_lines("gitwildmatch", includes) if includes else None
        )

    def allows(self, relative_path: str) -> bool:
        value = relative_path.replace("\\", "/")
        if self._includes and self._includes.match_file(value):
            return True
        return not self._excludes.match_file(value)
