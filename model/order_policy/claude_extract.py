"""Claude(Anthropic API) 추출기. 키는 서버 환경변수 ANTHROPIC_API_KEY에만 둔다.

반환 형식은 CompatibleAPIExtractor와 같은 {changes, issues, warnings}이고,
병합·범위·근거 검사는 compile_policy가 결정론적으로 한다.
"""
from __future__ import annotations

import json
import os

from .extract import api_instruction

MODEL = "claude-opus-5-5"


def _change(fields, value):
    return {"type": "object", "additionalProperties": False, "required": ["field", "value", "evidence"],
            "properties": {"field": {"type": "string", "enum": sorted(fields)}, "value": value,
                           "evidence": {"type": "string"}}}


def output_schema():
    """필드마다 값 형식을 고정한 응답 스키마. 숫자 범위와 원문 근거는 compile_policy가 따로 검사한다."""
    from .policy import LIST_FIELDS, PRESETS, RANGES
    texts = {"type": "array", "items": {"type": "string"}}
    budget = {"type": "object", "additionalProperties": False, "required": ["won", "operator"],
              "properties": {"won": {"type": "integer"}, "operator": {"type": "string", "enum": ["lt", "le"]}}}
    changes = [_change({"mode"}, {"type": "string", "enum": sorted(PRESETS)}),
               _change({"budget"}, budget),
               _change({"allow_emergency"}, {"type": "boolean"}),
               _change(LIST_FIELDS, texts),
               _change(RANGES, {"type": "number"})]
    return {"type": "object", "additionalProperties": False, "required": ["changes", "issues", "warnings"],
            "properties": {"changes": {"type": "array", "items": {"anyOf": changes}},
                           "issues": texts, "warnings": texts}}


class ClaudeExtractor:
    """응답 실패·거절 시 규칙 추출기로 넘기지 않는다. 컴파일 결과가 차단된다.

    모델은 POLICY_CLAUDE_MODEL로 바꿀 수 있다. client를 주입하면 네트워크 없이 테스트할 수 있다.
    """
    def __init__(self, *, model=None, client=None):
        self.model = model or os.getenv("POLICY_CLAUDE_MODEL") or MODEL
        self.client = client

    def extract(self, text, catalog=None):
        try:
            import anthropic
        except ImportError:
            raise ValueError("Claude 해석에는 anthropic 패키지가 필요합니다: pip install -r api/requirements.txt") from None
        # Vercel 함수 제한 시간(60초) 안에 끝나도록 재시도는 한 번만.
        client = self.client or anthropic.Anthropic(timeout=25.0, max_retries=1)
        system = [{"type": "text", "text": json.dumps(api_instruction(catalog), ensure_ascii=False),
                   "cache_control": {"type": "ephemeral"}}]
        try:
            response = client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": text}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": output_schema()}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        # 공급사 응답 본문이나 인증 정보를 화면·로그에 노출하지 않는다.
        except anthropic.AuthenticationError:
            raise ValueError("Claude API 키를 확인해주세요; 요청을 실행하지 않았습니다") from None
        except anthropic.RateLimitError:
            raise ValueError("Claude API 사용 한도에 걸렸습니다. 잠시 후 다시 시도해주세요") from None
        except anthropic.APIStatusError as exc:
            raise ValueError(f"Claude API 오류({exc.status_code}); 요청을 실행하지 않았습니다") from None
        except anthropic.APIConnectionError:
            raise ValueError("Claude API 연결 실패; 요청을 실행하지 않았습니다") from None
        if response.stop_reason != "end_turn":
            raise ValueError(f"Claude가 해석을 마치지 못함({response.stop_reason}); 요청을 실행하지 않았습니다")
        try:
            return json.loads(next(b.text for b in response.content if b.type == "text"))
        except (StopIteration, json.JSONDecodeError):
            raise ValueError("Claude가 유효한 JSON 추출 결과를 반환하지 않음") from None
