from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_PATH = "./projects/deck"
DEFAULT_TEMPLATE = "B 자유디자인"
DEFAULT_FORMAT = "ppt169"
DEFAULT_TONE = "프로페셔널 경영보고"
DEFAULT_AUDIENCE = "사내 경영진"

_IMAGE_POLICIES = {
    "none": (
        "사용 안 함",
        "이미지 사용 안 함 조건을 지키고, 텍스트·도형·표 중심의 16:9 PPT를 생성하십시오.",
    ),
    "web": (
        "웹 이미지 검색 허용",
        "필요한 경우 image_search를 사용해 웹 이미지를 찾을 수 있습니다. 출처·라이선스 위험이 큰 이미지는 사용하지 말고, 스킬의 인용/출처 규칙을 따르십시오.",
    ),
    "ai": (
        "AI 이미지 생성(스킬 기본 체인)",
        "이미지가 필요하면 PPT Master 스킬의 기본 AI 이미지 생성 체인을 따르십시오. 임의의 외부 이미지 검색으로 대체하지 마십시오. 호스트 네이티브 AI 이미지 도구 또는 설정된 이미지 API만 사용하십시오. PIL·SVG·도형·스크립트로 이미지를 직접 그려 AI 생성 이미지라고 표시하는 행위는 금지합니다. 실제 AI 이미지 생성 도구를 사용할 수 없으면 명확한 실패 사유를 출력하고 작업을 실패 처리하십시오.",
    ),
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _absolute_path(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _image_policy(image_source: str) -> tuple[str, str]:
    try:
        return _IMAGE_POLICIES[image_source]
    except KeyError as exc:
        allowed = ", ".join(sorted(_IMAGE_POLICIES))
        raise ValueError(f"image_source must be one of: {allowed}") from exc


def _company_style_block(style: dict[str, Any]) -> str:
    mapping = style.get("design_spec_mapping")
    if not isinstance(mapping, dict):
        return ""

    lines: list[str] = []
    for section, spec in mapping.items():
        if not isinstance(spec, dict):
            continue
        instruction = _text(spec.get("prompt_instruction_ko"))
        if not instruction:
            continue
        lines.append(f"- {section}: {instruction}")

    if not lines:
        return ""
    return "[회사 스타일 규칙]\n" + "\n".join(lines)


def build_prompt(
    form: dict,
    style: dict | None,
    skill_md_path: str,
    sources_desc: str,
    *,
    python_executable: str | None = None,
) -> str:
    """Build the headless PPT Master prompt from web form values."""

    request_text = _text(form.get("request_text"), "제공된 입력 자료를 바탕으로 경영진 보고용 PPT를 생성하십시오.")
    page_range = _text(form.get("page_range"), "1~3")
    image_source = _text(form.get("image_source"), "none")
    audience = _text(form.get("audience"), DEFAULT_AUDIENCE)
    cli = _text(form.get("cli"), "미지정")
    company_style_enabled = bool(form.get("company_style"))

    image_spec, image_rule = _image_policy(image_source)
    company_style_state = "적용" if company_style_enabled else "미적용"
    skill_abs_path = _absolute_path(skill_md_path)
    source_text = _text(sources_desc, "전처리된 입력 자료를 사용하십시오.")
    python_abs_path = _absolute_path(python_executable) if python_executable else ""
    style_block = _company_style_block(style) if company_style_enabled and style is not None else ""

    blocks = [
        f"""당신은 PPT Master 스킬을 실행하는 헤드리스 one-shot 에이전트입니다.

아래 스킬 문서를 절대경로로 읽고, 입력 원문을 바탕으로 경영진 보고용 PPT를 생성하십시오.
- 스킬 문서 절대경로: {skill_abs_path}
- 입력 설명: {source_text}
- 사용자 요청: {request_text}
- 프로젝트 경로: {PROJECT_PATH}
- Python 실행파일 절대경로: {python_abs_path or "현재 환경의 Python"}

사전 확정 디자인 스펙:
- [Template] {DEFAULT_TEMPLATE}
- [Format] {DEFAULT_FORMAT}
- [Pages] {page_range}
- [Tone] {DEFAULT_TONE}
- [Image] {image_spec}
- [Company Style] {company_style_state}
- [Audience] {audience}
- [CLI] {cli}"""
    ]
    if style_block:
        blocks.append(style_block)

    blocks.append(
        f"""실행 규칙:
1. 이 실행은 헤드리스 one-shot입니다. 사용자에게 질문하지 마십시오.
2. 모든 BLOCKING 확인 게이트는 위 사전 확정 디자인 스펙값으로 이미 선응답된 것으로 간주하고 즉시 진행하십시오. 이는 스킬 규칙 2의 확인 대기 동작을 오버라이드합니다.
3. confirm_ui 서버(포트 5050)를 절대 기동하지 마십시오. 확인이 필요하면 "just confirm in chat" 경로로 자체 확정하고 계속 진행하십시오.
4. 프로젝트는 반드시 {PROJECT_PATH} 에 생성하십시오. 다른 프로젝트 경로를 만들지 마십시오.
5. 모든 Python 스크립트는 반드시 위 Python 실행파일 절대경로로 실행하십시오. `python`, `python3`, `py` 명령을 사용하지 마십시오.
6. {image_rule}
7. 마지막에는 {PROJECT_PATH}/exports/*.pptx 산출을 완료한 뒤, 완성된 PPTX의 절대경로를 한 줄로 출력하십시오."""
    )
    return "\n\n".join(blocks) + "\n"
