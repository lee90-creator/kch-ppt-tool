from __future__ import annotations

import glob as glob_module
import json
from pathlib import Path
from typing import Any

try:  # Package import in the Flask app.
    from .stage_contract import Stage
except ImportError:  # Direct module import in small smoke checks.
    from stage_contract import Stage


def load_manifest(workspace: Path) -> dict[str, Any] | None:
    manifest_path = workspace / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError("manifest 루트가 객체가 아닙니다")
    return loaded


def refresh_manifest(workspace: Path) -> dict[str, Any] | None:
    return load_manifest(workspace)


def collect_result_files(stages: list[Stage], workspace: Path) -> list[str]:
    files: set[str] = set()
    export_stages = [stage for stage in stages if stage.kind == "export"]
    stages_to_scan = export_stages or stages
    for stage in stages_to_scan:
        patterns = stage.expected_outputs or (["exports/**/*"] if stage.kind == "export" else [])
        for pattern in patterns:
            if "exports" not in pattern:
                continue
            for path in _glob_from(stage.cwd, pattern):
                if path.is_file():
                    files.add(_display_path(path, workspace))

    if not files:
        project_dir = workspace / "project"
        for exports_dir in project_dir.glob("**/exports"):
            if exports_dir.is_dir():
                for path in exports_dir.rglob("*"):
                    if path.is_file():
                        files.add(_display_path(path, workspace))
    return sorted(files)


def preprocess_failure_reason(manifest: dict[str, Any]) -> str | None:
    preprocess = manifest.get("preprocess")
    if manifest.get("status") == "failed":
        detail = manifest.get("detail")
        return str(detail) if detail else "전처리에 실패했습니다"
    if isinstance(preprocess, dict) and preprocess.get("status") == "failed":
        detail = preprocess.get("detail")
        return str(detail) if detail else "전처리에 실패했습니다"
    return None


def _glob_from(cwd: str, pattern: str) -> list[Path]:
    raw_pattern = Path(pattern)
    if raw_pattern.is_absolute():
        expanded = str(raw_pattern)
    else:
        expanded = str(Path(cwd) / pattern)
    return [Path(match) for match in glob_module.glob(expanded, recursive=True)]


def _display_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve(strict=False).relative_to(base.resolve(strict=False)))
    except ValueError:
        return str(path)
