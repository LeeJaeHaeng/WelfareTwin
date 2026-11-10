"""Claude API 호출(캐시 포함) — CLAUDE.md 원칙 5(비용 통제).

- 모델: 규칙 추출 = WT_LLM_MODEL (기본 claude-opus-5), 에이전트·레드팀 = WT_LLM_MODEL_AGENT (기본 claude-haiku-4-5)
- 응답은 .cache/llm/<키>.json 에 저장하고, 같은 키의 요청은 API 를 다시 부르지 않는다.
  키 = (모델, 프롬프트 버전, system, user, 스키마)의 해시
- JSON 스키마 구조화 출력(output_config.format)으로 받는다.
- 안전 분류기 거절에 대비해 서버 측 폴백(fallbacks="default")을 켠다. 거절이면 LLMRefusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = ROOT / ".cache" / "llm"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_AGENT_MODEL = "claude-haiku-4-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    pass


@dataclass
class LLMResult:
    data: dict
    model: str            # 실제로 응답한 모델(폴백 시 다를 수 있음)
    cached: bool
    usage: dict
    cache_key: str


def model_name(role: str = "extract") -> str:
    load_dotenv(ROOT / ".env")
    if role == "agent":
        return os.environ.get("WT_LLM_MODEL_AGENT", "").strip() or DEFAULT_AGENT_MODEL
    return os.environ.get("WT_LLM_MODEL", "").strip() or DEFAULT_MODEL


def _supports_effort_and_fallback(model: str) -> bool:
    """effort·서버 측 폴백은 Opus 5 / Fable 계열만. Haiku 4.5 는 둘 다 미지원."""
    return model.startswith(("claude-opus-5", "claude-fable"))


def _key(model: str, prompt_version: str, system: str, user: str, schema: dict) -> str:
    blob = json.dumps({"m": model, "v": prompt_version, "s": system, "u": user, "f": schema},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def call_json(system: str, user: str, schema: dict, *, prompt_version: str, effort: str = "high",
              max_tokens: int = 16000, client: anthropic.Anthropic | None = None,
              refresh: bool = False, role: str = "extract", sample: int = 0) -> LLMResult:
    """sample: 같은 입력을 여러 번 표본 추출할 때 구분용(캐시 키에 포함).
    temperature 등 샘플링 파라미터는 현재 API 가 받지 않으므로(400) 기본 샘플링의 반복 표본을 쓴다."""
    model = model_name(role)
    key = _key(model, f"{prompt_version}|s={sample}", system, user, schema)
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and not refresh:
        rec = json.loads(path.read_text(encoding="utf-8"))
        return LLMResult(rec["data"], rec["model"], True, rec["usage"], key)

    load_dotenv(ROOT / ".env")
    client = client or anthropic.Anthropic()
    t0 = time.perf_counter()
    common = dict(
        model=model,
        max_tokens=max_tokens,
        # 시스템 프롬프트는 호출마다 공통 → 캐시
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    fmt = {"type": "json_schema", "schema": schema}
    if _supports_effort_and_fallback(model):
        resp = client.beta.messages.create(**common, betas=[FALLBACK_BETA], fallbacks="default",
                                           output_config={"effort": effort, "format": fmt})
    else:
        resp = client.messages.create(**common, output_config={"format": fmt})
    if resp.stop_reason == "refusal":
        raise LLMRefusal(f"모델이 응답을 거절함: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("max_tokens 도달 — 출력이 잘림")
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        "seconds": round(time.perf_counter() - t0, 1),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data": data, "model": resp.model, "usage": usage,
                                "request_id": getattr(resp, "_request_id", None),
                                "prompt_version": prompt_version}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return LLMResult(data, resp.model, False, usage, key)
