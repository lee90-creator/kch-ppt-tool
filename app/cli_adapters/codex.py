from __future__ import annotations

from .base import CliAdapter


class CodexAdapter(CliAdapter):
    name = "codex"
    # 실측 v0.142: `--ask-for-approval` 플래그가 제거되었고 `exec` 자체가 비대화형으로 동작한다.
    COMMAND_PREFIX = [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ephemeral",
        "-c",
        'windows.sandbox="elevated"',
    ]
    COMMAND_SUFFIX: list[str] = []
    # Pin the requested GPT-5.6 Luna model and maximum reasoning effort so
    # portable installs behave consistently without loading user configuration.
    DEFAULT_MODEL = "gpt-5.6-luna"
    REASONING_CONFIG = 'model_reasoning_effort="max"'
    # 프롬프트는 stdin으로 전달한다(멀티라인 프롬프트를 Windows cmd 인자로 넘기면 깨짐).
    # `-`는 codex exec가 stdin에서 지시문을 읽게 한다.
    def _model_flags(self, job_ctx: dict[str, object]) -> list[str]:
        return ["-c", self.REASONING_CONFIG, "--model", self.DEFAULT_MODEL]

    def runtime_info(self, job_ctx: dict[str, object]) -> dict[str, str | None]:
        return {
            "cli": self.name,
            "model": self.DEFAULT_MODEL,
            "reasoning_effort": "max",
        }
    STDIN_ARGS = ["-"]
