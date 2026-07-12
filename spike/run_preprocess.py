#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import resource
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SPIKE_DIR = Path(__file__).resolve().parent
PPT_MASTER_ROOT = (SPIKE_DIR / "../../ppt-master").resolve()
SOURCE_TO_MD = PPT_MASTER_ROOT / "skills/ppt-master/scripts/source_to_md.py"
DEFAULT_INPUT = "../../samples/large-sample.pptx"
RESULT_MD = SPIKE_DIR / "RESULT.md"
RESULT_PREFIX = "RESULT_JSON: "
DIRECT_WALL_LIMIT_SECONDS = 20 * 60
DIRECT_RSS_LIMIT_MB = 4 * 1024
MB = 1024 * 1024


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_from_spike(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = SPIKE_DIR / path
    return path.resolve()


def make_run_dir(prefix: str) -> tuple[str, Path]:
    ts = timestamp()
    run_dir = SPIKE_DIR / "workspace" / f"{prefix}_{ts}"
    if run_dir.exists():
        ts = f"{ts}_{os.getpid()}"
        run_dir = SPIKE_DIR / "workspace" / f"{prefix}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return ts, run_dir


def ru_maxrss_to_mb(value: int) -> float:
    if sys.platform == "darwin":
        return value / MB
    return value / 1024


def terminate_process_group(proc: subprocess.Popen[str]) -> None:
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
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
    proc.wait(timeout=10)


def run_command(command: list[str], timeout_seconds: int, run_dir: Path) -> dict[str, Any]:
    stdout_log = run_dir / "stdout.log"
    stderr_log = run_dir / "stderr.log"
    started = time.monotonic()
    stdout_text = ""
    stderr_text = ""
    exit_code: int | None = None
    timed_out = False
    exception: str | None = None

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(SPIKE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(proc)
            stdout_text, stderr_text = proc.communicate()
        exit_code = proc.returncode
    except Exception as exc:  # noqa: BLE001 - this is a spike runner; preserve the failure in JSON.
        exception = f"{type(exc).__name__}: {exc}"

    wall_seconds = time.monotonic() - started
    stdout_log.write_text(stdout_text or "", encoding="utf-8", errors="replace")
    stderr_log.write_text(stderr_text or "", encoding="utf-8", errors="replace")
    peak_rss_mb = ru_maxrss_to_mb(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)

    return {
        "exit_code": exit_code,
        "exception": exception,
        "peak_rss_mb": round(peak_rss_mb, 2),
        "stderr_log": str(stderr_log),
        "stdout_log": str(stdout_log),
        "timed_out": timed_out,
        "wall_seconds": round(wall_seconds, 3),
    }


def path_stats(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "output_exists": False,
            "output_file_count": 0,
            "output_kind": "missing",
            "output_total_size_mb": 0.0,
        }
    if path.is_file():
        return {
            "output_exists": True,
            "output_file_count": 1,
            "output_kind": "file",
            "output_total_size_mb": round(path.stat().st_size / MB, 3),
        }

    total_size = 0
    file_count = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                file_count += 1
                total_size += child.stat().st_size
        except OSError:
            continue
    return {
        "output_exists": True,
        "output_file_count": file_count,
        "output_kind": "directory",
        "output_total_size_mb": round(total_size / MB, 3),
    }


def decide_verdict(run: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    success = run["exception"] is None and not run["timed_out"] and run["exit_code"] == 0
    if not success:
        if run["exception"]:
            reasons.append(f"exception={run['exception']}")
        if run["timed_out"]:
            reasons.append("timeout")
        if run["exit_code"] != 0:
            reasons.append(f"exit_code={run['exit_code']}")
    if run["wall_seconds"] > DIRECT_WALL_LIMIT_SECONDS:
        reasons.append(f"wall_seconds>{DIRECT_WALL_LIMIT_SECONDS}")
    if run["peak_rss_mb"] > DIRECT_RSS_LIMIT_MB:
        reasons.append(f"peak_rss_mb>{DIRECT_RSS_LIMIT_MB}")
    return ("direct_ok" if not reasons else "fallback_needed", reasons)


def append_result_markdown(result: dict[str, Any]) -> None:
    RESULT_MD.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## P0b 전처리 스파이크 ({result['timestamp']})",
        "",
        f"- Verdict: `{result['verdict']}`",
        f"- Input: `{result['input_path']}`",
        f"- Output: `{result['output_path']}` ({result['output_kind']}, files={result['output_file_count']}, size={result['output_total_size_mb']} MB)",
        f"- Wall time: {result['wall_seconds']}s / limit {DIRECT_WALL_LIMIT_SECONDS}s",
        f"- Peak RSS: {result['peak_rss_mb']} MB / limit {DIRECT_RSS_LIMIT_MB} MB",
        f"- Exit code: `{result['exit_code']}`; timeout: `{result['timed_out']}`; exception: `{result['exception']}`",
        f"- Logs: stdout `{result['stdout_log']}`, stderr `{result['stderr_log']}`",
        f"- Reasons: {', '.join(result['reasons']) if result['reasons'] else 'none'}",
        "",
        "```bash",
        shlex.join(result["command"]),
        "```",
        "",
    ]
    prefix = "\n" if RESULT_MD.exists() and RESULT_MD.stat().st_size else ""
    with RESULT_MD.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Phase 0b source_to_md preprocessing spike runner.")
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help="Input PPTX path, resolved relative to this spike directory when relative.",
    )
    parser.add_argument("--timeout", type=int, default=1500, help="Subprocess timeout in seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ts, run_dir = make_run_dir("preprocess")
    input_path = resolve_from_spike(args.input)
    output_path = run_dir / "out"
    command = ["python3", str(SOURCE_TO_MD), str(input_path), "-o", str(output_path), "--json"]

    run = run_command(command, args.timeout, run_dir)
    stats = path_stats(output_path)
    verdict, reasons = decide_verdict(run)

    result: dict[str, Any] = {
        "command": command,
        "input_path": str(input_path),
        "limits": {
            "direct_peak_rss_mb": DIRECT_RSS_LIMIT_MB,
            "direct_wall_seconds": DIRECT_WALL_LIMIT_SECONDS,
            "timeout_seconds": args.timeout,
        },
        "output_path": str(output_path),
        "ppt_master_root": str(PPT_MASTER_ROOT),
        "reasons": reasons,
        "result_md": str(RESULT_MD),
        "run_dir": str(run_dir),
        "source_to_md": str(SOURCE_TO_MD),
        "timestamp": ts,
        "verdict": verdict,
        **run,
        **stats,
    }
    append_result_markdown(result)
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if verdict == "direct_ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
