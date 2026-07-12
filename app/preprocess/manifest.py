from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VALID_KINDS = {"document", "image"}
VALID_STATUSES = {"ok", "failed"}


def make_upload_record(
    *,
    original_name: str,
    sha256: str,
    size: int,
    raw_path: str,
    kind: str,
    outputs: list[str],
) -> dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"지원하지 않는 업로드 종류입니다: {kind}")
    return {
        "original_name": original_name,
        "sha256": sha256,
        "size": size,
        "raw_path": raw_path,
        "kind": kind,
        "outputs": outputs,
    }


def build_manifest(
    *,
    job_id: str,
    uploads: list[dict[str, Any]],
    status: str,
    detail: str,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"지원하지 않는 전처리 상태입니다: {status}")
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "uploads": uploads,
        "preprocess": {
            "status": status,
            "detail": detail,
        },
    }


def manifest_path(workspace: str) -> Path:
    return Path(workspace).resolve() / "manifest.json"


def write_manifest(workspace: str, manifest: dict[str, Any]) -> dict[str, Any]:
    path = manifest_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_manifest(workspace: str) -> dict[str, Any]:
    return json.loads(manifest_path(workspace).read_text(encoding="utf-8"))
