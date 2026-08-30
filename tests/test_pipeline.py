from __future__ import annotations

import threading
import zipfile
from pathlib import Path

from automd.models import OutputMode, ProcessingOptions, SourceSpec
from automd.pipeline import Pipeline


def test_pipeline_processes_folder_filters_and_archive(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    upload = workspace / "uploads" / "src"
    (upload / "project").mkdir(parents=True)
    (upload / "project/app.py").write_text("print('hello 世界')\n")
    (upload / "project/node_modules/pkg").mkdir(parents=True)
    (upload / "project/node_modules/pkg/index.js").write_text("noise")
    with zipfile.ZipFile(upload / "project/docs.zip", "w") as archive:
        archive.writestr("guide/readme.md", "# Guide\n\nUseful")
    events: list[str] = []
    pipeline = Pipeline(
        workspace,
        threading.Event(),
        lambda event, phase, completed, total, message, source_id: events.append(phase),
    )
    result = pipeline.run(
        [SourceSpec("src", "upload", "project")],
        ProcessingOptions(output_mode=OutputMode.MARKDOWN, output_name="project"),
    )
    output = result.artifacts[0].path.read_text()
    assert "hello 世界" in output
    assert "Useful" in output
    assert "node_modules" not in output
    assert "packaging" in events
