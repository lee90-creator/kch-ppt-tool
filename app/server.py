from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "app"

from .cli_adapters import ClaudeAdapter, CliAdapter, CodexAdapter, GeminiAdapter, detect_all
from .job_runner import BusyError, JobRunner
from .preprocess import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, preprocess_job
from .prompt_builder import build_prompt
from .storage import History, Settings
from .paths import resolve_ppt_master_root

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SEARCH_LIMIT = 20
PER_FILE_LIMIT_BYTES = 500 * 1024 * 1024
TOTAL_UPLOAD_LIMIT_BYTES = 1024 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
IMAGE_SOURCES = {"none", "web", "ai"}
IMAGE_API_KEY_ENV = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
IMAGE_API_BACKENDS = set(IMAGE_API_KEY_ENV)
PPTX_MIMETYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ADAPTER_CLASSES: dict[str, type[CliAdapter]] = {
    ClaudeAdapter.name: ClaudeAdapter,
    CodexAdapter.name: CodexAdapter,
    GeminiAdapter.name: GeminiAdapter,
}

_WEBTOOL_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _WEBTOOL_ROOT.parent


class ValidationError(ValueError):
    pass


def create_app(
    config: dict[str, Any] | None = None,
    *,
    data_dir: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=str((_WEBTOOL_ROOT / "data").resolve(strict=False)),
        WORKSPACE_ROOT=str((_WEBTOOL_ROOT / "workspace").resolve(strict=False)),
        WEBTOOL_ROOT=str(_WEBTOOL_ROOT),
        REPO_ROOT=str(_REPO_ROOT),
        MAX_CONTENT_LENGTH=TOTAL_UPLOAD_LIMIT_BYTES + (16 * 1024 * 1024),
        FAKE_CLI=False,
    )
    if config:
        app.config.update(config)
    if data_dir is not None:
        app.config["DATA_DIR"] = str(data_dir)
    if workspace_root is not None:
        app.config["WORKSPACE_ROOT"] = str(workspace_root)

    data_dir = Path(app.config["DATA_DIR"]).expanduser().resolve(strict=False)
    workspace_root = Path(app.config["WORKSPACE_ROOT"]).expanduser().resolve(strict=False)
    webtool_root = Path(app.config["WEBTOOL_ROOT"]).expanduser().resolve(strict=False)
    repo_root = Path(app.config["REPO_ROOT"]).expanduser().resolve(strict=False)
    ppt_master_root = resolve_ppt_master_root(webtool_root)
    settings = Settings(data_dir)
    history = History(data_dir)
    runner_holder: dict[str, JobRunner] = {}

    def runner() -> JobRunner:
        current = runner_holder.get("runner")
        if current is None:
            current = JobRunner(workspace_root, data_dir)
            runner_holder["runner"] = current
        return current

    app.extensions["ppt_webtool"] = {
        "data_dir": data_dir,
        "workspace_root": workspace_root,
        "settings": settings,
        "history": history,
        "runner": runner,
    }

    def page_context(**extra: Any) -> dict[str, Any]:
        context: dict[str, Any] = {
            "allowed_extensions": ", ".join(sorted(ALLOWED_EXTENSIONS)),
            "allowed_accept": ",".join(sorted(ALLOWED_EXTENSIONS)),
            "per_file_limit_mb": PER_FILE_LIMIT_BYTES // (1024 * 1024),
            "total_upload_limit_gb": TOTAL_UPLOAD_LIMIT_BYTES // (1024 * 1024 * 1024),
        }
        context.update(extra)
        return context

    @app.get("/")
    def page_index() -> str:
        return render_template("index.html", **page_context(page="index", title="PPT 생성"))

    @app.get("/job/<job_id>")
    def page_job(job_id: str) -> str:
        return render_template("job.html", **page_context(page="job", title="작업 상태", job_id=job_id))

    @app.get("/history")
    def page_history() -> str:
        return render_template("history.html", **page_context(page="history", title="작업 이력"))

    @app.get("/settings")
    def page_settings() -> str:
        return render_template("settings.html", **page_context(page="settings", title="설정"))

    @app.get("/guide")
    def page_guide() -> str:
        return render_template("guide.html", **page_context(page="guide", title="설치 안내"))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_request_too_large(_error: RequestEntityTooLarge) -> tuple[Response, int]:
        return _json_error("업로드 합계가 1GB 제한을 초과했습니다.", 400)

    @app.get("/api/meta")
    def api_meta() -> Response:
        settings_data = settings.load()
        public_settings = _public_settings(settings_data)
        return jsonify(
            {
                "version": _read_version(webtool_root, repo_root),
                "clis": _serialize_clis(_detect_all_for_app(app, webtool_root)),
                "notice_accepted": bool(settings_data.get("security_notice_accepted")),
                "image_api": public_settings["image_api"],
            }
        )

    @app.post("/api/notice/accept")
    def api_accept_notice() -> Response:
        updated = settings.accept_notice()
        return jsonify({"notice_accepted": True, "settings": _public_settings(updated)})

    @app.get("/api/settings")
    def api_get_settings() -> Response:
        return jsonify(_public_settings(settings.load()))

    @app.post("/api/settings")
    def api_post_settings() -> Response:
        payload = _request_payload()
        try:
            updated = _update_image_settings(settings, payload)
        except ValidationError as exc:
            return _json_error(str(exc), 400)
        return jsonify(_public_settings(updated))

    @app.post("/api/jobs")
    def api_create_job() -> tuple[Response, int] | Response:
        if not settings.load().get("security_notice_accepted"):
            return _json_error("보안 고지에 동의해야 작업을 생성할 수 있습니다.", 403)

        form = _job_form(request.form)
        try:
            _validate_image_source(form["image_source"])
            adapter = _available_adapter(
                form["cli"],
                fake_cli=bool(app.config.get("FAKE_CLI")),
                webtool_root=webtool_root,
            )
            uploads = _request_files()
            _validate_upload_metadata(uploads)
            if request.content_length is not None and request.content_length > TOTAL_UPLOAD_LIMIT_BYTES + (16 * 1024 * 1024):
                raise ValidationError("업로드 합계가 1GB 제한을 초과했습니다.")
        except ValidationError as exc:
            return _json_error(str(exc), 400)

        current_runner = runner()
        if current_runner.is_busy():
            return _json_error("다른 작업이 실행 중입니다.", 503)

        job_id = _new_job_id()
        workspace = workspace_root / job_id
        incoming_dir = workspace / "_incoming"
        workspace_created = False
        submitted = False
        submitted_job_id = job_id
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            workspace_created = True
            incoming_dir.mkdir()
            saved_uploads = _save_uploads(uploads, incoming_dir)
            project_dir = workspace / "project"
            pipeline_ctx: dict[str, Any] = {
                "job_id": job_id,
                "workspace": str(workspace),
                "raw_dir": str(workspace / "_raw"),
                "project_dir": str(project_dir),
                "manifest": None,
                "image_env": _image_env(settings.load()),
                "cli_model": settings.load().get("claude_model"),
                "job_timeout_seconds": int(settings.load().get("job_timeout_minutes", 60)) * 60,
            }

            def run_preprocess() -> dict[str, Any]:
                return preprocess_job(
                    str(workspace),
                    saved_uploads,
                    cancel_event=pipeline_ctx.get("cancel_event"),
                    popen_factory=pipeline_ctx.get("runner_managed_popen"),
                )

            def build_stages_from_manifest(manifest: dict[str, Any]) -> list[Any]:
                style = _load_company_style(webtool_root) if form["company_style"] else None
                prompt = build_prompt(
                    form,
                    style,
                    str(ppt_master_root / "skills" / "ppt-master" / "SKILL.md"),
                    _sources_description(manifest),
                )
                stage_ctx = dict(pipeline_ctx)
                stage_ctx["manifest"] = manifest
                stage_ctx["image_env"] = _image_env(settings.load())
                return adapter.build_stages(stage_ctx, prompt)

            submitted_job_id = current_runner.submit_pipeline(
                preprocess_fn=run_preprocess,
                stages_builder=build_stages_from_manifest,
                job_id=job_id,
                job_ctx=pipeline_ctx,
            )
            submitted = True
        except BusyError as exc:
            if workspace_created and not submitted:
                shutil.rmtree(workspace, ignore_errors=True)
            return _json_error(str(exc) or "다른 작업이 실행 중입니다.", 503)
        except ValidationError as exc:
            if workspace_created and not submitted:
                shutil.rmtree(workspace, ignore_errors=True)
            return _json_error(str(exc), 400)
        except ValueError as exc:
            if workspace_created and not submitted:
                shutil.rmtree(workspace, ignore_errors=True)
            return _json_error(str(exc), 400)
        except Exception as exc:
            if workspace_created and not submitted:
                shutil.rmtree(workspace, ignore_errors=True)
            app.logger.exception("작업 생성 중 예외가 발생했습니다.")
            return _json_error(f"작업 생성 중 오류가 발생했습니다: {type(exc).__name__}", 500)

        return jsonify({"job_id": submitted_job_id}), 202

    @app.get("/api/jobs/<job_id>")
    def api_job_status(job_id: str) -> Response:
        status = runner().get_status(job_id)
        _append_history_if_terminal(history, status)
        return jsonify(status)

    @app.get("/api/jobs/<job_id>/events")
    def api_job_events(job_id: str) -> Response:
        response = Response(
            stream_with_context(_event_stream(runner(), history, job_id)),
            mimetype="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/jobs/<job_id>/cancel")
    def api_cancel_job(job_id: str) -> Response:
        status = runner().cancel(job_id)
        _append_history_if_terminal(history, status)
        return jsonify(status)

    @app.get("/api/jobs/<job_id>/download")
    def api_download_job(job_id: str) -> Response | tuple[Response, int]:
        status = runner().get_status(job_id)
        path = _latest_result_pptx(status)
        if path is None:
            return _json_error("다운로드할 PPTX 결과물이 없습니다.", 404)
        return send_file(path, mimetype=PPTX_MIMETYPE, as_attachment=True, download_name=path.name)

    @app.get("/api/history")
    def api_history() -> Response:
        return jsonify(history.list())

    @app.post("/api/shutdown")
    def api_shutdown() -> Response | tuple[Response, int]:
        if request.remote_addr not in {"127.0.0.1", "::1", "localhost"}:
            return _json_error("localhost 요청만 종료할 수 있습니다.", 403)
        shutdown = request.environ.get("werkzeug.server.shutdown")
        if callable(shutdown):
            threading.Thread(target=shutdown, daemon=True).start()
        else:
            threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        return jsonify({"ok": True})

    return app


