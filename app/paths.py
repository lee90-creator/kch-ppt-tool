from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


def resolve_ppt_master_root(webtool_root: str | Path) -> Path:
    root = Path(webtool_root).expanduser().resolve(strict=False)
    bundled_root = root / "ppt-master"
    if bundled_root.exists():
        return bundled_root
    return root.parent / "ppt-master"


def resolve_launch_command(command: list[str] | None) -> list[str] | None:
    """Resolve argv[0] to a concrete executable path.

    On Windows the CLIs (claude/codex/gemini) are installed as npm ``.cmd``
    shims, which CreateProcess cannot find or run directly (``Popen(["codex"])``
    fails with WinError 2). Resolve via ``shutil.which`` (honours PATHEXT so the
    ``.cmd`` is found) and wrap batch shims through ``cmd.exe /c``. Prompts are
    delivered on stdin, so the wrapped command line stays short and single-line.
    """
    if not command:
        return list(command) if command is not None else None
    argv = list(command)
    resolved = shutil.which(argv[0])
    if resolved is None:
        return argv
    rest = argv[1:]
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/c", resolved, *rest]
    return [resolved, *rest]


def process_group_popen_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return

    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
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
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
