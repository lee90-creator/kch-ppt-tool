from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any, Callable

from app import server as server_module
from app import job_runner as job_runner_module
from app.images.intake import intake_images
from app.job_runner import BusyError, DEFAULT_TIMEOUT_SECONDS, JobRunner
from app.job_store import JobStore
from app.results import collect_result_files
from app.paths import resolve_ppt_master_root
from app.preprocess import preprocess_job
from app.stage_contract import (
    Stage,
    ValidationResult,
    make_outputs_exist_validator,
    make_pptx_valid_validator,
    make_raw_absent_validator,
)
from app.storage import History, Settings


TERMINAL_STATUSES = {"done", "failed", "cancelled"}
EXPECTED_PPTX = ["projects/*/exports/*.pptx"]
FAKE_CLI = Path(__file__).with_name("fake_cli.py").resolve()
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeServerAdapter:
    name = "fake"

    def build_stages(self, job_ctx: dict[str, Any], _prompt: str) -> list[Stage]:
        expected_outputs = list(EXPECTED_PPTX)
        return [
            Stage(
                id="fake_agent_oneshot",
                kind="agent",
                owner="agent",
                command=[sys.executable, str(FAKE_CLI), "success"],
                cwd=job_ctx["project_dir"],
                env={},
                expected_outputs=expected_outputs,
                validators=[
                    make_outputs_exist_validator(expected_outputs),
                    make_pptx_valid_validator(EXPECTED_PPTX[0]),
                    make_raw_absent_validator(),
                ],
                timeout_seconds=30,
            )
        ]


class ImageEnvServerAdapter(server_module.CliAdapter):
    name = "fake"
    EXPECTED_OUTPUTS = EXPECTED_PPTX
    PPTX_OUTPUT_GLOB = EXPECTED_PPTX[0]
    TIMEOUT_SECONDS = 30
    captured_envs: list[dict[str, str]] = []

    def _build_command(self, _prompt: str, _job_ctx: dict[str, Any] | None = None) -> list[str]:
        return [sys.executable, str(FAKE_CLI), "success"]

    def build_stages(self, job_ctx: dict[str, Any], prompt: str) -> list[Stage]:
        stages = super().build_stages(job_ctx, prompt)
        self.captured_envs.append(dict(stages[0].env))
        return stages

def _ok_manifest(workspace: str | Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": Path(workspace).name,
        "uploads": [],
        "preprocess": {"status": "ok", "detail": "test preprocess ok"},
    }


