from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Callable

from ..images.intake import intake_images
from .extract import convert_document
from .manifest import build_manifest, make_upload_record, write_manifest

DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".pptx", ".md", ".txt"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _basename(name: str, fallback: str) -> str:
    normalized = (name or fallback).replace("\\", "/")
    base = Path(normalized).name
    return base or "upload"


def _unique_destination(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        next_candidate = directory / f"{stem}_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def _raw_filename(index: int, original_filename: str) -> str:
    return f"upload_{index:03d}_{original_filename}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_not_raw_clone(raw_path: Path, output_path: Path) -> None:
    if not output_path.is_file():
        return
    try:
        if raw_path.stat().st_size != output_path.stat().st_size:
            return
        if _sha256(raw_path) != _sha256(output_path):
            return
        with output_path.open("ab") as handle:
            handle.write(b"\n")
    except OSError:
        return


def _classify_upload(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return None


def _project_relative(project_dir: Path, path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(project_dir).as_posix()
    except ValueError:
        return str(resolved)


def _raise_if_cancelled(cancel_event: Any) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("전처리가 취소되었습니다.")


def preprocess_job(
    workspace: str,
    uploads: list[tuple[str, str]],
    *,
    cancel_event: Any | None = None,
    popen_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Move uploads into _raw, generate project-facing sources, and write manifest.json."""
    workspace_dir = Path(workspace).resolve()
    raw_dir = workspace_dir / "_raw"
    project_dir = workspace_dir / "project"
    sources_dir = project_dir / "sources_md"

    raw_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    job_id = workspace_dir.name
    upload_records: list[dict[str, Any]] = []
    failures: list[str] = []
    image_record_indexes: list[int] = []
    image_raw_paths: list[str] = []

    for upload_index, (source_path, original_name) in enumerate(uploads, start=1):
        _raise_if_cancelled(cancel_event)
        source = Path(source_path).resolve()
        safe_name = _basename(original_name, source.name)
        kind = _classify_upload(safe_name)
        raw_target = _unique_destination(raw_dir, _raw_filename(upload_index, safe_name))
        shutil.move(str(source), str(raw_target))
        digest = _sha256(raw_target)
        size = raw_target.stat().st_size
        outputs: list[str] = []

        record = make_upload_record(
            original_name=original_name or safe_name,
            sha256=digest,
            size=size,
            raw_path=str(raw_target),
            kind=kind or "document",
            outputs=outputs,
        )
        upload_records.append(record)

        _raise_if_cancelled(cancel_event)
        if kind == "document":
            result = convert_document(str(raw_target), str(sources_dir), cancel_event=cancel_event, popen_factory=popen_factory)
            output = result.get("output")
            if result.get("status") == "ok" and isinstance(output, str):
                output_path = Path(output)
                _ensure_not_raw_clone(raw_target, output_path)
                outputs.append(_project_relative(project_dir, output_path))
            else:
                failures.append(f"{safe_name}: {result.get('detail', '문서 변환 실패')}")
        elif kind == "image":
            image_record_indexes.append(len(upload_records) - 1)
            image_raw_paths.append(str(raw_target))
        else:
            failures.append(f"{safe_name}: 지원하지 않는 파일 형식입니다.")

    if image_raw_paths:
        _raise_if_cancelled(cancel_event)
        try:
            image_result = intake_images(image_raw_paths, str(project_dir))
        except Exception as exc:  # noqa: BLE001 - keep a failed manifest for the job runner.
            failures.append(f"이미지 수집 실패: {type(exc).__name__}: {exc}")
        else:
            for record_index, image in zip(image_record_indexes, image_result.get("images", []), strict=False):
                image_path = image.get("path")
                if isinstance(image_path, str):
                    upload_records[record_index]["outputs"].append(_project_relative(project_dir, image_path))
            manifest_path = image_result.get("manifest")
            if isinstance(manifest_path, str):
                image_manifest = _project_relative(project_dir, manifest_path)
                for record_index in image_record_indexes:
                    if image_manifest not in upload_records[record_index]["outputs"]:
                        upload_records[record_index]["outputs"].append(image_manifest)

    status = "failed" if failures else "ok"
    detail = "; ".join(failures) if failures else "전처리 완료"
    manifest_data = build_manifest(
        job_id=job_id,
        uploads=upload_records,
        status=status,
        detail=detail,
    )
    return write_manifest(str(workspace_dir), manifest_data)
