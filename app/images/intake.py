from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..paths import process_group_popen_kwargs, resolve_ppt_master_root, terminate_process_tree

ANALYZE_TIMEOUT_SECONDS = 720
DERIVATIVE_MARKER = b"ppt-webtool:user-provided-asset"


def default_ppt_master_root() -> Path:
    return resolve_ppt_master_root(Path(__file__).resolve().parents[2])


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_destination(directory: Path, filename: str) -> Path:
    candidate = directory / Path(filename).name
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


def _png_derivative(data: bytes) -> bytes | None:
    import zlib

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    marker_data = b"Comment\x00" + DERIVATIVE_MARKER
    chunk = (
        len(marker_data).to_bytes(4, "big")
        + b"tEXt"
        + marker_data
        + (zlib.crc32(b"tEXt" + marker_data) & 0xFFFFFFFF).to_bytes(4, "big")
    )
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return None
        if chunk_type == b"IEND":
            return data[:offset] + chunk + data[offset:]
        offset = chunk_end
    return None


def _jpeg_derivative(data: bytes) -> bytes | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    payload = DERIVATIVE_MARKER
    segment_length = len(payload) + 2
    if segment_length > 0xFFFF:
        return None
    return (
        data[:2]
        + b"\xff\xfe"
        + segment_length.to_bytes(2, "big")
        + payload
        + data[2:]
    )


def _gif_derivative(data: bytes) -> bytes | None:
    if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")) or len(data) < 13:
        return None
    packed = data[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    if offset > len(data) or len(DERIVATIVE_MARKER) > 255:
        return None
    comment = (
        b"\x21\xfe"
        + bytes([len(DERIVATIVE_MARKER)])
        + DERIVATIVE_MARKER
        + b"\x00"
    )
    return data[:offset] + comment + data[offset:]


def _webp_derivative(data: bytes) -> bytes | None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    payload = DERIVATIVE_MARKER
    padding = b"\x00" if len(payload) % 2 else b""
    chunk = b"EXIF" + len(payload).to_bytes(4, "little") + payload + padding
    output = bytearray(data + chunk)
    output[4:8] = (len(output) - 8).to_bytes(4, "little")
    return bytes(output)


def _mark_derivative_copy(path: Path) -> None:
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".png":
        marked = _png_derivative(data)
    elif suffix in {".jpg", ".jpeg"}:
        marked = _jpeg_derivative(data)
    elif suffix == ".gif":
        marked = _gif_derivative(data)
    elif suffix == ".webp":
        marked = _webp_derivative(data)
    else:
        marked = None
    path.write_bytes(
        marked if marked is not None else data + b"\n" + DERIVATIVE_MARKER
    )


def _ensure_not_raw_clone(raw_path: Path, destination: Path) -> None:
    try:
        if raw_path.stat().st_size != destination.stat().st_size:
            return
        if _sha256(raw_path) != _sha256(destination):
            return
        _mark_derivative_copy(destination)
    except OSError:
        return

def _read_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _write_manifest(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )




def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _run_analyze_images(images_dir: Path, ppt_master_root: Path) -> dict[str, Any]:
    script = ppt_master_root / "skills" / "ppt-master" / "scripts" / "analyze_images.py"
    if not script.is_file():
        return {"status": "skipped", "detail": f"이미지 분석 스크립트를 찾을 수 없습니다: {script}"}

    command = [sys.executable, str(script), str(images_dir)]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(ppt_master_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_group_popen_kwargs(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=ANALYZE_TIMEOUT_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(proc)
            stdout, stderr = proc.communicate()
    except Exception as exc:  # noqa: BLE001 - image analysis is best-effort.
        if proc is not None:
            terminate_process_tree(proc)
        return {"status": "failed", "detail": f"이미지 분석 실행 실패: {type(exc).__name__}: {exc}"}

    if timed_out:
        return {
            "status": "failed",
            "detail": f"이미지 분석 시간이 {ANALYZE_TIMEOUT_SECONDS}초를 초과했습니다.",
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
            "command": command,
        }

    if proc.returncode != 0:
        detail = _tail(stderr) or _tail(stdout) or f"이미지 분석 종료 코드: {proc.returncode}"
        return {
            "status": "failed",
            "detail": detail,
            "returncode": proc.returncode,
            "stdout": _tail(stdout),
            "stderr": _tail(stderr),
            "command": command,
        }

    return {
        "status": "ok",
        "detail": "이미지 분석 완료",
        "returncode": proc.returncode,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
        "command": command,
    }


def intake_images(
    image_raw_paths: list[str],
    project_dir: str,
    ppt_master_root: str | None = None,
) -> dict[str, Any]:
    """Copy user-provided images into project/images and maintain image_manifest.json."""
    project = Path(project_dir).resolve()
    images_dir = project / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = images_dir / "image_manifest.json"
    entries_by_filename = {
        str(item["filename"]): item
        for item in _read_manifest(manifest_path)
        if isinstance(item.get("filename"), str)
    }

    copied: list[dict[str, Any]] = []
    for raw_path in image_raw_paths:
        raw = Path(raw_path).resolve()
        destination = _unique_destination(images_dir, raw.name)
        shutil.copy2(raw, destination)
        _ensure_not_raw_clone(raw, destination)
        digest = _sha256(destination)
        entry = {
            "filename": destination.name,
            "source": "user-provided",
            "sha256": digest,
        }
        entries_by_filename[destination.name] = entry
        copied.append({**entry, "path": str(destination)})

    entries = [entries_by_filename[name] for name in sorted(entries_by_filename)]
    _write_manifest(manifest_path, entries)

    root = Path(ppt_master_root).resolve() if ppt_master_root else default_ppt_master_root().resolve()
    analysis = _run_analyze_images(images_dir, root) if copied else {"status": "skipped", "detail": "처리할 이미지가 없습니다."}

    detail = "이미지 수집 완료"
    if analysis.get("status") == "failed":
        detail = f"이미지 수집 완료, 분석 실패: {analysis.get('detail', '')}"
    elif analysis.get("status") == "skipped":
        detail = f"이미지 수집 완료, 분석 건너뜀: {analysis.get('detail', '')}"

    return {
        "status": "ok",
        "detail": detail,
        "images_dir": str(images_dir),
        "manifest": str(manifest_path),
        "images": copied,
        "analysis": analysis,
    }