def _json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _read_version(webtool_root: Path, repo_root: Path) -> str:
    for candidate in (webtool_root / "VERSION", repo_root / "VERSION"):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        if text:
            return text.splitlines()[0].strip() or "dev"
    return "dev"


def _serialize_clis(clis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    serialized: dict[str, dict[str, Any]] = {}
    for name, info in clis.items():
        if is_dataclass(info):
            value = asdict(info)
        elif isinstance(info, dict):
            value = dict(info)
        else:
            value = dict(getattr(info, "__dict__", {}))
        serialized[str(name)] = value
    return serialized

def _detect_all_for_app(app: Flask, webtool_root: Path) -> dict[str, Any]:
    if not app.config.get("FAKE_CLI"):
        return detect_all()

    fake_cli = _fake_cli_path(webtool_root)
    note = "개발/e2e 전용 --fake-cli 모드: 실제 CLI 대신 tests/fake_cli.py를 실행합니다."
    return {
        name: {
            "name": name,
            "path": str(fake_cli),
            "version": "tests.fake_cli.py",
            "available": True,
            "note": note,
        }
        for name in ADAPTER_CLASSES
    }


def _fake_cli_path(webtool_root: Path) -> Path:
    return webtool_root / "tests" / "fake_cli.py"


class _FakeCliAdapter(CliAdapter):
    TIMEOUT_SECONDS = 60

    def __init__(self, selected_name: str, fake_cli_path: Path) -> None:
        self.name = selected_name
        self.fake_cli_path = fake_cli_path

    def _build_command(self, _prompt: str, job_ctx: dict[str, Any] | None = None) -> list[str]:
        return [sys.executable, str(self.fake_cli_path), "success"]


def _public_settings(data: dict[str, Any]) -> dict[str, Any]:
    public = dict(data)
    image_api = data.get("image_api") or {}
    if not isinstance(image_api, dict):
        image_api = {}
    public["image_api"] = {
        "backend": _none_if_blank(image_api.get("backend")),
        "has_key": _none_if_blank(image_api.get("key")) is not None,
    }
    return public


def _request_payload() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form.to_dict(flat=True)


def _update_image_settings(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    current = settings.load()
    image_api = dict(current.get("image_api") or {})
    nested = payload.get("image_api")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ValidationError("image_api는 객체여야 합니다.")
        image_api.update(nested)
    if "backend" in payload:
        image_api["backend"] = payload.get("backend")
    if "key" in payload:
        image_api["key"] = payload.get("key")
    if "image_backend" in payload:
        image_api["backend"] = payload.get("image_backend")
    if "image_key" in payload:
        image_api["key"] = payload.get("image_key")

    backend = _normalize_image_backend(image_api.get("backend"))
    key = _none_if_blank(image_api.get("key"))

    updated = dict(current)
    updated["image_api"] = {
        "backend": backend,
        "key": key,
    }
    if "claude_model" in payload:
        updated["claude_model"] = payload.get("claude_model")
    if "job_timeout_minutes" in payload:
        updated["job_timeout_minutes"] = payload.get("job_timeout_minutes")
    return settings.save(updated)


def _normalize_image_backend(value: Any) -> str | None:
    backend = _none_if_blank(value)
    if backend is None:
        return None
    backend = backend.lower()
    if backend not in IMAGE_API_BACKENDS:
        allowed = ", ".join(sorted(IMAGE_API_BACKENDS))
        raise ValidationError(f"image_api.backend는 다음 중 하나여야 합니다: {allowed}")
    return backend


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _job_form(raw_form: Any) -> dict[str, Any]:
    return {
        "request_text": str(raw_form.get("request_text", "")).strip(),
        "page_range": str(raw_form.get("page_range", "")).strip(),
        "image_source": str(raw_form.get("image_source", "none")).strip() or "none",
        "company_style": _parse_bool(raw_form.get("company_style")),
        "audience": str(raw_form.get("audience", "")).strip(),
        "cli": str(raw_form.get("cli", "")).strip(),
    }


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _validate_image_source(image_source: str) -> None:
    if image_source not in IMAGE_SOURCES:
        allowed = ", ".join(sorted(IMAGE_SOURCES))
        raise ValidationError(f"image_source는 다음 중 하나여야 합니다: {allowed}")


def _available_adapter(
    cli_name: str,
    *,
    fake_cli: bool = False,
    webtool_root: Path | None = None,
) -> CliAdapter:
    if not cli_name:
        raise ValidationError("cli를 선택해야 합니다.")
    adapter_class = ADAPTER_CLASSES.get(cli_name)
    if adapter_class is None:
        allowed = ", ".join(sorted(ADAPTER_CLASSES))
        raise ValidationError(f"지원하지 않는 CLI입니다: {cli_name}. 선택 가능: {allowed}")
    if fake_cli:
        fake_cli_file = _fake_cli_path(webtool_root or _WEBTOOL_ROOT)
        if not fake_cli_file.is_file():
            raise ValidationError(f"--fake-cli 테스트 실행 파일을 찾을 수 없습니다: {fake_cli_file}")
        return _FakeCliAdapter(cli_name, fake_cli_file)
    info = detect_all().get(cli_name)
    if info is None or not _cli_info_available(info):
        note = _cli_info_note(info) if info is not None else None
        detail = f" ({note})" if note else ""
        raise ValidationError(f"사용 가능한 CLI가 아닙니다: {cli_name}{detail}")
    return adapter_class()


def _cli_info_available(info: Any) -> bool:
    if isinstance(info, dict):
        return bool(info.get("available"))
    return bool(getattr(info, "available", False))


def _cli_info_note(info: Any) -> str | None:
    if isinstance(info, dict):
        note = info.get("note")
    else:
        note = getattr(info, "note", None)
    return str(note) if note else None


def _request_files() -> list[Any]:
    uploads = [file for file in request.files.getlist("files[]") if file and file.filename]
    if not uploads:
        uploads = [file for file in request.files.getlist("files") if file and file.filename]
    if not uploads:
        raise ValidationError("업로드 파일이 필요합니다.")
    return uploads


def _validate_upload_metadata(uploads: list[Any]) -> None:
    for upload in uploads:
        filename = str(upload.filename or "")
        suffix = Path(filename.replace("\\", "/")).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            raise ValidationError(f"지원하지 않는 파일 형식입니다: {filename} (허용: {allowed})")
        if upload.content_length is not None and upload.content_length > PER_FILE_LIMIT_BYTES:
            raise ValidationError(f"파일당 500MB 제한을 초과했습니다: {filename}")


def _save_uploads(uploads: list[Any], incoming_dir: Path) -> list[tuple[str, str]]:
    saved: list[tuple[str, str]] = []
    total_size = 0
    for index, upload in enumerate(uploads, start=1):
        original_name = str(upload.filename or f"upload_{index}")
        suffix = Path(original_name.replace("\\", "/")).suffix.lower()
        safe_name = secure_filename(Path(original_name.replace("\\", "/")).name) or f"upload_{index}{suffix}"
        target = incoming_dir / f"{index:03d}_{uuid.uuid4().hex}_{safe_name}"
        file_size = 0
        try:
            with target.open("xb") as handle:
                while True:
                    chunk = upload.stream.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if file_size > PER_FILE_LIMIT_BYTES:
                        raise ValidationError(f"파일당 500MB 제한을 초과했습니다: {original_name}")
                    if total_size > TOTAL_UPLOAD_LIMIT_BYTES:
                        raise ValidationError("업로드 합계가 1GB 제한을 초과했습니다.")
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        saved.append((str(target), original_name))
    return saved


def _load_company_style(webtool_root: Path) -> dict[str, Any]:
    path = webtool_root / "style" / "company_style.json"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("company_style.json 루트가 객체가 아닙니다")
    return data


def _sources_description(manifest: dict[str, Any]) -> str:
    uploads = manifest.get("uploads") or []
    if not isinstance(uploads, list) or not uploads:
        return "전처리된 입력 자료가 없습니다."
    lines: list[str] = []
    for item in uploads:
        if not isinstance(item, dict):
            continue
        name = str(item.get("original_name") or "upload")
        kind = str(item.get("kind") or "document")
        outputs = item.get("outputs") or []
        output_text = ", ".join(str(output) for output in outputs) if isinstance(outputs, list) and outputs else "전처리 산출물 없음"
        lines.append(f"- {name} ({kind}): {output_text}")
    return "전처리된 입력 자료:\n" + "\n".join(lines)


def _image_env(settings_data: dict[str, Any]) -> dict[str, str]:
    image_api = settings_data.get("image_api") or {}
    if not isinstance(image_api, dict):
        return {}
    backend = _none_if_blank(image_api.get("backend"))
    if backend is not None:
        backend = backend.lower()
    key = _none_if_blank(image_api.get("key"))
    if not backend or not key:
        return {}
    key_name = IMAGE_API_KEY_ENV.get(backend)
    if key_name is None:
        return {}
    return {"IMAGE_BACKEND": backend, key_name: key}


def _event_stream(runner: JobRunner, history: History, job_id: str) -> Any:
    offset = 0
    last_status: str | None = None
    while True:
        offset, lines = runner.iter_log(job_id, offset)
        for line in lines:
            yield _sse("log", {"line": line})

        status = runner.get_status(job_id)
        current_status = str(status.get("status") or "unknown")
        if current_status != last_status:
            yield _sse("status", status)
            last_status = current_status

        if current_status in TERMINAL_STATUSES or current_status == "missing":
            offset, lines = runner.iter_log(job_id, offset)
            for line in lines:
                yield _sse("log", {"line": line})
            _append_history_if_terminal(history, status)
            yield _sse("done", status)
            return
        time.sleep(1)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n\n"


def _latest_result_pptx(status: dict[str, Any]) -> Path | None:
    workspace_raw = status.get("workspace")
    if not workspace_raw:
        return None
    workspace = Path(str(workspace_raw))
    candidates: list[Path] = []
    for item in status.get("result_files") or []:
        path = Path(str(item))
        if not path.is_absolute():
            path = workspace / path
        if path.suffix.lower() == ".pptx" and path.is_file():
            candidates.append(path)

    if not candidates and workspace.is_dir():
        candidates.extend(path for path in workspace.glob("**/exports/*.pptx") if path.is_file())
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _append_history_if_terminal(history: History, status: dict[str, Any]) -> None:
    if status.get("status") not in TERMINAL_STATUSES:
        return
    job_id = str(status.get("job_id") or status.get("id") or "")
    if not job_id or history.get(job_id) is not None:
        return
    history.append(
        {
            "job_id": job_id,
            "created_at": str(status.get("created_at") or status.get("updated_at") or ""),
            "status": str(status.get("status") or ""),
            "cli": _infer_cli_from_status(status),
            "result_files": status.get("result_files") or [],
            "workspace": str(status.get("workspace") or ""),
        }
    )


def _infer_cli_from_status(status: dict[str, Any]) -> str:
    for event in status.get("events") or []:
        if not isinstance(event, dict):
            continue
        stage_id = str(event.get("stage_id") or event.get("current_stage") or "")
        for cli_name in ADAPTER_CLASSES:
            if stage_id.startswith(f"{cli_name}_"):
                return cli_name
    current_stage = str(status.get("current_stage") or "")
    for cli_name in ADAPTER_CLASSES:
        if current_stage.startswith(f"{cli_name}_"):
            return cli_name
    return ""


def _new_job_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def _find_available_port(host: str = HOST, start_port: int = DEFAULT_PORT, max_increment: int = PORT_SEARCH_LIMIT) -> int:
    last_error: OSError | None = None
    for port in range(start_port, start_port + max_increment + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError as exc:
                last_error = exc
                continue
            return port
    raise OSError(f"사용 가능한 포트를 찾지 못했습니다: {start_port}-{start_port + max_increment}") from last_error


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ppt-webtool Flask server")
    parser.add_argument("--no-browser", action="store_true", help="서버 시작 후 브라우저를 열지 않습니다.")
    parser.add_argument(
        "--fake-cli",
        action="store_true",
        help="개발/e2e 전용: UI 선택지는 그대로 두고 실제 CLI 실행만 tests/fake_cli.py로 대체합니다.",
    )
    args = parser.parse_args(argv)

    port = _find_available_port()
    url = f"http://{HOST}:{port}"
    # Development/e2e only: this does not expose a fake option in the UI; selected CLI names use tests/fake_cli.py.
    app = create_app(config={"FAKE_CLI": args.fake_cli})
    print(f"ppt-webtool server listening on {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    app.run(host=HOST, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
