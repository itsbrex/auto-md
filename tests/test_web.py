from __future__ import annotations

import json
import time

from starlette.testclient import TestClient

from automd.web import create_app


def test_index_serves_the_compact_application_shell() -> None:
    app = create_app()
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="workspace"' in response.text
    assert 'id="sourceList"' in response.text
    assert 'id="setupView"' in response.text
    assert 'id="processingView"' in response.text
    assert 'id="resultView"' in response.text
    assert 'id="settingsDrawer"' in response.text
    assert "Turn almost anything" not in response.text
    assert "One clean context file" not in response.text


def test_local_job_api_end_to_end() -> None:
    app = create_app()
    with TestClient(app, base_url="http://localhost") as client:
        index = client.get("/")
        assert index.status_code == 200
        token = app.state.session_token
        payload = {
            "sources": [{"id": "source1", "kind": "upload", "label": "demo"}],
            "files": [{"part": "upload_0", "source_id": "source1", "relative_path": "demo.py"}],
            "options": {"output_name": "demo", "output_mode": "markdown"},
        }
        response = client.post(
            "/api/v1/jobs",
            data={"spec": json.dumps(payload)},
            files={"upload_0": ("demo.py", "print('hello')\n", "text/x-python")},
            headers={"Origin": "http://localhost", "X-AutoMD-Token": token},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job"]["id"]
        snapshot = response.json()["job"]
        for _ in range(100):
            snapshot = client.get(f"/api/v1/jobs/{job_id}").json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert snapshot["status"] == "completed"
        artifact = snapshot["artifacts"][0]
        download = client.get(f"/api/v1/jobs/{job_id}/artifacts/{artifact['id']}")
        assert download.status_code == 200
        assert "print('hello')" in download.text
        deleted = client.delete(
            f"/api/v1/jobs/{job_id}",
            headers={"Origin": "http://localhost", "X-AutoMD-Token": token},
        )
        assert deleted.status_code == 204


def test_mutation_requires_local_session_and_token() -> None:
    app = create_app()
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post("/api/v1/jobs", data={})
        assert response.status_code == 403
