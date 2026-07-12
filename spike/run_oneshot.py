#!/usr/bin/env python3
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

SPIKE_DIR = Path(__file__).resolve().parent
PPT_MASTER_ROOT = (SPIKE_DIR / "../../ppt-master").resolve()
PPT_MASTER_SKILL = PPT_MASTER_ROOT / "skills/ppt-master/SKILL.md"
EXPECTED_PROJECT_DIR = "spike_deck"
FIXTURE = SPIKE_DIR / "fixtures/one_pager.md"
RESULT_MD = SPIKE_DIR / "RESULT.md"
RESULT_PREFIX = "RESULT_JSON: "
WARN_IDLE_SECONDS = 180
STALL_IDLE_SECONDS = 720
CONFIRM_UI_HOST = "127.0.0.1"
CONFIRM_UI_PORT = 5050
MB = 1024 * 1024


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_workspace(cli: str) -> tuple[str, Path]:
    ts = timestamp()
    workspace = SPIKE_DIR / "workspace" / f"oneshot_{cli}_{ts}"
    if workspace.exists():
        ts = f"{ts}_{os.getpid()}"
        workspace = SPIKE_DIR / "workspace" / f"oneshot_{cli}_{ts}"
    (workspace / "sources").mkdir(parents=True, exist_ok=False)
    (workspace / "projects").mkdir(parents=True, exist_ok=False)
    shutil.copy2(FIXTURE, workspace / "sources/one_pager.md")
    return ts, workspace


