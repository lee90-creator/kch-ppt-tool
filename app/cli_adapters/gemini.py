from __future__ import annotations

from .base import CliAdapter


class GeminiAdapter(CliAdapter):
    name = "gemini"
    COMMAND_PREFIX = ["gemini", "-p"]
    COMMAND_SUFFIX = ["--approval-mode", "yolo"]
    EXTRA_ENV = {"GEMINI_CLI_TRUST_WORKSPACE": "true"}
    NOTE = "free-tier 미지원 이슈가 있어 유료/지원 계정에서만 one-shot 실행이 안정적입니다."
