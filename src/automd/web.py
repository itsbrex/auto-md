from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .config import DEFAULT_EXCLUDES, LIMITS, SUPPORTED_IMAGE_EXTENSIONS
from .jobs import BusyError, Job, JobManager
from .models import JobStatus, ProcessingOptions, SourceSpec
from .security import SecurityError, resolve_inside, safe_relative_path

PACKAGE_ROOT = Path(__file__).parent
WEB_ROOT = PACKAGE_ROOT / "web"
TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.COMPLETED_WITH_WARNINGS,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}


class SecurityHeadersMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; "
                            b"img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                            b"base-uri 'none'; frame-ancestors 'none'",
                        ),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"cross-origin-opener-policy", b"same-origin"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(manager: JobManager | None = None) -> Starlette:
    job_manager = manager or JobManager()
    session_token = secrets.token_urlsafe(32)
    instance_id = secrets.token_urlsafe(12)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        job_manager.shutdown()

    async def index(request: Request) -> Response:
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        html = html.replace("__AUTOMD_TOKEN__", session_token)
        response = HTMLResponse(html)
        response.set_cookie(
            "automd_session",
            session_token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "instance_id": instance_id})

    async def capabilities(request: Request) -> Response:
        _require_session(request, session_token)
        return JSONResponse(
            {
                "version": __version__,
                "source_kinds": ["upload", "github", "web"],
                "output_modes": ["markdown", "rag"],
                "ocr_modes": ["auto", "always", "never"],
                "image_extensions": sorted(SUPPORTED_IMAGE_EXTENSIONS),
                "default_excludes": list(DEFAULT_EXCLUDES),
                "limits": {
                    "max_job_bytes": LIMITS.max_job_bytes,
                    "max_file_bytes": LIMITS.max_file_bytes,
                    "max_github_bytes": LIMITS.max_github_bytes,
                    "max_web_bytes": LIMITS.max_web_bytes,
                    "max_archive_entries": LIMITS.max_archive_entries,
                    "max_pdf_pages": LIMITS.max_pdf_pages,
                },
            }
        )

    async def create_job(request: Request) -> Response:
        _require_mutation(request, session_token)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > LIMITS.max_job_bytes:
            return _error("Upload exceeds the job size limit", 413)
        job: Job | None = None
        try:
            form = await request.form(
                max_files=LIMITS.max_archive_entries,
                max_fields=LIMITS.max_archive_entries + 10,
                max_part_size=LIMITS.max_file_bytes,
            )
            raw_spec = form.get("spec")
            if not isinstance(raw_spec, str):
                raise ValueError("Missing JSON job spec")
            payload = json.loads(raw_spec)
            sources = _parse_sources(payload.get("sources"))
            options = ProcessingOptions.from_dict(_object(payload.get("options", {}), "options"))
            file_manifest = _parse_file_manifest(payload.get("files", []), sources)
            job = job_manager.create(sources, options)
            total = await _stage_uploads(job, form, file_manifest)
            if total > LIMITS.max_job_bytes:
                raise SecurityError("Upload exceeds the job size limit")
            upload_source_ids = {source.id for source in sources if source.kind == "upload"}
            supplied_ids = {entry["source_id"] for entry in file_manifest}
            missing = upload_source_ids - supplied_ids
            if missing:
                raise ValueError("Every upload source must include at least one file")
            job_manager.start(job)
            return JSONResponse(
                {
                    "job": job.snapshot(),
                    "events_url": f"/api/v1/jobs/{job.id}/events",
                },
                status_code=202,
            )
        except BusyError as exc:
            return _error(str(exc), 429)
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            SecurityError,
            MultiPartException,
        ) as exc:
            if job:
                job_manager.delete(job.id)
            return _error(str(exc), 400)
        except Exception as exc:
            if job:
                job_manager.delete(job.id)
            return _error(f"Unable to create job: {exc}", 500)

    async def get_job(request: Request) -> Response:
        _require_session(request, session_token)
        job = _job_or_none(job_manager, request.path_params["job_id"])
        return JSONResponse(job.snapshot()) if job else _error("Job not found", 404)

    async def job_events(request: Request) -> Response:
        _require_session(request, session_token)
        job = _job_or_none(job_manager, request.path_params["job_id"])
        if not job:
            return _error("Job not found", 404)
        try:
            sequence = int(
                request.headers.get("last-event-id", request.query_params.get("after", "0"))
            )
        except ValueError:
            sequence = 0

        async def stream() -> AsyncIterator[bytes]:
            nonlocal sequence
            idle_ticks = 0
            while True:
                current = job_manager.get(job.id)
                if not current:
                    return
                events = current.events_after(sequence)
                for event in events:
                    sequence = event.sequence
                    payload = json.dumps(event.public(), ensure_ascii=False, separators=(",", ":"))
                    message = f"id: {event.sequence}\nevent: {event.event}\ndata: {payload}\n\n"
                    yield message.encode()
                if current.status in TERMINAL_STATUSES and not current.events_after(sequence):
                    return
                idle_ticks += 1
                if idle_ticks % 60 == 0:
                    yield b": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def cancel_job(request: Request) -> Response:
        _require_mutation(request, session_token)
        job = job_manager.cancel(request.path_params["job_id"])
        return JSONResponse(job.snapshot()) if job else _error("Job not found", 404)

    async def delete_job(request: Request) -> Response:
        _require_mutation(request, session_token)
        deleted = job_manager.delete(request.path_params["job_id"])
        return Response(status_code=204) if deleted else _error("Job not found", 404)

    async def artifact(request: Request) -> Response:
        _require_session(request, session_token)
        job = _job_or_none(job_manager, request.path_params["job_id"])
        if not job:
            return _error("Job not found", 404)
        artifact_id = request.path_params["artifact_id"]
        match = next((item for item in job.artifacts if item.id == artifact_id), None)
        if not match or not match.path.is_file() or match.path.parent != job.workspace / "output":
            return _error("Artifact not found", 404)
        disposition = "inline" if request.query_params.get("inline") == "1" else "attachment"
        return FileResponse(
            match.path,
            media_type=match.media_type,
            filename=match.name,
            content_disposition_type=disposition,
        )

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/api/v1/capabilities", capabilities),
        Route("/api/v1/jobs", create_job, methods=["POST"]),
        Route("/api/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/api/v1/jobs/{job_id}", delete_job, methods=["DELETE"]),
        Route("/api/v1/jobs/{job_id}/events", job_events, methods=["GET"]),
        Route("/api/v1/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
        Route("/api/v1/jobs/{job_id}/artifacts/{artifact_id}", artifact, methods=["GET"]),
        Mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static"),
    ]
    middleware = [
        Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]),
        Middleware(SecurityHeadersMiddleware),
    ]

    async def security_error(request: Request, exc: SecurityError) -> Response:
        return _error(str(exc), 403)

    app = Starlette(
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
        exception_handlers={SecurityError: security_error},
    )
    app.state.job_manager = job_manager
    app.state.session_token = session_token
    return app