def parse_page_range(raw: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(?:[~\-–—]\s*(\d+))?\s*", raw)
    if not match:
        raise ValueError("pages must be a number or range like 3~5")
    low = int(match.group(1))
    high = int(match.group(2) or match.group(1))
    if low <= 0 or high < low:
        raise ValueError("pages range must be positive and ordered")
    return low, high


def build_prompt(pages: str, source_path: Path) -> str:
    return f"""당신은 PPT Master 스킬을 실행하는 헤드리스 one-shot 에이전트입니다.

아래 스킬 문서를 절대경로로 읽고, 입력 원문을 바탕으로 경영진 보고용 PPT를 생성하십시오.
- 스킬 문서 절대경로: {PPT_MASTER_SKILL}
- 입력 원문: ./sources/one_pager.md
- 입력 원문 절대경로: {source_path}
- 프로젝트 경로: ./projects/spike_deck

사전 확정 디자인 스펙:
- [Template] B 자유디자인
- [Format] ppt169
- [Pages] {pages}
- [Tone] 프로페셔널 경영보고
- [Image] 사용 안 함
- [Audience] 사내 경영진

실행 규칙:
1. 이 실행은 헤드리스 one-shot입니다. 사용자에게 질문하지 마십시오.
2. 모든 BLOCKING 확인 게이트는 위 사전 확정 디자인 스펙값으로 이미 선응답된 것으로 간주하고 즉시 진행하십시오. 이는 스킬 규칙 2의 확인 대기 동작을 오버라이드합니다.
3. confirm_ui 서버(포트 5050)를 절대 기동하지 마십시오. 확인이 필요하면 "just confirm in chat" 경로로 자체 확정하고 계속 진행하십시오.
4. 프로젝트는 반드시 ./projects/spike_deck 에 생성하십시오. 다른 프로젝트 경로를 만들지 마십시오.
5. 이미지 사용 안 함 조건을 지키고, 텍스트·도형·표 중심의 16:9 PPT를 생성하십시오.
6. 마지막에는 exports/*.pptx 산출을 완료한 뒤, 완성된 PPTX의 절대경로를 한 줄로 출력하십시오.
"""


def build_command(cli: str, prompt: str, model: str | None = None) -> list[str]:
    if cli == "claude":
        # R9 승인 스펙 결정: claude는 이 스파이크에서 bypassPermissions를 유지한다.
        cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
        # 기본 모델이 사용량 한도에 걸릴 때 --model 로 대체 지정 (예: sonnet).
        if model:
            cmd += ["--model", model]
        return cmd
    return [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        prompt,
    ]


def terminate_process_group(proc: subprocess.Popen[str], events: list[dict[str, Any]], log: TextIO, start: float, lock: threading.Lock) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        record_event(events, log, start, lock, "SIGKILL", {"reason": "process did not exit after SIGTERM"})
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
    proc.wait(timeout=10)


def record_event(
    events: list[dict[str, Any]],
    log: TextIO,
    start: float,
    lock: threading.Lock,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    now = time.monotonic()
    item: dict[str, Any] = {
        "elapsed_seconds": round(now - start, 3),
        "event": event,
        "ts": iso_now(),
    }
    if details:
        item.update(details)
    with lock:
        events.append(item)
        suffix = f" {json.dumps(details, ensure_ascii=False, sort_keys=True)}" if details else ""
        log.write(f"[{item['ts']}] [EVENT] {event}{suffix}\n")
        log.flush()


def port_is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False

def upstream_manifest(root: Path) -> tuple[dict[str, list[int]] | None, str | None]:
    """Stat-based recursive manifest of the upstream tree (path -> [size, mtime_ns]).

    Covers gitignored paths too (unlike `git status --porcelain`), so mutations to
    ignored files such as ppt-master/projects/* or .env are still detected.
    `.git/` internals are excluded.
    """
    try:
        manifest: dict[str, list[int]] = {}
        root = root.resolve(strict=True)
        if not root.is_dir():
            return None, f"upstream root is not a directory: {root}"

        def _raise(exc: OSError) -> None:
            raise exc

        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                fp = Path(dirpath) / name
                try:
                    st = fp.stat()
                except OSError as exc:
                    return None, f"stat failed for {fp}: {type(exc).__name__}: {exc}"
                manifest[str(fp.relative_to(root))] = [st.st_size, st.st_mtime_ns]
        if not manifest:
            return None, f"upstream manifest is empty for {root}; refusing to treat as scannable"
        return manifest, None
    except Exception as exc:  # noqa: BLE001 - guard availability must survive environment failures.
        return None, f"{type(exc).__name__}: {exc}"


def manifest_diff(before: dict[str, list[int]], after: dict[str, list[int]]) -> list[str]:
    changes: list[str] = []
    for path in sorted(set(before) | set(after)):
        if path not in after:
            changes.append(f"removed: {path}")
        elif path not in before:
            changes.append(f"added: {path}")
        elif before[path] != after[path]:
            changes.append(f"modified: {path}")
    return changes


def start_upstream_guard() -> dict[str, Any]:
    snapshot, error = upstream_manifest(PPT_MASTER_ROOT)
    guard: dict[str, Any] = {
        "available": error is None,
        "before_file_count": len(snapshot) if snapshot is not None else None,
        "changes": [],
        "error": error,
        "method": "stat-manifest (size+mtime_ns, .git excluded)",
        "mutated": False,
        # Fail closed: an unavailable guard is itself a failure reason.
        "reason": "upstream_guard_unavailable" if error else None,
        "root": str(PPT_MASTER_ROOT),
    }
    guard["_before"] = snapshot
    return guard


def finish_upstream_guard(guard: dict[str, Any]) -> dict[str, Any]:
    before = guard.pop("_before", None)
    if not guard["available"] or before is None:
        return guard

    snapshot, error = upstream_manifest(PPT_MASTER_ROOT)
    if error:
        guard.update(
            {
                "available": False,
                "error": error,
                "mutated": False,
                "reason": "upstream_guard_unavailable",
            }
        )
        return guard

    changes = manifest_diff(before, snapshot)
    guard["after_file_count"] = len(snapshot)
    guard["changes"] = changes[:50]
    guard["change_count"] = len(changes)
    guard["mutated"] = bool(changes)
    if guard["mutated"]:
        guard["reason"] = "upstream_mutation"
    return guard


def confirm_ui_baseline() -> dict[str, Any]:
    listening = port_is_listening(CONFIRM_UI_HOST, CONFIRM_UI_PORT)
    return {
        "confirm_ui": {
            "host": CONFIRM_UI_HOST,
            "listening": listening,
            "port": CONFIRM_UI_PORT,
            "reason": "confirm_ui_baseline_busy" if listening else None,
        }
    }


def stream_reader(
    stream: TextIO,
    name: str,
    log: TextIO,
    state: dict[str, Any],
    lock: threading.Lock,
) -> None:
    try:
        for raw_line in iter(stream.readline, ""):
            line = raw_line.rstrip("\n")
            now = time.monotonic()
            ts = iso_now()
            with lock:
                state["last_output_time"] = now
                state["idle_warned"] = False
                log.write(f"[{ts}] [{name}] {line}\n")
                log.flush()
    finally:
        try:
            stream.close()
        except OSError:
            pass


def monitor_process(command: list[str], workspace: Path, timeout_seconds: int) -> dict[str, Any]:
    log_path = workspace / "process.log"
    events: list[dict[str, Any]] = []
    lock = threading.Lock()
    start = time.monotonic()
    state: dict[str, Any] = {
        "confirm_ui_detected": False,
        "idle_warned": False,
        "kill_reason": None,
        "last_output_time": start,
    }
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    baseline = confirm_ui_baseline()
    baseline_busy = bool(baseline["confirm_ui"]["listening"])

    with log_path.open("w", encoding="utf-8") as log:
        baseline_event = "CONFIRM_UI_BASELINE_BUSY" if baseline_busy else "CONFIRM_UI_BASELINE_IDLE"
        record_event(events, log, start, lock, baseline_event, baseline["confirm_ui"])
        record_event(events, log, start, lock, "START", {"command": command, "cwd": str(workspace)})
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001 - preserve CLI startup failures in the spike result.
            record_event(events, log, start, lock, "EXCEPTION", {"message": f"{type(exc).__name__}: {exc}"})
            return {
                "baseline": baseline,
                "confirm_ui_detected": False,
                "events": events,
                "exception": f"{type(exc).__name__}: {exc}",
                "exit_code": None,
                "intervention": False,
                "kill_reason": None,
                "log_path": str(log_path),
                "timed_out": False,
                "wall_seconds": round(time.monotonic() - start, 3),
            }

        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_thread = threading.Thread(target=stream_reader, args=(proc.stdout, "stdout", log, state, lock), daemon=True)
        stderr_thread = threading.Thread(target=stream_reader, args=(proc.stderr, "stderr", log, state, lock), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        next_port_check = start

        while True:
            now = time.monotonic()
            if proc.poll() is not None:
                break

            elapsed = now - start
            with lock:
                idle_seconds = now - state["last_output_time"]
                idle_warned = bool(state["idle_warned"])

            if elapsed >= timeout_seconds:
                state["kill_reason"] = "timeout"
                record_event(events, log, start, lock, "TIMEOUT", {"timeout_seconds": timeout_seconds})
                terminate_process_group(proc, events, log, start, lock)
                break

            if idle_seconds >= STALL_IDLE_SECONDS:
                state["kill_reason"] = "stall"
                record_event(events, log, start, lock, "STALL", {"idle_seconds": round(idle_seconds, 3)})
                terminate_process_group(proc, events, log, start, lock)
                break

            if idle_seconds >= WARN_IDLE_SECONDS and not idle_warned:
                with lock:
                    state["idle_warned"] = True
                record_event(events, log, start, lock, "WARN_NO_OUTPUT", {"idle_seconds": round(idle_seconds, 3)})

            if now >= next_port_check:
                if not baseline_busy and port_is_listening(CONFIRM_UI_HOST, CONFIRM_UI_PORT):
                    with lock:
                        already_detected = bool(state["confirm_ui_detected"])
                        state["confirm_ui_detected"] = True
                    if not already_detected:
                        record_event(
                            events,
                            log,
                            start,
                            lock,
                            "CONFIRM_UI_DETECTED",
                            {"host": CONFIRM_UI_HOST, "port": CONFIRM_UI_PORT},
                        )
                next_port_check = now + 5

            time.sleep(1)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            state["kill_reason"] = state["kill_reason"] or "stall"
            record_event(events, log, start, lock, "STALL", {"reason": "process did not finish after monitor loop"})
            terminate_process_group(proc, events, log, start, lock)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if not baseline_busy and port_is_listening(CONFIRM_UI_HOST, CONFIRM_UI_PORT):
            with lock:
                already_detected = bool(state["confirm_ui_detected"])
                state["confirm_ui_detected"] = True
            if not already_detected:
                record_event(
                    events,
                    log,
                    start,
                    lock,
                    "CONFIRM_UI_DETECTED",
                    {"host": CONFIRM_UI_HOST, "port": CONFIRM_UI_PORT},
                )
        record_event(events, log, start, lock, "EXIT", {"exit_code": proc.returncode})

    wall_seconds = time.monotonic() - start
    return {
        "baseline": baseline,
        "confirm_ui_detected": bool(state["confirm_ui_detected"]),
        "events": events,
        "exception": None,
        "exit_code": proc.returncode,
        "intervention": state["kill_reason"] is not None,
        "kill_reason": state["kill_reason"],
        "log_path": str(log_path),
        "timed_out": state["kill_reason"] == "timeout",
        "wall_seconds": round(wall_seconds, 3),
    }


def unexpected_project_dirs(workspace: Path) -> list[str]:
    projects_dir = workspace / "projects"
    if not projects_dir.exists():
        return []
    return sorted(
        path.name
        for path in projects_dir.iterdir()
        if path.is_dir() and path.name != EXPECTED_PROJECT_DIR
    )


def latest_pptx(workspace: Path) -> Path | None:
    pattern = str(workspace / "projects" / EXPECTED_PROJECT_DIR / "exports" / "*.pptx")
    candidates = [Path(path) for path in glob.glob(pattern)]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def validate_pptx(workspace: Path, page_range: tuple[int, int]) -> dict[str, Any]:
    unexpected_dirs = unexpected_project_dirs(workspace)

    pptx_path = latest_pptx(workspace)
    if pptx_path is None:
        return {
            "pptx_path": None,
            "pptx_size_mb": 0.0,
            "slide_count": None,
            "slide_count_ok": False,
            "valid_pptx": False,
            "unexpected_dirs": unexpected_dirs,
            "validation_error": "no_pptx",
        }
    try:
        from pptx import Presentation

        deck = Presentation(str(pptx_path))
        slide_count = len(deck.slides)
    except Exception as exc:  # noqa: BLE001 - validation must record the concrete loader failure.
        return {
            "pptx_path": str(pptx_path),
            "pptx_size_mb": round(pptx_path.stat().st_size / MB, 3),
            "slide_count": None,
            "slide_count_ok": False,
            "valid_pptx": False,
            "unexpected_dirs": unexpected_dirs,
            "validation_error": f"{type(exc).__name__}: {exc}",
        }

    low, high = page_range
    return {
        "pptx_path": str(pptx_path),
        "pptx_size_mb": round(pptx_path.stat().st_size / MB, 3),
        "slide_count": slide_count,
        "slide_count_ok": low <= slide_count <= high,
        "valid_pptx": True,
        "unexpected_dirs": unexpected_dirs,
        "validation_error": None,
    }


def decide_verdict(run: dict[str, Any], validation: dict[str, Any], upstream_guard: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    kill_reason = run["kill_reason"]
    if kill_reason == "timeout":
        reasons.append("timeout")
    elif kill_reason == "stall":
        reasons.append("stall")

    if run["confirm_ui_detected"]:
        reasons.append("confirm_ui_started")

    if validation["unexpected_dirs"]:
        reasons.append("unexpected_project_dirs")

    if validation["pptx_path"] is None:
        reasons.append("no_pptx")
    elif not validation["valid_pptx"]:
        reasons.append("invalid_pptx")
    elif not validation["slide_count_ok"]:
        reasons.append("slide_count")

    if run["exception"] or (run["exit_code"] not in (0, None) and not kill_reason):
        reasons.append("nonzero_exit")

    if upstream_guard["mutated"]:
        reasons.append("upstream_mutation")
    if not upstream_guard["available"]:
        # Fail closed: without a working guard we cannot prove upstream integrity.
        reasons.append("upstream_guard_unavailable")

    deduped = list(dict.fromkeys(reasons))
    return ("pass" if not deduped else "fail", deduped)


def append_result_markdown(result: dict[str, Any]) -> None:
    RESULT_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## P0 one-shot 스파이크 {result['cli']} ({result['timestamp']})",
        "",
        f"- Verdict: `{result['verdict']}`",
        f"- Reasons: {', '.join(result['reasons']) if result['reasons'] else 'none'}",
        f"- Workspace: `{result['workspace']}`",
        f"- Prompt: `{result['prompt_path']}`",
        f"- Log: `{result['log_path']}`",
        f"- Wall time: {result['wall_seconds']}s / timeout {result['timeout_seconds']}s",
        f"- Exit code: `{result['exit_code']}`; intervention: `{result['intervention']}`; confirm_ui_detected: `{result['confirm_ui_detected']}`",
        f"- baseline: `{json.dumps(result['baseline'], ensure_ascii=False, sort_keys=True)}`",
        f"- unexpected_dirs: `{json.dumps(result['unexpected_dirs'], ensure_ascii=False, sort_keys=True)}`",
        f"- upstream_guard: `{json.dumps(result['upstream_guard'], ensure_ascii=False, sort_keys=True)}`",
        f"- PPTX: `{result['pptx_path']}`; valid: `{result['valid_pptx']}`; slides: `{result['slide_count']}`; slide_count_ok: `{result['slide_count_ok']}`",
        "",
        "### 이벤트 타임라인",
        "",
    ]
    if result["events"]:
        for event in result["events"]:
            detail = {key: value for key, value in event.items() if key not in {"ts", "elapsed_seconds", "event"}}
            detail_text = f" — {json.dumps(detail, ensure_ascii=False, sort_keys=True)}" if detail else ""
            lines.append(f"- {event['ts']} (+{event['elapsed_seconds']}s) `{event['event']}`{detail_text}")
    else:
        lines.append("- none")
    lines.append("")

    prefix = "\n" if RESULT_MD.exists() and RESULT_MD.stat().st_size else ""
    with RESULT_MD.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0 headless one-shot PPT Master spike runner.")
    parser.add_argument("--cli", choices=["claude", "codex"], required=True, help="Agent CLI to execute.")
    parser.add_argument("--timeout", type=int, default=1800, help="Whole-run timeout in seconds.")
    parser.add_argument("--pages", default="3~5", help="Expected slide count or range, e.g. 3~5.")
    parser.add_argument("--model", default=None, help="claude 전용: 대체 모델 지정(예: sonnet). 기본 모델 사용량 한도 회피용.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        page_range = parse_page_range(args.pages)
    except ValueError as exc:
        parser.error(str(exc))

    ts, workspace = make_workspace(args.cli)
    source_path = workspace / "sources/one_pager.md"
    prompt = build_prompt(args.pages, source_path)
    prompt_path = workspace / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    command = build_command(args.cli, prompt, getattr(args, "model", None))
    upstream_guard = start_upstream_guard()

    run = monitor_process(command, workspace, args.timeout)
    upstream_guard = finish_upstream_guard(upstream_guard)
    validation = validate_pptx(workspace, page_range)
    verdict, reasons = decide_verdict(run, validation, upstream_guard)

    result: dict[str, Any] = {
        "cli": args.cli,
        "command": command,
        "confirm_ui_probe": {"host": CONFIRM_UI_HOST, "interval_seconds": 5, "port": CONFIRM_UI_PORT},
        "page_range": {"max": page_range[1], "min": page_range[0], "raw": args.pages},
        "prompt_path": str(prompt_path),
        "result_md": str(RESULT_MD),
        "source_path": str(source_path),
        "timestamp": ts,
        "timeout_seconds": args.timeout,
        "upstream_guard": upstream_guard,
        "verdict": verdict,
        "workspace": str(workspace),
        **run,
        **validation,
        "reasons": reasons,
    }
    append_result_markdown(result)
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
