from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class JobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def persist(self, job_id: str, payload: dict[str, Any], persist_lock: Any) -> None:
        path = self.data_dir / f"{job_id}.json"
        with persist_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, path)

    def load_record(self, job_id: str) -> dict[str, Any] | None:
        if job_id in {".", ".."} or not _JOB_ID_RE.fullmatch(job_id):
            return None
        path = self.data_dir / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None
