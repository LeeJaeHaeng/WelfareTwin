"""LLM 규칙 추출 — 문서 04 §4.

지자체 복지서비스 원문(data/raw/local/<servId>.json) → 규칙 초안(DSL) 두 벌(프롬프트 A·B).
- A: 원문을 바로 규칙으로 옮긴다.
- B: 먼저 원자 조건을 나열한 뒤 규칙으로 옮긴다(독립 재추출).
검증: 스키마·DSL 파싱(실패 시 오류를 알려 1회 재시도), 인용문이 원문 부분 문자열인지(환각 탐지).
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from welfaretwin.agents.llm import LLMResult, call_json
from welfaretwin.rules.dsl import RULES_DB, Program
from welfaretwin.rules.engine import RuleSet, load_ruleset

RAW_LOCAL = RULES_DB.parent / "data" / "raw" / "local"
PROMPT_VERSION = "extract-v1"


@dataclass
class ServiceText:
    serv_id: str
    name: str
    sido: str
    sigungu: str
    dept: str
    target: str
    criteria: str
    benefit: str
    cycle: str
    provision: str

    def prompt_text(self) -> str:
        return (f"[서비스ID] {self.serv_id}\n[서비스명] {self.name}\n[지역] {self.sido} {self.sigungu}\n"
                f"[담당] {self.dept}\n[지원주기] {self.cycle}\n[제공유형] {self.provision}\n"
                f"[지원대상]\n{self.target}\n\n[선정기준]\n{self.criteria}\n\n[지원내용]\n{self.benefit}")

    def full(self) -> str:
        return "\n".join([self.name, self.target, self.criteria, self.benefit])

    def digest(self) -> str:
        return hashlib.sha256(self.prompt_text().encode()).hexdigest()[:16]


def load_service(serv_id: str) -> ServiceText:
    rec = json.loads((RAW_LOCAL / f"{serv_id}.json").read_text(encoding="utf-8"))
    root = ET.fromstring(rec["raw_xml"])
    g = lambda t: (root.findtext(t) or "").strip()  # noqa: E731
    return ServiceText(serv_id, g("servNm"), g("ctpvNm"), g("sggNm"), g("bizChrDeptNm"), g("sprtTrgtCn"),
                       g("slctCritCn"), g("alwServCn"), g("sprtCycNm"), g("srvPvsnNm"))


SYSTEM = """당신은 한국 복지서비스의 선정기준 원문을 '실행 가능한 규칙(Rules-as-Code)'으로 옮기는 전문가입니다.
출력은 지정된 JSON 스키마를 따르며, 각 조건은 아래 표현식 언어로 쓴 파이썬식 식(expr)입니다.

## 표현식 언어
- 가구 특성 hh.<이름> (금액은 원/월):
  size(가구원 수), n_elderly(65세 이상 수), n_child(18세 미만 수), n_disabled(장애인 수),
  n_severe_disabled(중증 장애인 수), n_pregnant(임산부 수), earned_income(근로소득 합),
  business_income(사업소득 합), other_income(기타소득 합), assets_housing(주거용재산·보증금),
  assets_general(일반재산), assets_financial(금융재산), car_value(자동차), debts(부채),
  rent_monthly(월세), housing('own'|'jeonse'|'monthly_rent'|'other'), single_parent(한부모 여부, bool),
  head_age(가구주 나이)
- 가구원 속성 p.<이름> — sum(...)/count(...) 안에서만: age, sex('M'|'F'), disability('none'|'mild'|'severe'),
  is_student, employed, pregnant, earned_income, business_income, other_income
- 함수: min, max, if(c, a, b), round, floor, ceil, median_income(가구원수)(2026 기준중위소득, 원/월),
  sum(가구원식), count(가구원조건), elig('제도ID')(다른 제도 자격, bool)
- 미리 계산된 값(이름 그대로 사용): nbls_income_recognized(기초생활보장 소득인정액, 원/월),
  bp_income_recognized(기초연금 소득인정액, 원/월)
- 제도ID: NBLS_LIVELIHOOD(생계급여) NBLS_MEDICAL(의료급여) NBLS_HOUSING(주거급여) NBLS_EDUCATION(교육급여)
  BASIC_PENSION(기초연금) EITC(근로장려금) ENERGY_VOUCHER(에너지바우처)
- 연산: + - * / 비교 and or not. 변수×변수 곱셈 금지(상수 곱셈만), 나눗셈은 상수로만.

## 옮기는 규칙
- "기준 중위소득 N% 이하" → nbls_income_recognized <= round(median_income(hh.size) * N / 100).
  지자체 사업은 건강보험료 등 다른 소득 기준을 쓰기도 하므로 원문이 '소득인정액'을 명시하지 않으면 uncertain=true.
