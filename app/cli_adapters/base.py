from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..stage_contract import (
    Stage,
    make_outputs_exist_validator,
    make_pptx_valid_validator,
    make_raw_absent_validator,
)


@dataclass
class AdapterInfo:
    name: str
    path: str | None
    version: str | None
    available: bool
    note: str | None = None


class CliAdapter:
    name: str

    COMMAND_PREFIX: ClassVar[list[str]] = []
    COMMAND_SUFFIX: ClassVar[list[str]] = []
    EXTRA_ENV: ClassVar[dict[str, str]] = {}
    NOTE: ClassVar[str | None] = None

    EXPECTED_OUTPUTS: ClassVar[list[str]] = ["projects/*/exports/*.pptx"]
    PPTX_OUTPUT_GLOB: ClassVar[str] = "projects/*/exports/*.pptx"
    TIMEOUT_SECONDS: ClassVar[int] = 3600
    VERSION_TIMEOUT_SECONDS: ClassVar[int] = 15

    def detect(self) -> AdapterInfo:
        return detect_cli(
            name=self.name,
            executable=self._executable_name(),
            timeout_seconds=self.VERSION_TIMEOUT_SECONDS,
            base_note=self.NOTE,
        )

    def build_stages(self, job_ctx: dict[str, Any], prompt: str) -> list[Stage]:
        expected_outputs = list(self.EXPECTED_OUTPUTS)
        env = self._build_env(job_ctx)
        stage_timeout = int(job_ctx.get("job_timeout_seconds") or self.TIMEOUT_SECONDS)
        return [
            Stage(
                id=f"{self.name}_agent_oneshot",
                kind="agent",
                owner="agent",
                command=self._build_command(prompt, job_ctx),
                cwd=job_ctx["project_dir"],
                env=env,
                expected_outputs=expected_outputs,
                validators=[
                    make_outputs_exist_validator(expected_outputs),
                    make_pptx_valid_validator(self.PPTX_OUTPUT_GLOB),
                    make_raw_absent_validator(),
                ],
                timeout_seconds=stage_timeout,
            )
        ]

    def prepare_workspace(self, project_dir: str) -> None:
        return None

    def _build_command(self, prompt: str, job_ctx: dict[str, Any] | None = None) -> list[str]:
        if not self.COMMAND_PREFIX:
            raise NotImplementedError(f"{self.__class__.__name__}.COMMAND_PREFIX 클래스 상수가 필요합니다")
        return [*self.COMMAND_PREFIX, prompt, *self.COMMAND_SUFFIX, *self._model_flags(job_ctx or {})]

    def _model_flags(self, job_ctx: dict[str, Any]) -> list[str]:
        """어댑터별 모델 지정 플래그. 기본은 없음(각 CLI 기본 모델 사용)."""
        return []

    def _build_env(self, job_ctx: dict[str, Any]) -> dict[str, str]:
        env = {"PYTHONUTF8": "1"}
        image_env = job_ctx.get("image_env") or {}
        if not isinstance(image_env, dict):
            raise TypeError("job_ctx['image_env']는 dict일 때만 병합할 수 있습니다")
        env.update({str(key): str(value) for key, value in image_env.items()})
        env.update(self.EXTRA_ENV)
        env["PYTHONUTF8"] = "1"
        return env

    def _executable_name(self) -> str:
        if self.COMMAND_PREFIX:
            return self.COMMAND_PREFIX[0]
        return self.name


def detect_cli(
    *,
    name: str,
    executable: str,
    timeout_seconds: int = 15,
    base_note: str | None = None,
) -> AdapterInfo:
    path = shutil.which(executable)
    if path is None:
        return AdapterInfo(
            name=name,
            path=None,
            version=None,
            available=False,
            note=_join_notes(base_note, "실행 파일을 찾을 수 없습니다."),
        )

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            [path, "--version"],
            cwd=str(Path.cwd()),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            start_new_session=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AdapterInfo(
            name=name,
            path=path,
            version=None,
            available=True,
            note=_join_notes(base_note, f"버전 확인 시간이 초과되었습니다({timeout_seconds}초)."),
        )
    except OSError as exc:
        return AdapterInfo(
            name=name,
            path=path,
            version=None,
            available=True,
            note=_join_notes(base_note, f"버전 확인을 실행하지 못했습니다: {exc}"),
        )

    if completed.returncode != 0:
        detail = _first_non_empty_line(completed.stderr, completed.stdout)
        message = f"버전 확인 실패(exit {completed.returncode})"
        if detail:
            message = f"{message}: {detail}"
        return AdapterInfo(
            name=name,
            path=path,
            version=None,
            available=True,
            note=_join_notes(base_note, message),
        )

    version = _first_non_empty_line(completed.stdout, completed.stderr)
    if version is None:
        return AdapterInfo(
            name=name,
            path=path,
            version=None,
            available=True,
            note=_join_notes(base_note, "버전 출력이 비어 있습니다."),
        )

    return AdapterInfo(
        name=name,
        path=path,
        version=version,
        available=True,
        note=base_note,
    )


def _first_non_empty_line(*values: str) -> str | None:
    for value in values:
        for line in value.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return None


def _join_notes(*notes: str | None) -> str | None:
    parts = [note for note in notes if note]
    if not parts:
        return None
    return " ".join(parts)
