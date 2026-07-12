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
    ]
    COMMAND_SUFFIX: list[str] = []