- "기초생활수급자" → elig('NBLS_LIVELIHOOD') or elig('NBLS_MEDICAL') or elig('NBLS_HOUSING') or elig('NBLS_EDUCATION').
  특정 급여가 명시되면 그 급여만(예: "의료급여 수급자" → elig('NBLS_MEDICAL')).
- "차상위계층" → 국민기초생활보장법상 소득인정액 기준 중위소득 50% 이하이고 수급자가 아닌 계층:
  nbls_income_recognized <= round(median_income(hh.size) * 50 / 100). 원문에 '차상위'가 있어야 하며 uncertain=true.
- 나이 조건 "만 N세 이상" → 해당하는 가구원이 있음: count(p.age >= N) >= 1. 범위면 count(p.age >= a and p.age <= b) >= 1.
- "독거/단독가구" → hh.size == 1. "한부모" → hh.single_parent. "장애인" → hh.n_disabled >= 1.
- "주택 소유자 제외" → hh.housing != 'own' (uncertain=true: 거주하지 않는 주택 소유는 표현 불가).
- 재산 기준 "재산 X원 이하" → hh.assets_housing + hh.assets_general + hh.assets_financial + hh.car_value - hh.debts <= X
  (부채 차감 여부가 원문에 없으면 uncertain=true).
- 거주지·주소 조건은 식으로 쓰지 말고 region(sido, sigungu)으로 내보낸다. 시군구가 없으면 sigungu는 빈 문자열.
- 표현할 수 없는 조건(거주 기간, 특정 질병·진단, 공무원 여부, 다른 사업 수혜 여부, 선착순·예산 소진, 기관 판단 등)은
  conditions 에 넣지 말고 not_modeled 에 원문 인용과 이유를 적는다.
- 여러 대상 유형 중 하나면 되는 경우(①②③ 중 하나)는 하나의 조건 식에서 or 로 묶는다.
- 급여(benefit): 원 단위 식. period 는 monthly(매월)·annual(연간)·once(1회성).
  가구원별로 주면 sum(if(조건, 금액, 0)). "최대 N원"·"N원 이내"는 N 을 쓰고 uncertain=true.
  금액을 정할 수 없는 현물·서비스·비율 지원(본인부담 N% 등)은 expr "0", uncertain=true.

