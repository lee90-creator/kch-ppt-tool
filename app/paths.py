from __future__ import annotations

import os
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
