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
    ]
    COMMAND_SUFFIX: list[str] = []
    # Codex CLI 0.130 defaults to gpt-5.3-codex, which ChatGPT accounts reject.
    # Pin the oldest currently supported general model so portable installs work
    # without loading the user's plugin-heavy config.toml.
    DEFAULT_MODEL = "gpt-5.4"
    # 프롬프트는 stdin으로 전달한다(멀티라인 프롬프트를 Windows cmd 인자로 넘기면 깨짐).
    # `-`는 codex exec가 stdin에서 지시문을 읽게 한다.
    def _model_flags(self, job_ctx: dict[str, object]) -> list[str]:
        return ["--model", self.DEFAULT_MODEL]
    STDIN_ARGS = ["-"]
