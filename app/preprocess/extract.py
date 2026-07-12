from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from ..paths import process_group_popen_kwargs, resolve_ppt_master_root, terminate_process_tree

DEFAULT_TIMEOUT_SECONDS = 1500


def default_ppt_master_root() -> Path:
    return resolve_ppt_master_root(Path(__file__).resolve().parents[2])



def _run_source_to_md(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
) -> tuple[int | None, bool, str, str, str | None]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    proc: subprocess.Popen[str] | None = None
    popen = popen_factory or subprocess.Popen
    try:
        proc = popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_popen_kwargs(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
            return proc.returncode, False, stdout, stderr, None
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc)
            stdout, stderr = proc.communicate()
            return proc.returncode, True, stdout, stderr, None
    except Exception as exc:  # noqa: BLE001 - callers need failure details, not exceptions.
        if proc is not None:
            terminate_process_tree(proc)
        return None, False, "", "", f"{type(exc).__name__}: {exc}"


def _parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def convert_document(
    raw_path: str,
    out_dir: str,
    ppt_master_root: str | None = None,
    *,
    cancel_event: Any | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
) -> dict[str, Any]:
    """Convert a supported user document into Markdown without raising on converter failure."""
    raw = Path(raw_path).resolve()
    output_dir = Path(out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    root = Path(ppt_master_root).resolve() if ppt_master_root else default_ppt_master_root().resolve()
    script = root / "skills" / "ppt-master" / "scripts" / "source_to_md.py"
    output_path = output_dir / f"{raw.stem}.md"
    command = [
        sys.executable,
        str(script),
        str(raw),
        "-o",
        str(output_path),
        "--json",
    ]

    if not script.is_file():
        return {
            "status": "failed",
            "detail": f"문서 변환 스크립트를 찾을 수 없습니다: {script}",
            "output": str(output_path),
            "command": command,
        }

    if cancel_event is not None and cancel_event.is_set():
        return {
            "status": "failed",
            "detail": "문서 변환이 취소되었습니다.",
            "output": str(output_path),
            "command": command,
        }

    return_code, timed_out, stdout, stderr, exception = _run_source_to_md(
        command=command,
        cwd=root,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        popen_factory=popen_factory,
    )

    if cancel_event is not None and cancel_event.is_set():
        return {
            "status": "failed",
            "detail": "문서 변환이 취소되었습니다.",
            "output": str(output_path),
            "command": command,
        }
    if exception is not None:
        return {
            "status": "failed",
            "detail": f"문서 변환 실행 실패: {exception}",
            "output": str(output_path),
            "command": command,
        }

    if timed_out:
        return {
            "status": "failed",
            "detail": f"문서 변환 시간이 {DEFAULT_TIMEOUT_SECONDS}초를 초과했습니다.",
            "output": str(output_path),
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
            "command": command,
        }

    if return_code != 0:
        detail = _tail(stderr) or _tail(stdout) or f"문서 변환 종료 코드: {return_code}"
        return {
            "status": "failed",
            "detail": detail,
            "returncode": return_code,
            "output": str(output_path),
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
            "command": command,
        }

    if not output_path.is_file():
        return {
            "status": "failed",
            "detail": f"문서 변환 결과 파일이 생성되지 않았습니다: {output_path}",
            "returncode": return_code,
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
            "command": command,
        }

    parsed = _parse_json_stdout(stdout)
    return {
        "status": "ok",
        "detail": "문서 변환 완료",
        "output": str(output_path),
        "returncode": return_code,
        "result": parsed,
    }
