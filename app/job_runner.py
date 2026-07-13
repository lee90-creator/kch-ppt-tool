from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

try:  # Package import in the Flask app.
    from .job_store import JobStore
    from .paths import process_group_popen_kwargs, resolve_launch_command, terminate_process_tree
    from .results import collect_result_files, load_manifest, preprocess_failure_reason, refresh_manifest
    from .stage_contract import Stage, ValidationResult
except ImportError:  # Direct module import in small smoke checks.
    from job_store import JobStore
    from paths import process_group_popen_kwargs, resolve_launch_command, terminate_process_tree
    from results import collect_result_files, load_manifest, preprocess_failure_reason, refresh_manifest
    from stage_contract import Stage, ValidationResult


WARN_IDLE_SECONDS = 180
STALL_IDLE_SECONDS = 720
DEFAULT_TIMEOUT_SECONDS = 3600
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class BusyError(RuntimeError):
    pass


@dataclass
class _JobState:
    id: str
    workspace: Path
    raw_dir: Path
    project_dir: Path
    log_path: Path
    stages: list[Stage]
    manifest: dict[str, Any] | None
    record: dict[str, Any]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lock: threading.RLock = field(default_factory=threading.RLock)
    log_lock: threading.Lock = field(default_factory=threading.Lock)
    persist_lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen[str] | None = None
    active_processes: list[subprocess.Popen[str]] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_requested: bool = False
    thread: threading.Thread | None = None


