from __future__ import annotations

from .base import AdapterInfo, CliAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter


ADAPTER_CLASSES: tuple[type[CliAdapter], ...] = (
    ClaudeAdapter,
    CodexAdapter,
    GeminiAdapter,
)


def detect_all() -> dict[str, AdapterInfo]:
    adapters = (adapter_class() for adapter_class in ADAPTER_CLASSES)
    return {adapter.name: adapter.detect() for adapter in adapters}