class IntegrationTest(unittest.TestCase):
    def _runner(self, root: Path) -> JobRunner:
        return JobRunner(root / "workspace", root / "data")

    def _project_dir(self, runner: JobRunner, job_id: str) -> Path:
        return runner.workspace_root / job_id / "project"

    def _fake_stage(
        self,
        runner: JobRunner,
        job_id: str,
        mode: str,
        *,
        validators: list[Callable[[dict[str, Any]], ValidationResult]] | None = None,
        timeout_seconds: int = 30,
    ) -> Stage:
        return Stage(
            id=f"fake_{mode}",
            kind="export",
            owner="script",
            command=[sys.executable, str(FAKE_CLI), mode],
            cwd=str(self._project_dir(runner, job_id)),
            env={},
            expected_outputs=list(EXPECTED_PPTX),
            validators=list(validators or []),
            timeout_seconds=timeout_seconds,
        )

    def _wait_for(
        self,
        runner: JobRunner,
        job_id: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_status = runner.get_status(job_id)
        while time.monotonic() < deadline:
            last_status = runner.get_status(job_id)
            if predicate(last_status):
                return last_status
            time.sleep(0.1)
        self.fail(f"timed out waiting for {job_id}; last status={last_status}")

    def _wait_terminal(self, runner: JobRunner, job_id: str, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
        return self._wait_for(
            runner,
            job_id,
            lambda status: status.get("status") in TERMINAL_STATUSES,
            timeout_seconds=timeout_seconds,
        )

    def _join_job_thread(self, runner: JobRunner, job_id: str, *, timeout_seconds: float = 5.0) -> None:
        job = runner._jobs.get(job_id)  # Integration coverage for background cleanup.
        thread = getattr(job, "thread", None)
        if thread is None:
            return
        thread.join(timeout_seconds)
        self.assertFalse(thread.is_alive(), f"job thread did not stop for {job_id}")

    def _pid_is_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _post_fake_job(self, client: Any, *, filename: str = "pixel.png") -> Any:
        return client.post(
            "/api/jobs",
            data={
                "request_text": "make a deck",
                "image_source": "none",
                "cli": "fake",
                "files": (io.BytesIO(PNG_1X1), filename),
            },
            content_type="multipart/form-data",
        )

    def test_job_success_generates_results_and_validators_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            job_id = "success-job"
            validators = [
                make_outputs_exist_validator(EXPECTED_PPTX),
                make_pptx_valid_validator(EXPECTED_PPTX[0]),
            ]
            stage = self._fake_stage(runner, job_id, "success", validators=validators)

            runner.submit([stage], job_id=job_id)
            self._wait_terminal(runner, job_id)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)

            self.assertEqual(final["status"], "done")
            result_files = final.get("result_files")
            self.assertIsInstance(result_files, list)
            self.assertTrue(result_files)
            workspace = runner.workspace_root / job_id
            for result_file in result_files:
                self.assertTrue((workspace / str(result_file)).is_file(), result_file)

            ctx = {"cwd": str(self._project_dir(runner, job_id)), "project_dir": str(self._project_dir(runner, job_id))}
            for validator in validators:
                result = validator(ctx)
                self.assertTrue(result.ok, result.reason)

    def test_job_failure_records_exit_code_and_keeps_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            job_id = "fail-job"
            stage = self._fake_stage(runner, job_id, "fail")

            runner.submit([stage], job_id=job_id)
            self._wait_terminal(runner, job_id)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)

            self.assertEqual(final["status"], "failed")
            self.assertIn("exit_code=3", str(final.get("reason")))
            log_path = Path(str(final["log_path"]))
            self.assertTrue(log_path.is_file())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("PROCESS_EXIT", log_text)
            self.assertIn("exit_code", log_text)

    def test_cancel_hang_job_preserves_workspace_and_stops_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            job_id = "cancel-job"
            stage = self._fake_stage(runner, job_id, "hang", timeout_seconds=60)

            runner.submit([stage], job_id=job_id)
            running = self._wait_for(
                runner,
                job_id,
                lambda status: any(event.get("event") == "PROCESS_START" for event in status.get("events", [])),
                timeout_seconds=10,
            )
            pids = [event["pid"] for event in running["events"] if event.get("event") == "PROCESS_START"]
            self.assertTrue(pids)

            cancelled = runner.cancel(job_id)
            self.assertEqual(cancelled["status"], "cancelled")
            self._wait_terminal(runner, job_id)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)

            self.assertEqual(final["status"], "cancelled")
            self.assertTrue(Path(str(final["workspace"])).is_dir())
            events = final.get("events", [])
            self.assertTrue(any(event.get("event") == "TERMINATE" for event in events))
            self.assertTrue(any(event.get("event") == "PROCESS_EXIT" for event in events))
            for pid in pids:
                self.assertFalse(self._pid_is_running(int(pid)), f"process still exists: {pid}")

    def test_running_job_heartbeat_updates_idle_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(Path(temp_dir))
            job_id = "heartbeat-job"
            stage = self._fake_stage(runner, job_id, "hang", timeout_seconds=30)
            try:
                with patch.object(job_runner_module, "HEARTBEAT_INTERVAL_SECONDS", 0.05):
                    runner.submit([stage], job_id=job_id)
                    status = self._wait_for(
                        runner,
                        job_id,
                        lambda current: int((current.get("progress") or {}).get("idle_seconds") or 0) >= 1,
                        timeout_seconds=5,
                    )
                progress = status["progress"]
                self.assertEqual(progress["phase"], "generating")
                self.assertIn("실행 중", progress["detail"])
                self.assertTrue(progress["last_activity_at"])
            finally:
                runner.cancel(job_id)
                self._wait_terminal(runner, job_id)
                self._join_job_thread(runner, job_id)

    def test_hang_job_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            job_id = "timeout-job"
            stage = self._fake_stage(runner, job_id, "hang", timeout_seconds=5)

            runner.submit([stage], job_id=job_id)
            self._wait_terminal(runner, job_id, timeout_seconds=15)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)

            self.assertEqual(final["status"], "failed")
            self.assertIn("제한 시간", str(final.get("reason")))
            self.assertIn("5", str(final.get("reason")))
            self.assertTrue(any(event.get("event") == "TIMEOUT" for event in final.get("events", [])))

    def test_only_one_job_can_run_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            first_job_id = "busy-first"
            second_job_id = "busy-second"
            first_stage = self._fake_stage(runner, first_job_id, "hang", timeout_seconds=60)
            second_stage = self._fake_stage(runner, second_job_id, "success")

            runner.submit([first_stage], job_id=first_job_id)
            try:
                self._wait_for(
                    runner,
                    first_job_id,
                    lambda status: status.get("status") == "running",
                    timeout_seconds=10,
                )
                with self.assertRaises(BusyError):
                    runner.submit([second_stage], job_id=second_job_id)
            finally:
                runner.cancel(first_job_id)
                self._wait_terminal(runner, first_job_id)
                self._join_job_thread(runner, first_job_id)

    def test_api_rejects_second_job_while_preprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            Settings(data_dir).accept_notice()
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()
            entered = threading.Event()
            release = threading.Event()

            def slow_preprocess(workspace: str, _uploads: list[tuple[str, str]], **_kwargs: Any) -> dict[str, Any]:
                entered.set()
                self.assertTrue(release.wait(10), "slow preprocess was not released")
                return _ok_manifest(workspace)

            with (
                patch.object(server_module, "ADAPTER_CLASSES", {"fake": FakeServerAdapter}),
                patch.object(server_module, "detect_all", return_value={"fake": {"available": True}}),
                patch.object(server_module, "preprocess_job", side_effect=slow_preprocess),
            ):
                first = self._post_fake_job(client)
                self.assertEqual(first.status_code, 202)
                job_id = first.get_json()["job_id"]
                runner = app.extensions["ppt_webtool"]["runner"]()
                self.assertTrue(entered.wait(5), "preprocess did not start")

                second = self._post_fake_job(client, filename="second.png")
                self.assertEqual(second.status_code, 503)
                release.set()

            final = self._wait_terminal(runner, job_id, timeout_seconds=20)
            self._join_job_thread(runner, job_id)
            self.assertEqual(final["status"], "done")

    def test_api_cancel_during_preprocessing_stops_active_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            Settings(data_dir).accept_notice()
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()
            child_started = threading.Event()
            child_pids: list[int] = []

            def child_preprocess(
                workspace: str,
                _uploads: list[tuple[str, str]],
                *,
                cancel_event: Any | None = None,
                popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
            ) -> dict[str, Any]:
                self.assertIsNotNone(popen_factory)
                assert popen_factory is not None
                process = popen_factory(
                    [sys.executable, "-c", "import time; time.sleep(3600)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                child_pids.append(process.pid)
                child_started.set()
                process.communicate()
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                return _ok_manifest(workspace)

            with (
                patch.object(server_module, "ADAPTER_CLASSES", {"fake": FakeServerAdapter}),
                patch.object(server_module, "detect_all", return_value={"fake": {"available": True}}),
                patch.object(server_module, "preprocess_job", side_effect=child_preprocess),
            ):
                response = self._post_fake_job(client)
                self.assertEqual(response.status_code, 202)
                job_id = response.get_json()["job_id"]
                runner = app.extensions["ppt_webtool"]["runner"]()
                self.assertTrue(child_started.wait(5), "preprocess child did not start")
                processing = self._wait_for(
                    runner,
                    job_id,
                    lambda status: status.get("status") == "preprocessing"
                    and any(event.get("event") == "PROCESS_START" for event in status.get("events", [])),
                    timeout_seconds=10,
                )
                pids = child_pids or [event["pid"] for event in processing["events"] if event.get("event") == "PROCESS_START"]

                cancel_response = client.post(f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancel_response.status_code, 200)
                self.assertEqual(cancel_response.get_json()["status"], "cancelled")

            final = self._wait_terminal(runner, job_id, timeout_seconds=10)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)
            self.assertEqual(final["status"], "cancelled")
            events = final.get("events", [])
            self.assertTrue(any(event.get("event") == "TERMINATE" for event in events))
            for pid in pids:
                self.assertFalse(self._pid_is_running(int(pid)), f"preprocess child still exists: {pid}")

    def test_pipeline_preprocess_timeout_kills_child_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = self._runner(root)
            job_ctx: dict[str, Any] = {"job_timeout_seconds": 3}
            job_id = "preprocess-timeout-job"
            child_started = threading.Event()
            child_pids: list[int] = []

            def stuck_preprocess() -> dict[str, Any]:
                popen_factory = job_ctx.get("runner_managed_popen")
                self.assertTrue(callable(popen_factory))
                assert callable(popen_factory)
                process = popen_factory(
                    [sys.executable, "-c", "import time; time.sleep(3600)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                child_pids.append(process.pid)
                child_started.set()
                time.sleep(30)
                return _ok_manifest(str(job_ctx["workspace"]))

            def build_no_stages(_manifest: dict[str, Any]) -> list[Stage]:
                return []

            submitted_job_id = runner.submit_pipeline(
                preprocess_fn=stuck_preprocess,
                stages_builder=build_no_stages,
                job_id=job_id,
                job_ctx=job_ctx,
            )
            self.assertEqual(submitted_job_id, job_id)
            self.assertTrue(child_started.wait(5), "preprocess child did not start")

            self._wait_terminal(runner, job_id, timeout_seconds=10)
            self._join_job_thread(runner, job_id)
            final = runner.get_status(job_id)

            self.assertEqual(final["status"], "failed")
            self.assertIn("작업 전체 제한 시간", str(final.get("reason")))
            events = final.get("events", [])
            self.assertTrue(any(event.get("event") == "JOB_TIMEOUT" for event in events))
            self.assertTrue(
                any(
                    event.get("event") == "TERMINATE" and event.get("reason") == "job_timeout"
                    for event in events
                )
            )
            self.assertTrue(child_pids)
            for pid in child_pids:
                self.assertFalse(self._pid_is_running(int(pid)), f"preprocess child still exists: {pid}")

            next_job_id = "after-preprocess-timeout"
            runner.submit([], job_id=next_job_id)
            next_final = self._wait_terminal(runner, next_job_id, timeout_seconds=5)
            self._join_job_thread(runner, next_job_id)
            self.assertEqual(next_final["status"], "done")

    def test_ppt_master_path_prefers_bundled_root_then_falls_back_to_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            webtool_root = parent / "ppt-webtool"
            source_root = parent / "ppt-master"
            webtool_root.mkdir()
            source_root.mkdir()

            self.assertEqual(resolve_ppt_master_root(webtool_root), source_root)

            bundled_root = webtool_root / "ppt-master"
            bundled_root.mkdir()
            self.assertEqual(resolve_ppt_master_root(webtool_root), bundled_root)

    def test_pipeline_timeout_override_is_captured_per_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(Path(temp_dir))

            def preprocess() -> dict[str, Any]:
                return _ok_manifest("unused")

            def build_no_stages(_manifest: dict[str, Any]) -> list[Stage]:
                return []

            first_job_id = runner.submit_pipeline(
                preprocess_fn=preprocess,
                stages_builder=build_no_stages,
                job_id="first-timeout",
                job_ctx={"job_timeout_seconds": 17},
            )
            self._wait_terminal(runner, first_job_id)
            self._join_job_thread(runner, first_job_id)

            second_job_id = runner.submit_pipeline(
                preprocess_fn=preprocess,
                stages_builder=build_no_stages,
                job_id="second-timeout",
            )
            self._wait_terminal(runner, second_job_id)
            self._join_job_thread(runner, second_job_id)

            self.assertEqual(runner._jobs[first_job_id].timeout_seconds, 17)
            self.assertEqual(runner._jobs[second_job_id].timeout_seconds, DEFAULT_TIMEOUT_SECONDS)
            first_start = next(event for event in runner.get_status(first_job_id)["events"] if event["event"] == "START")
            second_start = next(event for event in runner.get_status(second_job_id)["events"] if event["event"] == "START")
            self.assertEqual(first_start["job_timeout_seconds"], 17)
            self.assertEqual(second_start["job_timeout_seconds"], DEFAULT_TIMEOUT_SECONDS)

    def test_port_probe_skips_an_existing_listener(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((server_module.HOST, 0))
            listener.listen()
            occupied_port = int(listener.getsockname()[1])
            selected = server_module._find_available_port(
                host=server_module.HOST,
                start_port=occupied_port,
                max_increment=10,
            )

        self.assertNotEqual(selected, occupied_port)
        self.assertGreater(selected, occupied_port)

    def test_existing_instance_action_reuses_same_release_and_blocks_conflict(self) -> None:
        current_root = Path("/tmp/current-release")
        same = {
            "version": "0.0.5",
            "instance_root": str(current_root),
            "_url": "http://127.0.0.1:8765",
        }
        conflict = {
            "version": "0.0.4",
            "instance_root": "/tmp/old-release",
            "_url": "http://127.0.0.1:8765",
        }

        self.assertEqual(
            server_module._existing_instance_action(
                same,
                current_version="0.0.5",
                current_root=current_root,
            ),
            ("reuse", "http://127.0.0.1:8765"),
        )
        self.assertEqual(
            server_module._existing_instance_action(
                conflict,
                current_version="0.0.5",
                current_root=current_root,
            ),
            ("conflict", "http://127.0.0.1:8765"),
        )
    def test_server_instance_lock_is_exclusive_and_preserves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "server.lock"
            first = server_module.ServerInstanceLock(lock_path)
            second = server_module.ServerInstanceLock(lock_path)
            self.assertTrue(first.acquire())
            try:
                identity = {"version": "0.0.5", "_url": "http://127.0.0.1:8765"}
                first.write_identity(identity)
                self.assertFalse(second.acquire())
                self.assertEqual(second.read_identity(), identity)
            finally:
                first.release()

            self.assertTrue(second.acquire())
            second.release()

    def test_existing_release_probe_checks_secondary_ports(self) -> None:
        def fake_probe(*, host: str, port: int, timeout_seconds: float) -> dict[str, Any] | None:
            self.assertEqual(host, server_module.HOST)
            self.assertGreater(timeout_seconds, 0)
            if port == server_module.DEFAULT_PORT + 2:
                return {"version": "0.0.4", "_url": f"http://{host}:{port}"}
            return None

        with (
            patch.object(server_module, "_port_has_listener", return_value=True),
            patch.object(server_module, "_probe_existing_instance", side_effect=fake_probe) as probe,
        ):
            existing = server_module._probe_existing_release(max_increment=3)

        self.assertEqual(existing["version"], "0.0.4")
        self.assertEqual(probe.call_count, 3)

    def test_identity_api_does_not_detect_cli_and_shutdown_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app = server_module.create_app(data_dir=root / "data", workspace_root=root / "workspace")
            client = app.test_client()

            with patch.object(server_module, "detect_all", side_effect=AssertionError("CLI detection must not run")):
                identity_response = client.get("/api/identity")

            self.assertEqual(identity_response.status_code, 200)
            self.assertEqual(identity_response.get_json()["app_id"], "kch-ppt-tool")
            self.assertEqual(client.post("/api/shutdown").status_code, 403)

            token = app.config["INSTANCE_TOKEN"]
            current_runner = app.extensions["ppt_webtool"]["runner"]()
            with patch.object(current_runner, "is_busy", return_value=True):
                busy_response = client.post("/api/shutdown", headers={"X-KCH-Instance": token})
            self.assertEqual(busy_response.status_code, 409)

            shutdown_called = threading.Event()
            app.extensions["ppt_webtool"]["shutdown_server"] = shutdown_called.set
            response = client.post("/api/shutdown", headers={"X-KCH-Instance": token})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(shutdown_called.wait(1))

    def test_log_tail_starts_at_line_boundary_and_chunking_loses_no_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(Path(temp_dir))
            job_id = "large-log-job"
            log_path = runner.workspace_root / job_id / "job.log"
            log_path.parent.mkdir(parents=True)
            expected = [f"line-{index:05d}" for index in range(10000)]
            log_path.write_text("\n".join(expected) + "\n", encoding="utf-8")

            offset = runner.initial_log_offset(job_id, 4096)
            received: list[str] = []
            while True:
                next_offset, chunk = runner.iter_log(job_id, offset)
                received.extend(chunk)
                if next_offset == offset:
                    break
                offset = next_offset

            self.assertTrue(received)
            start = expected.index(received[0])
            self.assertEqual(received, expected[start:])


    def test_artifact_progress_reports_image_and_slide_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir) / "project"
            deck = project_dir / "projects" / "deck"
            images = deck / "images"
            svg_output = deck / "svg_output"
            svg_final = deck / "svg_final"
            images.mkdir(parents=True)
            svg_output.mkdir()
            svg_final.mkdir()
            (deck / "exports").mkdir()
            (deck / "design_spec.md").write_text(
                "| **Page Count** | 9 |\n",
                encoding="utf-8",
            )
            (images / "image_prompts.json").write_text(
                json.dumps({"items": [{"filename": "cover.png"}]}),
                encoding="utf-8",
            )
            (images / "cover.png").write_bytes(PNG_1X1)
            (svg_output / "01.svg").write_text("<svg/>", encoding="utf-8")
            (svg_output / "02.svg").write_text("<svg/>", encoding="utf-8")
            (svg_final / "01.svg").write_text("<svg/>", encoding="utf-8")

            progress = job_runner_module._artifact_progress(project_dir)

        self.assertTrue(progress["project_ready"])
        self.assertTrue(progress["design_ready"])
        self.assertEqual(progress["total_slides"], 9)
        self.assertEqual(progress["ai_images_total"], 1)
        self.assertEqual(progress["ai_images_ready"], 1)
        self.assertEqual(progress["slides_created"], 2)
        self.assertEqual(progress["slides_finalized"], 1)
    def test_api_success_runs_submit_pipeline_to_done_with_fake_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            Settings(data_dir).accept_notice()
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()

            with (
                patch.object(server_module, "ADAPTER_CLASSES", {"fake": FakeServerAdapter}),
                patch.object(server_module, "detect_all", return_value={"fake": {"available": True}}),
            ):
                response = self._post_fake_job(client)
                self.assertEqual(response.status_code, 202)
                job_id = response.get_json()["job_id"]
                runner = app.extensions["ppt_webtool"]["runner"]()
                final = self._wait_terminal(runner, job_id, timeout_seconds=20)
                self._join_job_thread(runner, job_id)

            final = runner.get_status(job_id)
            self.assertEqual(final["status"], "done")
            self.assertTrue(final.get("result_files"))
            self.assertTrue(any(event.get("event") == "PREPROCESS_DONE" for event in final.get("events", [])))
            self.assertTrue(any(event.get("event") == "STAGES_BUILT" for event in final.get("events", [])))

            self.assertEqual(final["runtime"]["cli"], "fake")
            self.assertEqual(final["runtime"]["version"], server_module._read_version(server_module._WEBTOOL_ROOT, server_module._REPO_ROOT))
            self.assertEqual(final["progress"]["phase"], "done")
            log_response = client.get(f"/api/jobs/{job_id}/log")
            self.assertEqual(log_response.status_code, 200)
            self.assertIn("PROCESS_EXIT", log_response.get_data(as_text=True))
            log_response.close()

    def test_raw_absent_validator_passes_after_preprocess_and_fails_on_raw_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace" / "raw-absent-job"
            upload = root / "notes.txt"
            upload.write_text("confidential source text\n", encoding="utf-8")

            manifest = preprocess_job(str(workspace), [(str(upload), "notes.txt")])
            validator = make_raw_absent_validator()
            ctx = {"project_dir": str(workspace / "project"), "manifest": manifest}

            result = validator(ctx)
            self.assertTrue(result.ok, result.reason)

            raw_path = Path(str(manifest["uploads"][0]["raw_path"]))
            shutil.copy2(raw_path, workspace / "project" / "notes.txt")
            copied_result = validator(ctx)
            self.assertFalse(copied_result.ok)
            self.assertIn("원본 파일", str(copied_result.reason))

    def test_security_notice_acceptance_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            settings = Settings(data_dir)

            self.assertFalse(settings.load()["security_notice_accepted"])
            accepted = settings.accept_notice()
            self.assertTrue(accepted["security_notice_accepted"])
            reloaded = Settings(data_dir)
            self.assertTrue(reloaded.load()["security_notice_accepted"])

    def test_settings_api_redacts_stored_image_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            secret = "sk-test-redaction-secret"
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)

            with patch.object(server_module, "detect_all", return_value={}):
                client = app.test_client()
                post_response = client.post(
                    "/api/settings",
                    json={"image_api": {"backend": "openai", "key": secret}},
                )
                settings_response = client.get("/api/settings")
                meta_response = client.get("/api/meta")

            self.assertEqual(post_response.status_code, 200)
            self.assertEqual(settings_response.status_code, 200)
            self.assertEqual(meta_response.status_code, 200)
            stored = Settings(data_dir).load()
            self.assertEqual(stored["image_api"]["key"], secret)

            for response in (post_response, settings_response, meta_response):
                self.assertNotIn(secret, response.get_data(as_text=True))

            post_json = post_response.get_json()
            settings_json = settings_response.get_json()
            meta_json = meta_response.get_json()
            self.assertEqual(post_json["image_api"], {"backend": "openai", "has_key": True})
            self.assertEqual(settings_json["image_api"], {"backend": "openai", "has_key": True})
            self.assertEqual(meta_json["image_api"], {"backend": "openai", "has_key": True})
            self.assertEqual(meta_json["app_id"], "kch-ppt-tool")
            self.assertEqual(meta_json["version"], server_module._read_version(server_module._WEBTOOL_ROOT, server_module._REPO_ROOT))
            self.assertEqual(meta_json["instance_root"], str(server_module._WEBTOOL_ROOT))
            self.assertEqual(meta_json["server_port"], server_module.DEFAULT_PORT)
            self.assertNotIn("key", post_json["image_api"])
            self.assertNotIn("key", settings_json["image_api"])
            self.assertNotIn("key", meta_json["image_api"])

            clear_response = client.post("/api/settings", json={"image_api": {"key": ""}})
            self.assertEqual(clear_response.status_code, 200)
            self.assertFalse(clear_response.get_json()["image_api"]["has_key"])
            self.assertIsNone(Settings(data_dir).load()["image_api"]["key"])

    def test_settings_api_rejects_unsupported_image_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()

            response = client.post(
                "/api/settings",
                json={"image_api": {"backend": "minimax", "key": "secret"}},
            )

            self.assertEqual(response.status_code, 400)
            body = response.get_data(as_text=True)
            self.assertIn("openai", body)
            self.assertIn("gemini", body)
            self.assertIsNone(Settings(data_dir).load()["image_api"]["backend"])

    def test_job_stage_env_uses_ppt_master_image_contract_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            Settings(data_dir).accept_notice()
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()
            ImageEnvServerAdapter.captured_envs = []
            secret = "sk-env-contract"

            def fast_preprocess(workspace: str, _uploads: list[tuple[str, str]], **_kwargs: Any) -> dict[str, Any]:
                return _ok_manifest(workspace)

            with (
                patch.object(server_module, "ADAPTER_CLASSES", {"fake": ImageEnvServerAdapter}),
                patch.object(server_module, "detect_all", return_value={"fake": {"available": True}}),
                patch.object(server_module, "preprocess_job", side_effect=fast_preprocess),
            ):
                incomplete_settings = client.post("/api/settings", json={"image_api": {"backend": "openai"}})
                self.assertEqual(incomplete_settings.status_code, 200)

                incomplete_response = self._post_fake_job(client, filename="incomplete.png")
                self.assertEqual(incomplete_response.status_code, 202)
                incomplete_job_id = incomplete_response.get_json()["job_id"]
                runner = app.extensions["ppt_webtool"]["runner"]()
                incomplete_final = self._wait_terminal(runner, incomplete_job_id, timeout_seconds=20)
                self._join_job_thread(runner, incomplete_job_id)
                self.assertEqual(incomplete_final["status"], "done")

                configured_settings = client.post(
                    "/api/settings",
                    json={"image_api": {"backend": "openai", "key": secret}},
                )
                self.assertEqual(configured_settings.status_code, 200)

                configured_response = self._post_fake_job(client, filename="configured.png")
                self.assertEqual(configured_response.status_code, 202)
                configured_job_id = configured_response.get_json()["job_id"]
                configured_final = self._wait_terminal(runner, configured_job_id, timeout_seconds=20)
                self._join_job_thread(runner, configured_job_id)
                self.assertEqual(configured_final["status"], "done")

            self.assertEqual(len(ImageEnvServerAdapter.captured_envs), 2)
            incomplete_env = ImageEnvServerAdapter.captured_envs[0]
            self.assertNotIn("IMAGE_BACKEND", incomplete_env)
            self.assertNotIn("OPENAI_API_KEY", incomplete_env)
            self.assertNotIn("GEMINI_API_KEY", incomplete_env)
            self.assertNotIn("PPT_WEBTOOL_IMAGE_BACKEND", incomplete_env)
            self.assertNotIn("PPT_WEBTOOL_IMAGE_API_KEY", incomplete_env)

            configured_env = ImageEnvServerAdapter.captured_envs[1]
            self.assertEqual(configured_env["IMAGE_BACKEND"], "openai")
            self.assertEqual(configured_env["OPENAI_API_KEY"], secret)
            self.assertNotIn("GEMINI_API_KEY", configured_env)
            self.assertNotIn("PPT_WEBTOOL_IMAGE_BACKEND", configured_env)
            self.assertNotIn("PPT_WEBTOOL_IMAGE_API_KEY", configured_env)

    def test_invalid_upload_extension_removes_workspace_before_job_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            workspace_root = root / "workspace"
            Settings(data_dir).accept_notice()
            app = server_module.create_app(data_dir=data_dir, workspace_root=workspace_root)
            client = app.test_client()
            with patch.object(server_module, "detect_all", return_value={"claude": {"available": True}}):
                response = client.post(
                    "/api/jobs",
                    data={
                        "request_text": "make a deck",
                        "image_source": "none",
                        "cli": "claude",
                        "files": (io.BytesIO(b"unsupported"), "source.bad"),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 400)
            remaining_workspaces = sorted(path.name for path in workspace_root.iterdir()) if workspace_root.exists() else []
            self.assertEqual(remaining_workspaces, [])

    def test_job_store_persist_and_load_record_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir))
            payload = {"job_id": "store-test", "status": "queued", "events": []}

            store.persist("store-test", payload, threading.Lock())

            self.assertEqual(store.load_record("store-test"), payload)
    def test_persist_serializes_snapshot_order_across_terminal_race(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = self._runner(Path(temp_dir))
            workspace = runner.workspace_root / "persist-race"
            workspace.mkdir(parents=True)
            job = job_runner_module._JobState(
                id="persist-race",
                workspace=workspace,
                raw_dir=workspace / "_raw",
                project_dir=workspace / "project",
                log_path=workspace / "job.log",
                stages=[],
                manifest=None,
                record={"job_id": "persist-race", "status": "running", "events": []},
            )
            running_persist_started = threading.Event()
            release_running_persist = threading.Event()
            original_persist = runner._store.persist

            def delayed_persist(job_id: str, payload: dict[str, Any], persist_lock: Any) -> None:
                if payload.get("status") == "running":
                    running_persist_started.set()
                    self.assertTrue(release_running_persist.wait(2))
                original_persist(job_id, payload, persist_lock)

            with patch.object(runner._store, "persist", side_effect=delayed_persist):
                running_thread = threading.Thread(target=runner._persist_job, args=(job,))
                running_thread.start()
                self.assertTrue(running_persist_started.wait(1))

                with job.lock:
                    job.record["status"] = "cancelled"
                terminal_thread = threading.Thread(target=runner._persist_job, args=(job,))
                terminal_thread.start()
                time.sleep(0.1)
                release_running_persist.set()
                running_thread.join(2)
                terminal_thread.join(2)

            self.assertFalse(running_thread.is_alive())
            self.assertFalse(terminal_thread.is_alive())
            self.assertEqual(runner._store.load_record("persist-race")["status"], "cancelled")

    def test_history_append_upserts_concurrent_terminal_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history = History(temp_dir)
            failures: list[Exception] = []

            def append_status(index: int) -> None:
                try:
                    history.append(
                        {
                            "job_id": "same-job",
                            "created_at": "2026-07-13T00:00:00+00:00",
                            "status": "done" if index % 2 else "failed",
                            "cli": "codex",
                            "result_files": [f"deck-{index}.pptx"],
                            "workspace": "workspace/same-job",
                        }
                    )
                except Exception as exc:  # pragma: no cover - asserted below.
                    failures.append(exc)

            threads = [threading.Thread(target=append_status, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            records = history.list()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["job_id"], "same-job")
    def test_collect_result_files_collects_export_stage_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            export_path = workspace / "project" / "exports" / "deck.pptx"
            export_path.parent.mkdir(parents=True)
            export_path.write_bytes(b"pptx")
            stage = Stage(
                id="export",
                kind="export",
                owner="script",
                command=[],
                cwd=str(workspace / "project"),
                env={},
                expected_outputs=["exports/**/*.pptx"],
                validators=[],
                timeout_seconds=30,
            )

            self.assertEqual(collect_result_files([stage], workspace), ["project/exports/deck.pptx"])
    def test_image_intake_copies_png_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_image = root / "pixel.png"
            raw_image.write_bytes(PNG_1X1)
            project_dir = root / "workspace" / "image-job" / "project"
            skipped_master_root = root / "missing-ppt-master"

            result = intake_images([str(raw_image)], str(project_dir), ppt_master_root=str(skipped_master_root))

            images_dir = project_dir / "images"
            manifest_path = images_dir / "image_manifest.json"
            self.assertEqual(result["status"], "ok")
            self.assertTrue(images_dir.is_dir())
            self.assertTrue(manifest_path.is_file())
            entries = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["source"], "user-provided")
            self.assertTrue((images_dir / entries[0]["filename"]).is_file())


class LaunchCommandTests(unittest.TestCase):
    def test_resolves_argv0_to_full_path(self) -> None:
        from app.paths import resolve_launch_command

        resolved = resolve_launch_command([sys.executable, "exec", "-"])
        self.assertEqual(resolved[1:], ["exec", "-"])
        self.assertTrue(Path(resolved[0]).is_file())

    def test_unknown_executable_left_unchanged(self) -> None:
        from app.paths import resolve_launch_command

        original = ["definitely-not-a-real-cli-xyz", "exec", "-"]
        self.assertEqual(resolve_launch_command(original), original)

    def test_windows_cmd_shim_wrapped_via_comspec(self) -> None:
        from app import paths

        fake = r"C:\\Users\\kch\\AppData\\Roaming\\npm\\codex.cmd"
        with patch.object(paths.shutil, "which", return_value=fake), patch.object(
            paths.sys, "platform", "win32"
        ), patch.dict(paths.os.environ, {"COMSPEC": r"C:\\Windows\\System32\\cmd.exe"}):
            resolved = paths.resolve_launch_command(["codex", "exec", "--sandbox", "-"])
        self.assertEqual(
            resolved,
            [r"C:\\Windows\\System32\\cmd.exe", "/d", "/c", fake, "exec", "--sandbox", "-"],
        )

    def test_adapter_delivers_prompt_via_stdin_not_argv(self) -> None:
        from app.cli_adapters import CodexAdapter

        ctx = {"project_dir": "/tmp/x", "image_env": {}, "job_timeout_seconds": 3600}
        stage = CodexAdapter().build_stages(ctx, "LINE1\nLINE2\nLINE3")[0]
        self.assertEqual(stage.stdin_data, "LINE1\nLINE2\nLINE3")
        self.assertEqual(stage.command[-1], "-")
        self.assertIn("--ignore-user-config", stage.command)
        self.assertIn("--ephemeral", stage.command)
        model_flag = stage.command.index("--model")
        self.assertEqual(stage.command[model_flag + 1], "gpt-5.6-luna")
        config_values = [
            stage.command[index + 1]
            for index, value in enumerate(stage.command[:-1])
            if value == "-c"
        ]
        runtime = CodexAdapter().runtime_info(ctx)
        self.assertEqual(runtime["model"], "gpt-5.6-luna")
        self.assertEqual(runtime["reasoning_effort"], "max")
        self.assertIn('windows.sandbox="elevated"', config_values)
        self.assertIn('model_reasoning_effort="max"', config_values)
        self.assertTrue(all("LINE1" not in part for part in stage.command))
    def test_gemini_keeps_headless_prompt_flag_with_stdin(self) -> None:
        from app.cli_adapters import GeminiAdapter

        ctx = {"project_dir": "/tmp/x", "image_env": {}, "job_timeout_seconds": 3600}
        stage = GeminiAdapter().build_stages(ctx, "PROMPT")[0]
        prompt_flag = stage.command.index("-p")
        self.assertEqual(stage.command[prompt_flag + 1], "")
        self.assertEqual(stage.stdin_data, "PROMPT")
    def test_prompt_pins_runtime_python_executable(self) -> None:
        from app.prompt_builder import build_prompt

        prompt = build_prompt(
            {"image_source": "ai", "cli": "codex"},
            None,
            "/tmp/ppt-master/SKILL.md",
            "source.md",
            python_executable=sys.executable,
        )
        self.assertIn(str(Path(sys.executable).resolve()), prompt)
        self.assertIn("`python`, `python3`, `py` 명령을 사용하지 마십시오.", prompt)
        self.assertIn("PIL·SVG·도형·스크립트로 이미지를 직접 그려", prompt)
    def test_job_runner_writes_stage_stdin_to_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "workspace" / "stdin-job" / "project"
            project.mkdir(parents=True)
            output = project / "stdin.txt"
            command = [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path('stdin.txt').write_text(sys.stdin.read(), encoding='utf-8')",
            ]
            stage = Stage(
                id="stdin-stage",
                kind="agent",
                owner="agent",
                command=command,
                cwd=str(project),
                env={},
                expected_outputs=[],
                validators=[],
                timeout_seconds=10,
                stdin_data="한글\nMULTILINE\nPROMPT",
            )
            runner = JobRunner(root / "workspace", root / "data")
            runner.submit([stage], job_id="stdin-job")

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = runner.get_status("stdin-job")
                if status["status"] in TERMINAL_STATUSES:
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "done", status)
            self.assertEqual(output.read_text(encoding="utf-8"), "한글\nMULTILINE\nPROMPT")

    def test_job_runner_retries_transient_model_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "workspace" / "retry-job" / "project"
            project.mkdir(parents=True)
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "p=Path('attempt.txt'); n=int(p.read_text()) if p.exists() else 0; "
                    "p.write_text(str(n+1)); "
                    "print('ERROR: Selected model is at capacity. Please try a different model.' if n == 0 else 'OK'); "
                    "sys.exit(1 if n == 0 else 0)"
                ),
            ]
            stage = Stage(
                id="retry-stage",
                kind="agent",
                owner="agent",
                command=command,
                cwd=str(project),
                env={},
                expected_outputs=[],
                validators=[],
                timeout_seconds=10,
                max_retries=1,
            )
            runner = JobRunner(root / "workspace", root / "data")
            runner.submit([stage], job_id="retry-job")

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status = runner.get_status("retry-job")
                if status["status"] in TERMINAL_STATUSES:
                    break
                time.sleep(0.05)
            self.assertEqual(status["status"], "done", status)
            self.assertEqual((project / "attempt.txt").read_text(), "2")
            self.assertTrue(any(event["event"] == "STAGE_RETRY" for event in status["events"]))


if __name__ == "__main__":
    unittest.main()
