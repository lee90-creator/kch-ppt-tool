from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SETTINGS_FILE = "settings.json"
HISTORY_FILE = "history.json"
_HISTORY_FIELDS = ("job_id", "created_at", "status", "cli", "result_files", "workspace")


def _default_settings() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "security_notice_accepted": False,
        "image_api": {
            "backend": None,
            "key": None,
        },
        "claude_model": None,
        "job_timeout_minutes": 60,
    }


def _normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    data = _default_settings()
    data.update(raw)
    data["schema_version"] = SCHEMA_VERSION
    data["security_notice_accepted"] = bool(data.get("security_notice_accepted"))

    image_api = data.get("image_api")
    if not isinstance(image_api, dict):
        image_api = {}
    merged_image_api = _default_settings()["image_api"]
    merged_image_api.update(image_api)
    data["image_api"] = merged_image_api

    cm = data.get("claude_model")
    data["claude_model"] = cm.strip() if isinstance(cm, str) and cm.strip() else None

    try:
        minutes = int(data.get("job_timeout_minutes") or 60)
    except (TypeError, ValueError):
        minutes = 60
    data["job_timeout_minutes"] = max(10, min(180, minutes))
    return data


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class Settings:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / SETTINGS_FILE
        self.data = self.load()

    def load(self) -> dict[str, Any]:
        raw = _read_json(self.path, _default_settings())
        if not isinstance(raw, dict):
            raise ValueError("settings.json must contain a JSON object")
        self.data = _normalize_settings(raw)
        return dict(self.data)

    def save(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if data is not None:
            self.data = _normalize_settings(data)
        else:
            self.data = _normalize_settings(getattr(self, "data", _default_settings()))
        _atomic_write_json(self.path, self.data)
        return dict(self.data)

    def accept_notice(self) -> dict[str, Any]:
        self.data["security_notice_accepted"] = True
        return self.save()

    def set_image_key(self, backend: str | None, key: str | None) -> dict[str, Any]:
        image_api = dict(self.data.get("image_api") or {})
        image_api["backend"] = backend
        image_api["key"] = key
        self.data["image_api"] = image_api
        return self.save()


class History:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / HISTORY_FILE

    def _load_history(self) -> dict[str, Any]:
        raw = _read_json(self.path, {"schema_version": SCHEMA_VERSION, "records": []})
        if isinstance(raw, list):
            raw = {"schema_version": SCHEMA_VERSION, "records": raw}
        if not isinstance(raw, dict):
            raise ValueError("history.json must contain a JSON object")
        records = raw.get("records", [])
        if not isinstance(records, list):
            raise ValueError("history.json records must be a list")
        return {"schema_version": SCHEMA_VERSION, "records": list(records)}

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_record(record)
        history = self._load_history()
        history["records"].append(normalized)
        _atomic_write_json(self.path, history)
        return dict(normalized)

    def list(self) -> list[dict[str, Any]]:
        history = self._load_history()
        return [dict(record) for record in history["records"]]

    def get(self, job_id: str) -> dict[str, Any] | None:
        for record in reversed(self.list()):
            if record.get("job_id") == job_id:
                return record
        return None

    @staticmethod
    def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("history record must be a JSON object")
        missing = [field for field in _HISTORY_FIELDS if field not in record]
        if missing:
            raise ValueError(f"history record missing fields: {', '.join(missing)}")

        normalized = dict(record)
        for field in ("job_id", "created_at", "status", "cli", "workspace"):
            normalized[field] = str(normalized[field])

        result_files = normalized["result_files"]
        if result_files is None:
            normalized["result_files"] = []
        elif isinstance(result_files, list):
            normalized["result_files"] = [str(item) for item in result_files]
        else:
            raise ValueError("history record result_files must be a list")
        return normalized