class JobRunner:
    def __init__(self, workspace_root: str | os.PathLike[str], data_dir: str | os.PathLike[str]) -> None:
        self.workspace_root = Path(workspace_root)
        self.data_dir = Path(data_dir)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_job_id: str | None = None
        self._jobs: dict[str, _JobState] = {}
        self._store = JobStore(self.data_dir)

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_job_id is not None

    def submit(self, stages: list[Stage], manifest: dict[str, Any] | None = None, job_id: str | None = None) -> dict[str, Any]:
        stage_list = list(stages)
        selected_job_id = job_id or self._infer_job_id(stage_list) or self._new_job_id()
        self._validate_job_id(selected_job_id)

        with self._lock:
            if self._active_job_id is not None:
                raise BusyError("다른 작업이 실행 중입니다.")
            self._active_job_id = selected_job_id

        try:
            workspace = self.workspace_root / selected_job_id
            raw_dir = workspace / "_raw"
            project_dir = workspace / "project"
            raw_dir.mkdir(parents=True, exist_ok=True)
            project_dir.mkdir(parents=True, exist_ok=True)

            loaded_manifest = manifest if manifest is not None else self._load_manifest(workspace)
            now = _utc_now()
            log_path = workspace / "job.log"
            record: dict[str, Any] = {
                "job_id": selected_job_id,
                "id": selected_job_id,
                "status": "queued",
                "workspace": str(workspace),
                "raw_dir": str(raw_dir),
                "project_dir": str(project_dir),
                "log_path": str(log_path),
                "created_at": now,
                "updated_at": now,
                "current_stage": None,
                "stage_index": None,
                "stages_total": len(stage_list),
                "reason": None,
                "result_files": [],
                "events": [],
            }
            job = _JobState(
                id=selected_job_id,
                workspace=workspace,
                raw_dir=raw_dir,
                project_dir=project_dir,
                log_path=log_path,
                stages=stage_list,
                manifest=loaded_manifest,
                record=record,
            )
            with self._lock:
                self._jobs[selected_job_id] = job

            self._persist_job(job)
            self._record_event(job, "QUEUED", {"stages_total": len(stage_list)})
            thread = threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"job-runner-{selected_job_id}")
            job.thread = thread
            thread.start()
            return self.get_status(selected_job_id)
        except Exception:
            with self._lock:
                if self._active_job_id == selected_job_id:
                    self._active_job_id = None
                self._jobs.pop(selected_job_id, None)
            raise

    def submit_pipeline(
        self,
        *,
        preprocess_fn: Callable[[], dict[str, Any]],
        stages_builder: Callable[[dict[str, Any]], list[Stage]],
        job_id: str | None = None,
        job_ctx: dict[str, Any] | None = None,
    ) -> str:
        selected_job_id = job_id or self._new_job_id()
        self._validate_job_id(selected_job_id)
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if job_ctx is not None:
            try:
                override = int(job_ctx.get("job_timeout_seconds") or 0)
            except (TypeError, ValueError):
                override = 0
            if override > 0:
                timeout_seconds = override

        with self._lock:
            if self._active_job_id is not None:
                raise BusyError("다른 작업이 실행 중입니다.")
            self._active_job_id = selected_job_id

        try:
            workspace = self.workspace_root / selected_job_id
            raw_dir = workspace / "_raw"
            project_dir = workspace / "project"
            raw_dir.mkdir(parents=True, exist_ok=True)
            project_dir.mkdir(parents=True, exist_ok=True)

            now = _utc_now()
            log_path = workspace / "job.log"
            record: dict[str, Any] = {
                "job_id": selected_job_id,
                "id": selected_job_id,
                "status": "queued",
                "workspace": str(workspace),
                "raw_dir": str(raw_dir),
                "project_dir": str(project_dir),
                "log_path": str(log_path),
                "created_at": now,
                "updated_at": now,
                "current_stage": None,
                "stage_index": None,
                "stages_total": 0,
                "reason": None,
                "result_files": [],
                "events": [],
            }
            job = _JobState(
                id=selected_job_id,
                workspace=workspace,
                raw_dir=raw_dir,
                project_dir=project_dir,
                log_path=log_path,
                stages=[],
                manifest=None,
                record=record,
                timeout_seconds=timeout_seconds,
            )
            if job_ctx is not None:
                job_ctx.update(self._job_ctx(job))
            with self._lock:
                self._jobs[selected_job_id] = job

            self._persist_job(job)
            self._record_event(job, "QUEUED", {"pipeline": True, "stages_total": 0})
            thread = threading.Thread(
                target=self._run_pipeline_job,
                args=(job, preprocess_fn, stages_builder, job_ctx),
                daemon=True,
                name=f"job-runner-{selected_job_id}",
            )
            job.thread = thread
            thread.start()
            return selected_job_id
        except Exception:
            if job_ctx is not None:
                cancel_event = job_ctx.get("cancel_event")
                if hasattr(cancel_event, "set"):
                    cancel_event.set()
            with self._lock:
                if self._active_job_id == selected_job_id:
                    self._active_job_id = None
                self._jobs.pop(selected_job_id, None)
            raise

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._missing_status(job_id)

        with job.lock:
            if job.record["status"] in TERMINAL_STATUSES:
                return dict(job.record)
            job.cancel_requested = True
            job.cancel_event.set()
        self._record_event(job, "CANCEL_REQUESTED", {"reason": "사용자가 작업을 취소했습니다"})

        self._terminate_active_processes(job, "cancelled")

        self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
        return self.get_status(job_id)

    def get_status(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is not None:
            with job.lock:
                return dict(job.record)

        record = self._load_record(job_id)
        if record is not None:
            return record
        return self._missing_status(job_id)

    def iter_log(self, job_id: str, offset: int) -> tuple[int, list[str]]:
        log_path = self._log_path_for(job_id)
        if log_path is None or not log_path.exists():
            return max(0, offset), []

        safe_offset = max(0, offset)
        size = log_path.stat().st_size
        if safe_offset > size:
            safe_offset = 0
        with log_path.open("rb") as handle:
            handle.seek(safe_offset)
            data = handle.read()
            new_offset = handle.tell()
        if not data:
            return new_offset, []
        return new_offset, data.decode("utf-8", errors="replace").splitlines()

    def _run_job(self, job: _JobState) -> None:
        job_start = time.monotonic()
        try:
            self._record_event(job, "START", {"job_timeout_seconds": job.timeout_seconds})
            self._run_stages(job, job_start)
        except Exception as exc:  # noqa: BLE001 - background threads must persist failures, not crash silently.
            self._handle_job_exception(job, exc)
        finally:
            self._finish_job(job)

    def _run_pipeline_job(
        self,
        job: _JobState,
        preprocess_fn: Callable[[], dict[str, Any]],
        stages_builder: Callable[[dict[str, Any]], list[Stage]],
        job_ctx: dict[str, Any] | None,
    ) -> None:
        job_start = time.monotonic()
        try:
            self._record_event(job, "START", {"job_timeout_seconds": job.timeout_seconds, "pipeline": True})
            if self._is_cancelled(job):
                self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                return

            self._set_status(job, "preprocessing", current_stage="preprocess", stage_index=None)
            self._record_event(job, "PREPROCESS_START")
            preprocess_done, manifest, preprocess_exc = self._run_preprocess_with_deadline(job, preprocess_fn, job_start)
            if not preprocess_done:
                return
            if preprocess_exc is not None:
                if self._is_cancelled(job):
                    self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                else:
                    reason = f"전처리 중 예외가 발생했습니다: {type(preprocess_exc).__name__}: {preprocess_exc}"
                    self._record_event(job, "PREPROCESS_EXCEPTION", {"message": reason})
                    self._set_terminal(job, "failed", reason)
                return

            if self._is_cancelled(job):
                self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                return
            if not isinstance(manifest, dict):
                self._set_terminal(job, "failed", "전처리가 manifest 객체를 반환하지 않았습니다")
                return

            with job.lock:
                job.manifest = manifest
                job.record["updated_at"] = _utc_now()
            if job_ctx is not None:
                job_ctx["manifest"] = manifest
            self._persist_job(job)

            preprocess_failure = self._preprocess_failure_reason(manifest)
            if preprocess_failure is not None:
                self._record_event(job, "PREPROCESS_FAILED", {"reason": preprocess_failure})
                self._set_terminal(job, "failed", preprocess_failure)
                return

            uploads = manifest.get("uploads")
            upload_count = len(uploads) if isinstance(uploads, list) else 0
            self._record_event(job, "PREPROCESS_DONE", {"uploads": upload_count})
            if self._fail_if_job_timeout(job, job_start):
                return

            try:
                stage_list = list(stages_builder(manifest))
            except Exception as exc:  # noqa: BLE001 - adapter/prompt failures are job failures.
                if self._is_cancelled(job):
                    self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                else:
                    reason = f"스테이지 생성 중 예외가 발생했습니다: {type(exc).__name__}: {exc}"
                    self._record_event(job, "STAGE_BUILD_EXCEPTION", {"message": reason})
                    self._set_terminal(job, "failed", reason)
                return

            with job.lock:
                job.stages = stage_list
                job.record["stages_total"] = len(stage_list)
                job.record["updated_at"] = _utc_now()
            self._persist_job(job)
            self._record_event(job, "STAGES_BUILT", {"stages_total": len(stage_list)})
            self._run_stages(job, job_start)
        except Exception as exc:  # noqa: BLE001 - background threads must persist failures, not crash silently.
            self._handle_job_exception(job, exc)
        finally:
            self._finish_job(job)

    def _run_preprocess_with_deadline(
        self,
        job: _JobState,
        preprocess_fn: Callable[[], dict[str, Any]],
        job_start: float,
    ) -> tuple[bool, Any, Exception | None]:
        result: dict[str, Any] = {}

        def run_preprocess() -> None:
            try:
                result["manifest"] = preprocess_fn()
            except Exception as exc:  # noqa: BLE001 - preprocessing failures become job failures.
                result["exception"] = exc

        preprocess_thread = threading.Thread(
            target=run_preprocess,
            daemon=True,
            name=f"job-preprocess-{job.id}",
        )
        preprocess_thread.start()

        deadline = job_start + job.timeout_seconds
        while preprocess_thread.is_alive():
            if self._is_cancelled(job):
                self._terminate_active_processes(job, "cancelled")
                self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                return False, None, None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f"작업 전체 제한 시간({job.timeout_seconds}초)을 초과했습니다"
                self._record_event(
                    job,
                    "JOB_TIMEOUT",
                    {
                        "timeout_seconds": job.timeout_seconds,
                        "phase": "preprocess",
                        "reason": reason,
                    },
                )
                self._terminate_active_processes(job, "job_timeout")
                self._set_terminal(job, "failed", reason)
                return False, None, None

            preprocess_thread.join(timeout=min(1.0, remaining))

        if self._is_cancelled(job):
            self._terminate_active_processes(job, "cancelled")
            self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
            return False, None, None
        if self._fail_if_job_timeout(job, job_start):
            self._terminate_active_processes(job, "job_timeout")
            return False, None, None

        exception = result.get("exception")
        if isinstance(exception, Exception):
            return True, None, exception
        return True, result.get("manifest"), None

    def _run_stages(self, job: _JobState, job_start: float) -> None:
        for index, stage in enumerate(job.stages):
            if self._is_cancelled(job):
                self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
                return
            if self._fail_if_job_timeout(job, job_start):
                return

            status = "preprocessing" if stage.kind == "preprocess" else "running"
            self._set_status(job, status, current_stage=stage.id, stage_index=index)
            ok, terminal_status, reason = self._run_stage(job, stage, job_start)
            if terminal_status == "cancelled":
                self._set_terminal(job, "cancelled", reason or "사용자가 작업을 취소했습니다")
                return
            if not ok:
                self._set_terminal(job, "failed", reason or "스테이지 실행에 실패했습니다")
                return

            manifest_error = self._refresh_manifest(job)
            if manifest_error is not None:
                self._set_terminal(job, "failed", manifest_error)
                return

            ok, reason = self._run_validators(job, stage)
            if not ok:
                self._set_terminal(job, "failed", reason or "스테이지 검증에 실패했습니다")
                return

        if self._is_cancelled(job):
            self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
            return
        if self._fail_if_job_timeout(job, job_start):
            return

        result_files = self._collect_result_files(job)
        with job.lock:
            job.record["result_files"] = result_files
            job.record["updated_at"] = _utc_now()
        self._persist_job(job)
        self._set_terminal(job, "done", None)
        self._record_event(job, "DONE", {"result_files": result_files})

    def _handle_job_exception(self, job: _JobState, exc: Exception) -> None:
        if self._is_cancelled(job):
            self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
        else:
            self._record_event(job, "EXCEPTION", {"message": f"{type(exc).__name__}: {exc}"})
            self._set_terminal(job, "failed", f"작업 실행 중 예외가 발생했습니다: {type(exc).__name__}: {exc}")

    def _finish_job(self, job: _JobState) -> None:
        self._terminate_active_processes(job, "job_finished")
        with job.lock:
            job.process = None
            job.active_processes.clear()
        with self._lock:
            if self._active_job_id == job.id:
                self._active_job_id = None

    def _preprocess_failure_reason(self, manifest: dict[str, Any]) -> str | None:
        return preprocess_failure_reason(manifest)


    def _fail_if_job_timeout(self, job: _JobState, job_start: float) -> bool:
        if time.monotonic() - job_start < job.timeout_seconds:
            return False
        if self._is_cancelled(job):
            self._set_terminal(job, "cancelled", "사용자가 작업을 취소했습니다")
            return True
        reason = f"작업 전체 제한 시간({job.timeout_seconds}초)을 초과했습니다"
        self._record_event(
            job,
            "JOB_TIMEOUT",
            {"timeout_seconds": job.timeout_seconds, "reason": reason},
        )
        self._set_terminal(job, "failed", reason)
        return True

    def _run_stage(self, job: _JobState, stage: Stage, job_start: float) -> tuple[bool, str | None, str | None]:
        details = {"stage_id": stage.id, "kind": stage.kind, "owner": stage.owner, "cwd": stage.cwd}
        self._record_event(job, "STAGE_START", details)
        if stage.owner == "agent":
            self._record_event(job, "AGENT_STAGE_MONITOR_ATTACHED", {"stage_id": stage.id})

        if not stage.command:
            self._record_event(job, "STAGE_NO_COMMAND", {"stage_id": stage.id})
            return True, None, None

        env = os.environ.copy()
        env.update({key: str(value) for key, value in stage.env.items()})
        env["PYTHONUTF8"] = "1"
        stage_timeout = stage.timeout_seconds if stage.timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS
        state: dict[str, Any] = {
            "last_output_time": time.monotonic(),
            "idle_warned": False,
        }
        state_lock = threading.Lock()

        launch_command = resolve_launch_command(stage.command)
        stdin_mode = subprocess.PIPE if stage.stdin_data is not None else subprocess.DEVNULL
        try:
            process = self._runner_managed_popen(
                job,
                launch_command,
                cwd=stage.cwd,
                env=env,
                stdin=stdin_mode,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **process_group_popen_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001 - preserve startup failure for users.
            reason = f"스테이지 시작 실패({stage.id}): {type(exc).__name__}: {exc}"
            self._record_event(job, "STAGE_START_FAILED", {"stage_id": stage.id, "message": reason})
            return False, None, reason


        assert process.stdout is not None
        output_thread = threading.Thread(
            target=self._stream_output,
            args=(job, process.stdout, state, state_lock),
            daemon=True,
            name=f"job-output-{job.id}-{stage.id}",
        )
        output_thread.start()
        if stage.stdin_data is not None and process.stdin is not None:
            try:
                process.stdin.write(stage.stdin_data)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        stage_start = time.monotonic()
        kill_reason: str | None = None
        try:
            while True:
                now = time.monotonic()
                if process.poll() is not None:
                    break
                if self._is_cancelled(job):
                    kill_reason = "cancelled"
                    self._terminate_process_group(job, process, kill_reason)
                    break

                stage_elapsed = now - stage_start
                job_elapsed = now - job_start
                with state_lock:
                    idle_seconds = now - float(state["last_output_time"])
                    idle_warned = bool(state["idle_warned"])

                if job_elapsed >= job.timeout_seconds:
                    kill_reason = "job_timeout"
                    self._record_event(job, "JOB_TIMEOUT", {"timeout_seconds": job.timeout_seconds})
                    self._terminate_process_group(job, process, kill_reason)
                    break
                if stage_elapsed >= stage_timeout:
                    kill_reason = "timeout"
                    self._record_event(job, "TIMEOUT", {"stage_id": stage.id, "timeout_seconds": stage_timeout})
                    self._terminate_process_group(job, process, kill_reason)
                    break
                if idle_seconds >= STALL_IDLE_SECONDS:
                    kill_reason = "stall"
                    self._record_event(job, "STALL", {"stage_id": stage.id, "idle_seconds": round(idle_seconds, 3)})
                    self._terminate_process_group(job, process, kill_reason)
                    break
                if idle_seconds >= WARN_IDLE_SECONDS and not idle_warned:
                    with state_lock:
                        state["idle_warned"] = True
                    self._record_event(job, "WARN_NO_OUTPUT", {"stage_id": stage.id, "idle_seconds": round(idle_seconds, 3)})

                time.sleep(0.5)

            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                kill_reason = kill_reason or "wait_timeout"
                self._record_event(job, "WAIT_TIMEOUT", {"stage_id": stage.id})
                self._terminate_process_group(job, process, kill_reason)

            output_thread.join(timeout=2)
        finally:
            self._unregister_process(job, process)

        self._record_event(job, "PROCESS_EXIT", {"stage_id": stage.id, "exit_code": process.returncode})
        if kill_reason == "cancelled" or self._is_cancelled(job):
            return False, "cancelled", "사용자가 작업을 취소했습니다"
        if kill_reason == "job_timeout":
            return False, None, f"작업 전체 제한 시간({job.timeout_seconds}초)을 초과했습니다"
        if kill_reason == "timeout":
            return False, None, f"스테이지 제한 시간({stage_timeout}초)을 초과했습니다: {stage.id}"
        if kill_reason == "stall":
            return False, None, f"{STALL_IDLE_SECONDS}초 동안 출력이 없어 스테이지를 중지했습니다: {stage.id}"
        if kill_reason is not None:
            return False, None, f"스테이지가 중지되었습니다({kill_reason}): {stage.id}"
        if process.returncode != 0:
            return False, None, f"스테이지가 실패했습니다({stage.id}, exit_code={process.returncode})"

        self._record_event(job, "STAGE_DONE", {"stage_id": stage.id})
        return True, None, None

    def _run_validators(self, job: _JobState, stage: Stage) -> tuple[bool, str | None]:
        if not stage.validators:
            return True, None

        ctx = self._job_ctx(job, stage)
        for index, validator in enumerate(stage.validators):
            try:
                result = validator(ctx)
            except Exception as exc:  # noqa: BLE001 - validator exceptions are failed validations.
                reason = f"검증기 예외({stage.id} #{index}): {type(exc).__name__}: {exc}"
                self._record_event(job, "VALIDATOR_EXCEPTION", {"stage_id": stage.id, "validator_index": index, "message": reason})
                return False, reason

            if not isinstance(result, ValidationResult):
                reason = f"검증기가 ValidationResult를 반환하지 않았습니다: {stage.id} #{index}"
                self._record_event(job, "VALIDATOR_INVALID_RESULT", {"stage_id": stage.id, "validator_index": index})
                return False, reason
            if not result.ok:
                reason = result.reason or "검증 실패"
                self._record_event(job, "VALIDATOR_FAILED", {"stage_id": stage.id, "validator_index": index, "reason": reason})
                return False, reason
            self._record_event(job, "VALIDATOR_OK", {"stage_id": stage.id, "validator_index": index})
        return True, None

    def _stream_output(self, job: _JobState, stream: TextIO, state: dict[str, Any], state_lock: threading.Lock) -> None:
        try:
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\n")
                with state_lock:
                    state["last_output_time"] = time.monotonic()
                    state["idle_warned"] = False
                self._write_log(job, f"[output] {line}")
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _runner_managed_popen(self, job: _JobState, command: Any, *popen_args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        for key, value in process_group_popen_kwargs().items():
            kwargs.setdefault(key, value)
        process = subprocess.Popen(command, *popen_args, **kwargs)
        self._register_process(job, process, command)
        return process

    def _register_process(self, job: _JobState, process: subprocess.Popen[str], command: Any) -> None:
        with job.lock:
            job.process = process
            job.active_processes.append(process)
            stage_id = job.record.get("current_stage")
        self._record_event(job, "PROCESS_START", {"stage_id": stage_id, "pid": process.pid, "command": _display_command(command)})

    def _unregister_process(self, job: _JobState, process: subprocess.Popen[str]) -> None:
        with job.lock:
            live_processes = [
                candidate
                for candidate in job.active_processes
                if candidate is not process and candidate.poll() is None
            ]
            job.active_processes = live_processes
            if job.process is process or (job.process is not None and job.process.poll() is not None):
                job.process = live_processes[-1] if live_processes else None

    def _terminate_active_processes(self, job: _JobState, reason: str) -> None:
        with job.lock:
            processes = list(job.active_processes)
            if job.process is not None and job.process not in processes:
                processes.append(job.process)

        for process in processes:
            if process.poll() is None:
                self._terminate_process_group(job, process, reason)

    def _terminate_process_group(self, job: _JobState, process: subprocess.Popen[str], reason: str) -> None:
        if process.poll() is not None:
            return
        self._record_event(job, "TERMINATE", {"pid": process.pid, "reason": reason})
        terminate_process_tree(process)

    def _collect_result_files(self, job: _JobState) -> list[str]:
        return collect_result_files(job.stages, job.workspace)

    def _refresh_manifest(self, job: _JobState) -> str | None:
        if not (job.workspace / "manifest.json").exists():
            return None
        try:
            manifest = refresh_manifest(job.workspace)
        except ValueError as exc:
            reason = f"manifest.json을 읽을 수 없습니다: {exc}"
            self._record_event(job, "MANIFEST_READ_FAILED", {"message": reason})
            return reason
        with job.lock:
            job.manifest = manifest
        return None

    def _load_manifest(self, workspace: Path) -> dict[str, Any] | None:
        return load_manifest(workspace)

    def _job_ctx(self, job: _JobState, stage: Stage | None = None) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "job_id": job.id,
            "workspace": str(job.workspace),
            "raw_dir": str(job.raw_dir),
            "project_dir": str(job.project_dir),
            "manifest": job.manifest,
            "cancel_event": job.cancel_event,
            "runner_managed_popen": lambda command, *args, **kwargs: self._runner_managed_popen(job, command, *args, **kwargs),
        }
        if stage is not None:
            ctx["cwd"] = stage.cwd
            ctx["stage_cwd"] = stage.cwd
        return ctx

    def _set_status(
        self,
        job: _JobState,
        status: str,
        current_stage: str | None = None,
        stage_index: int | None = None,
    ) -> None:
        with job.lock:
            if job.record["status"] == "cancelled" and status != "cancelled":
                return
            job.record["status"] = status
            job.record["current_stage"] = current_stage
            job.record["stage_index"] = stage_index
            job.record["updated_at"] = _utc_now()
        self._persist_job(job)
        self._record_event(job, "STATUS", {"status": status, "current_stage": current_stage})

    def _set_terminal(self, job: _JobState, status: str, reason: str | None) -> None:
        with job.lock:
            existing = job.record["status"]
            if existing in TERMINAL_STATUSES and existing != status:
                return
            job.record["status"] = status
            job.record["current_stage"] = None
            job.record["stage_index"] = None
            job.record["reason"] = reason
            job.record["updated_at"] = _utc_now()
        self._persist_job(job)
        details: dict[str, Any] = {"status": status}
        if reason:
            details["reason"] = reason
        self._record_event(job, "TERMINAL", details)

    def _record_event(self, job: _JobState, event: str, details: dict[str, Any] | None = None) -> None:
        item: dict[str, Any] = {"ts": _utc_now(), "event": event}
        if details:
            item.update(details)
        with job.lock:
            job.record["events"].append(item)
            job.record["updated_at"] = item["ts"]
        suffix = f" {json.dumps(details, ensure_ascii=False, sort_keys=True)}" if details else ""
        self._write_log(job, f"[EVENT] {event}{suffix}")
        self._persist_job(job)

    def _write_log(self, job: _JobState, message: str) -> None:
        line = f"[{_utc_now()}] {message}\n"
        with job.log_lock:
            job.log_path.parent.mkdir(parents=True, exist_ok=True)
            with job.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _persist_job(self, job: _JobState) -> None:
        with job.lock:
            payload = dict(job.record)
        self._store.persist(job.id, payload, job.persist_lock)

    def _is_cancelled(self, job: _JobState) -> bool:
        with job.lock:
            return job.cancel_requested or job.cancel_event.is_set() or job.record["status"] == "cancelled"

    def _get_job(self, job_id: str) -> _JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _load_record(self, job_id: str) -> dict[str, Any] | None:
        return self._store.load_record(job_id)

    def _log_path_for(self, job_id: str) -> Path | None:
        job = self._get_job(job_id)
        if job is not None:
            return job.log_path
        record = self._load_record(job_id)
        if record is not None and record.get("log_path"):
            return Path(str(record["log_path"]))
        if not _JOB_ID_RE.fullmatch(job_id):
            return None
        return self.workspace_root / job_id / "job.log"

    def _missing_status(self, job_id: str) -> dict[str, Any]:
        return {"job_id": job_id, "id": job_id, "status": "missing", "reason": "작업을 찾을 수 없습니다"}

    def _infer_job_id(self, stages: list[Stage]) -> str | None:
        root = self.workspace_root.resolve(strict=False)
        candidates: set[str] = set()
        for stage in stages:
            try:
                cwd = Path(stage.cwd).resolve(strict=False)
                relative = cwd.relative_to(root)
            except (OSError, ValueError):
                continue
            if relative.parts:
                candidates.add(relative.parts[0])
        if len(candidates) > 1:
            raise ValueError("여러 작업 workspace를 가리키는 stage.cwd가 섞여 있습니다: " + ", ".join(sorted(candidates)))
        return next(iter(candidates), None)

    def _new_job_id(self) -> str:
        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

    def _validate_job_id(self, job_id: str) -> None:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"유효하지 않은 job_id입니다: {job_id!r}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")




def _display_command(command: Any) -> Any:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return str(command)
