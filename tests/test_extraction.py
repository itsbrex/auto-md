from __future__ import annotations

import json
from pathlib import Path

from automd.extraction import Extractor, decode_text
from automd.models import ProcessingOptions, StagedFile


def staged(path: Path) -> StagedFile:
    return StagedFile("source", "Example", path, path.name)


def test_text_preserves_unicode_and_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "hello.py"
    content = "def hello():\n    return 'G’day 世界'\n"
    path.write_text(content, encoding="utf-8")
    document = Extractor().extract(staged(path), ProcessingOptions())
    assert document.content == content.rstrip("\n")
    assert document.language == "python"
    assert document.content_kind == "raw"


def test_cp1252_fallback_reports_warning() -> None:
    text, warning = decode_text(b"caf\xe9")
    assert text == "café"
    assert warning


def test_html_becomes_markdown_without_scripts(tmp_path: Path) -> None:
    path = tmp_path / "page.html"
    path.write_text("<main><h1>Hello</h1><script>bad()</script><p>World</p></main>")
    document = Extractor().extract(staged(path), ProcessingOptions())
    assert "# Hello" in document.content
    assert "World" in document.content
    assert "bad()" not in document.content
    assert document.content_kind == "markdown"


def test_notebook_keeps_cells_and_optional_text_output(tmp_path: Path) -> None:
    path = tmp_path / "demo.ipynb"
    path.write_text(
        json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# Notes"]},
                    {
                        "cell_type": "code",
                        "source": ["print('ok')"],
                        "outputs": [{"output_type": "stream", "text": ["ok\\n"]}],
                    },
                ]
            }
        )
    )
    options = ProcessingOptions(include_notebook_outputs=True)
    document = Extractor().extract(staged(path), options)
    assert "Cell 1 · Markdown" in document.content
    assert "```python" in document.content
    assert "### Output" in document.content
