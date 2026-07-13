from __future__ import annotations

import glob as glob_module
import hashlib
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ValidationResult:
    ok: bool
    reason: str | None = None


@dataclass
class Stage:
    """Immutable v1 stage contract.

    Retry/resume is new-job-only; a stage does not carry resume state.
    """
    id: str
    kind: str  # "preprocess"|"agent"|"script"|"export"
    owner: str  # "agent"|"script"
    command: list[str] | None
    cwd: str
    env: dict[str, str]
    expected_outputs: list[str]  # glob, cwd 기준
    validators: list  # Callable[[dict], ValidationResult], dict=job_ctx
    timeout_seconds: int = 1800
    stdin_data: str | None = None


Validator = Callable[[dict[str, Any]], ValidationResult]


def _base_dir(job_ctx: dict[str, Any]) -> Path:
    cwd = job_ctx.get("cwd") or job_ctx.get("stage_cwd") or job_ctx.get("project_dir")
    return Path(str(cwd))


def _glob_matches(base_dir: Path, pattern: str) -> list[Path]:
    raw_pattern = Path(pattern)
    if raw_pattern.is_absolute():
        resolved_pattern = str(raw_pattern)
    else:
        resolved_pattern = str(base_dir / pattern)
    return [Path(match) for match in glob_module.glob(resolved_pattern, recursive=True)]


def make_outputs_exist_validator(globs: list[str]) -> Validator:
    def validate(job_ctx: dict[str, Any]) -> ValidationResult:
        base_dir = _base_dir(job_ctx)
        missing = [pattern for pattern in globs if not any(path.exists() for path in _glob_matches(base_dir, pattern))]
        if missing:
            return ValidationResult(False, "출력 파일을 찾을 수 없습니다: " + ", ".join(missing))
        return ValidationResult(True)

    return validate


def make_pptx_valid_validator(glob: str) -> Validator:
    def validate(job_ctx: dict[str, Any]) -> ValidationResult:
        base_dir = _base_dir(job_ctx)
        matches = [path for path in _glob_matches(base_dir, glob) if path.is_file()]
        if not matches:
            return ValidationResult(False, f"PPTX 파일을 찾을 수 없습니다: {glob}")

        invalid: list[str] = []
        for path in matches:
            try:
                if not zipfile.is_zipfile(path):
                    invalid.append(f"{path}: ZIP 형식이 아닙니다")
                    continue
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    required = {"[Content_Types].xml", "ppt/presentation.xml"}
                    missing = sorted(required - names)
                    if missing:
                        invalid.append(f"{path}: 필수 PPTX 항목 누락({', '.join(missing)})")
            except (OSError, zipfile.BadZipFile) as exc:
                invalid.append(f"{path}: {type(exc).__name__}: {exc}")

        if invalid:
            return ValidationResult(False, "유효하지 않은 PPTX 파일: " + "; ".join(invalid))
        return ValidationResult(True)

    return validate


def make_raw_absent_validator() -> Validator:
    def validate(job_ctx: dict[str, Any]) -> ValidationResult:
        manifest = job_ctx.get("manifest")
        if not manifest:
            return ValidationResult(True)

        uploads = manifest.get("uploads") or []
        if not uploads:
            return ValidationResult(True)

        project_dir_raw = job_ctx.get("project_dir")
        if not project_dir_raw:
            return ValidationResult(False, "project_dir이 없어 원본 격리 검증을 수행할 수 없습니다")
        project_dir = Path(str(project_dir_raw))
        if not project_dir.exists():
            return ValidationResult(True)

        sha_to_upload: dict[str, str] = {}
        sizes_to_sha: dict[int, set[str]] = {}
        original_names: set[str] = set()
        for item in uploads:
            if not isinstance(item, dict):
                continue
            sha = str(item.get("sha256") or "").lower()
            if sha:
                sha_to_upload[sha] = str(item.get("original_name") or sha)
                try:
                    size = int(item.get("size"))
                except (TypeError, ValueError):
                    size = -1
                if size >= 0:
                    sizes_to_sha.setdefault(size, set()).add(sha)
            original_name = str(item.get("original_name") or "")
            if original_name:
                original_names.add(Path(original_name).name)

        violations: list[str] = []
        errors: list[str] = []
        for root, _dirs, files in os.walk(project_dir):
            root_path = Path(root)
            for filename in files:
                path = root_path / filename
                rel_path = _safe_relative(path, project_dir)
                if filename in original_names:
                    violations.append(f"파일명 일치: {rel_path}")
                try:
                    stat = path.stat()
                except OSError as exc:
                    errors.append(f"{rel_path}: {type(exc).__name__}: {exc}")
                    continue
                candidate_shas = sizes_to_sha.get(stat.st_size)
                if not candidate_shas:
                    continue
                try:
                    digest = _sha256_file(path)
                except OSError as exc:
                    errors.append(f"{rel_path}: {type(exc).__name__}: {exc}")
                    continue
                if digest in candidate_shas:
                    violations.append(f"sha256 일치: {rel_path} == {sha_to_upload.get(digest, digest)}")

        if violations:
            return ValidationResult(False, "원본 파일이 project 트리에 존재합니다: " + "; ".join(violations))
        if errors:
            return ValidationResult(False, "원본 격리 검증 중 파일 접근 오류: " + "; ".join(errors))
        return ValidationResult(True)

    return validate


def _safe_relative(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