## 반드시 지킬 것
- 원문에 없는 수치를 만들지 않는다. 식에 쓴 모든 수치는 인용문(quote)에 근거가 있어야 한다.
- quote 는 원문에서 그대로 복사한 연속된 문자열(띄어쓰기 포함)이어야 한다. 요약·수정 금지.
- 불확실하면 uncertain=true 로 표시한다. 추측으로 조건을 늘리거나 줄이지 않는다.
"""

_COND = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "expr": {"type": "string"},
        "quote": {"type": "string"},
        "uncertain": {"type": "boolean"},
    },
    "required": ["label", "expr", "quote", "uncertain"],
    "additionalProperties": False,
}
_BODY = {
    "region": {
        "type": "object",
        "properties": {"sido": {"type": "string"}, "sigungu": {"type": "string"}},
        "required": ["sido", "sigungu"], "additionalProperties": False,
    },
    "conditions": {"type": "array", "items": _COND},
    "benefit": {
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["monthly", "annual", "once"]},
            "expr": {"type": "string"},
            "quote": {"type": "string"},
            "uncertain": {"type": "boolean"},
        },
        "required": ["period", "expr", "quote", "uncertain"], "additionalProperties": False,
    },
    "not_modeled": {
        "type": "array",
        "items": {"type": "object", "properties": {"quote": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["quote", "reason"], "additionalProperties": False},
    },
    "notes": {"type": "string"},
}
SCHEMA_A = {"type": "object", "properties": _BODY, "required": list(_BODY), "additionalProperties": False}
SCHEMA_B = {
    "type": "object",
    "properties": {"atomic_criteria": {"type": "array", "items": {"type": "string"}}, **_BODY},
    "required": ["atomic_criteria", *_BODY], "additionalProperties": False,
}
INSTRUCTION = {
    "A": "아래 복지서비스 원문을 규칙으로 옮겨 주세요.",
    "B": ("아래 복지서비스 원문에서 먼저 자격 판단에 쓰이는 원자 조건을 빠짐없이 atomic_criteria 에 나열한 뒤, "
          "각 원자 조건을 표현식으로 옮기거나 not_modeled 로 분류해 규칙을 만들어 주세요."),
}


@dataclass
class Draft:
    variant: str
    service: ServiceText
    raw: dict
    program: Program | None = None
    errors: list[str] = field(default_factory=list)
    quote_issues: list[str] = field(default_factory=list)
    llm: list[dict] = field(default_factory=list)   # 호출 기록(모델, 캐시 여부, 토큰)

    @property
    def ok(self) -> bool:
        return self.program is not None and not self.errors


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def check_quotes(raw: dict, svc: ServiceText) -> list[str]:
    """인용문이 원문 부분 문자열인지(공백 무시) — 환각 탐지."""
    src = _norm(svc.full())
    issues = []
    items = [("조건", c["label"], c["quote"]) for c in raw["conditions"]]
    items += [("급여", "benefit", raw["benefit"]["quote"])]
    items += [("미모델링", n["reason"], n["quote"]) for n in raw["not_modeled"]]
    for kind, label, quote in items:
        if not quote.strip():
            issues.append(f"{kind} '{label}': 인용문 없음")
        elif _norm(quote) not in src:
            issues.append(f"{kind} '{label}': 원문에 없는 인용 — {quote[:60]}")
    return issues


def to_program(raw: dict, svc: ServiceText, variant: str, model: str) -> dict:
    pid = f"LOCAL_{svc.serv_id}"
    ref = {"doc": f"한국사회보장정보원 지자체복지서비스 상세 {svc.serv_id} ({svc.name})",
           "clause": "지원대상·선정기준·지원내용"}
    conds = [{"expr": c["expr"], "label": c["label"], "quote": c["quote"], "uncertain": c["uncertain"], "ref": 0}
             for c in raw["conditions"]]
    if not conds:   # 조건이 모두 미모델링이면 지역 조건만 남는다
        conds = [{"expr": "hh.size >= 1", "label": "(표현 가능한 조건 없음)", "ref": 0}]
    b = raw["benefit"]
    period = "annual" if b["period"] in ("annual", "once") else "monthly"
    assumptions = ["시군구 단위 사업도 시도 단위로 판정(합성 인구·형식검증에 시군구 없음)"]
    if b["period"] == "once":
        assumptions.append("1회성 지원은 연 1회 지급으로 보고 월 환산(÷12)")
    if raw["not_modeled"]:
        assumptions.append(f"표현하지 못한 조건 {len(raw['not_modeled'])}개(not_modeled) → 대상 과대 추정")
    return {
        "id": pid, "name": svc.name, "level": "local",
        "region": {"sido": raw["region"]["sido"] or svc.sido, "sigungu": raw["region"]["sigungu"] or None},
        "unit": "household", "source_refs": [ref], "assumptions": assumptions,
        "eligibility": {"all": conds},
        "benefit": {period: b["expr"], "quote": b["quote"], "uncertain": b["uncertain"], "ref": 0},
        "not_modeled": raw["not_modeled"], "status": "draft",
        "provenance": {"serv_id": svc.serv_id, "variant": variant, "model": model,
                       "prompt_version": PROMPT_VERSION, "source_hash": svc.digest(), "notes": raw.get("notes", "")},
    }


def validate(prog_dict: dict, base: RuleSet) -> tuple[Program | None, list[str]]:
    try:
        prog = Program.model_validate(prog_dict)
        RuleSet({**base.programs, prog.id: prog}, base.libraries, base.params)
        return prog, []
    except Exception as e:  # noqa: BLE001 — 오류 메시지를 LLM 재시도에 그대로 전달
        return None, [str(e).splitlines()[0] if len(str(e)) < 300 else str(e)[:600]]


def extract(svc: ServiceText, variant: str, base: RuleSet | None = None, refresh: bool = False) -> Draft:
    base = base or load_ruleset()
    schema = SCHEMA_A if variant == "A" else SCHEMA_B
    user = f"{INSTRUCTION[variant]}\n\n{svc.prompt_text()}"
    calls = []
    res: LLMResult = call_json(SYSTEM, user, schema, prompt_version=f"{PROMPT_VERSION}-{variant}", refresh=refresh)
    calls.append({"model": res.model, "cached": res.cached, **res.usage})
    prog, errors = validate(to_program(res.data, svc, variant, res.model), base)
    if errors:   # 스키마·DSL 위반 → 오류를 알려 1회 재시도(문서 04 §4)
        retry_user = (f"{user}\n\n[이전 출력의 오류 — 표현식 언어 규칙에 맞게 고쳐서 다시 출력]\n"
                      + "\n".join(errors) + "\n[이전 출력]\n" + json.dumps(res.data, ensure_ascii=False))
        res = call_json(SYSTEM, retry_user, schema, prompt_version=f"{PROMPT_VERSION}-{variant}-retry",
                        refresh=refresh)
        calls.append({"model": res.model, "cached": res.cached, **res.usage})
        prog, errors = validate(to_program(res.data, svc, variant, res.model), base)
    d = Draft(variant, svc, res.data, prog, errors, check_quotes(res.data, svc), calls)
    return d