async def _stage_uploads(job: Job, form: Any, manifest: list[dict[str, str]]) -> int:
    total = 0
    seen_parts: set[str] = set()
    for entry in manifest:
        part = entry["part"]
        if part in seen_parts:
            raise ValueError("Duplicate upload part")
        seen_parts.add(part)
        upload = form.get(part)
        if not isinstance(upload, UploadFile):
            raise ValueError(f"Missing upload part {part}")
        relative = safe_relative_path(entry["relative_path"])
        root = job.workspace / "uploads" / entry["source_id"]
        destination = resolve_inside(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with destination.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    written += len(chunk)
                    total += len(chunk)
                    if written > LIMITS.max_file_bytes or total > LIMITS.max_job_bytes:
                        raise SecurityError("Upload exceeds the configured size limit")
                    output.write(chunk)
        finally:
            await upload.close()
    return total


def _parse_sources(value: Any) -> list[SourceSpec]:
    if not isinstance(value, list) or not value:
        raise ValueError("At least one source is required")
    sources: list[SourceSpec] = []
    ids: set[str] = set()
    for raw in value:
        item = _object(raw, "source")
        source_id = str(item.get("id", ""))
        kind = str(item.get("kind", ""))
        label = str(item.get("label", "")).strip()[:200]
        if not source_id or source_id in ids:
            raise ValueError("Source IDs must be present and unique")
        if kind not in {"upload", "github", "web"}:
            raise ValueError(f"Unsupported source kind: {kind}")
        if not label:
            raise ValueError("Every source needs a label")
        url = item.get("url")
        ref = item.get("ref")
        if url is not None and not isinstance(url, str):
            raise ValueError("Source URL must be a string")
        if ref is not None and not isinstance(ref, str):
            raise ValueError("GitHub ref must be a string")
        sources.append(SourceSpec(source_id, kind, label, url=url, ref=ref or None))
        ids.add(source_id)
    return sources


def _parse_file_manifest(value: Any, sources: list[SourceSpec]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("files must be an array")
    upload_ids = {source.id for source in sources if source.kind == "upload"}
    parsed: list[dict[str, str]] = []
    for raw in value:
        item = _object(raw, "file")
        part = str(item.get("part", ""))
        source_id = str(item.get("source_id", ""))
        relative_path = str(item.get("relative_path", ""))
        if not part or source_id not in upload_ids or not relative_path:
            raise ValueError("Invalid upload manifest entry")
        parsed.append({"part": part, "source_id": source_id, "relative_path": relative_path})
    return parsed


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_session(request: Request, token: str) -> None:
    if not secrets.compare_digest(request.cookies.get("automd_session", ""), token):
        raise SecurityError("Invalid local session")


def _require_mutation(request: Request, token: str) -> None:
    _require_session(request, token)
    if not secrets.compare_digest(request.headers.get("x-automd-token", ""), token):
        raise SecurityError("Invalid request token")
    origin = request.headers.get("origin")
    if not origin:
        raise SecurityError("Missing request origin")
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SecurityError("Untrusted request origin")
    if parsed.netloc != request.headers.get("host"):
        raise SecurityError("Request origin does not match the local server")


def _job_or_none(manager: JobManager, job_id: str) -> Job | None:
    if len(job_id) > 100:
        return None
    return manager.get(job_id)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
