from __future__ import annotations

from typing import Any

from .base import CliAdapter


class ClaudeAdapter(CliAdapter):
    name = "claude"
    COMMAND_PREFIX = ["claude", "-p"]
    # --output-format stream-json --verbose: claude -p 는 완료 전까지 stdout이 거의 없어
    # 무출력 감지(STALL)에 오탐으로 걸린다. 스트리밍 모드로 진행 상황을 계속 내보내
    # 잡 러너 모니터가 활동을 인지하게 한다(검증은 exports/*.pptx 글롭으로 수행).
    COMMAND_SUFFIX = [
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    NOTE = "v1 완전 인수 대상"

    def _model_flags(self, job_ctx: dict[str, Any]) -> list[str]:
        # 계정 기본 모델이 사용량 한도(예: 'You've reached your Fable 5 limit')에 걸릴 때
        # 설정의 claude_model 로 대체 모델을 지정한다(예: sonnet). 비어 있으면 CLI 기본 모델.
        model = job_ctx.get("cli_model")
        if isinstance(model, str) and model.strip():
            return ["--model", model.strip()]
        return []

    def runtime_info(self, job_ctx: dict[str, Any]) -> dict[str, str | None]:
        model = job_ctx.get("cli_model")
        selected_model = model.strip() if isinstance(model, str) and model.strip() else None
        return {
            "cli": self.name,
            "model": selected_model,
            "reasoning_effort": None,
        }
