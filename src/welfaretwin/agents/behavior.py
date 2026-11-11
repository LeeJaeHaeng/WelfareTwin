"""페르소나 행동 응답(LLM) — 문서 06 §2.2.

v2(기본, 11/11~): 페르소나 클러스터마다 Nemotron-Personas-Korea 가상 개인 3명(population/residents.py)이
각자 1인칭으로 '자격이 있을 때 실제로 신청할 확률' 등을 JSON 으로 답한다. 클러스터 확률 = 3명 평균.
v1(--agents cluster): 클러스터 설명 하나를 3번 표본 추출(재현용으로 유지).
LLM 확률은 그대로 믿지 않는다 — sim/scenario.py 에서 실제 수급 통계로 공통 보정(로지스틱 절편)한다.
모델: WT_LLM_MODEL_AGENT(기본 claude-haiku-4-5). 응답은 .cache/llm/ 에 캐시.

    uv run python -m welfaretwin.agents.behavior [--agents residents|cluster] [--limit 10]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

from welfaretwin.agents.llm import call_json
from welfaretwin.population.personas import Persona, build
from welfaretwin.population.residents import OUT as RESIDENTS
from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
PROMPT_VERSION = "behavior-v1"
PROMPT_VERSION_RESIDENT = "behavior-v2-resident"
REASONS = ["정보 부족", "낙인감", "절차 복잡", "가족·부양의무자 부담", "급여가 적음", "자격 없다고 생각",
           "근로·소득 노출 우려", "필요 없음", "기타"]
CHANNELS = ["온라인", "주민센터 방문", "안내문 받은 뒤 신청", "신청 안 함"]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    name: str
    process: str


SCENARIOS = {
    "S0": Scenario("S0", "현행 신청주의",
                   "본인이 제도를 알아보고 주민센터를 방문하거나 복지로에서 직접 신청한다. "
                   "소득·재산 조사와 금융정보 제공 동의서 등 서류가 필요하고 결정까지 약 30일 걸린다. 먼저 안내해 주지 않는다."),
    "S1": Scenario("S1", "자동 안내 + 간편 신청",
                   "정부가 행정자료로 자격 가능성을 먼저 판단해 문자·우편으로 개별 안내한다. "
                   "안내문의 링크나 전화 한 통으로 신청할 수 있고 소득·재산 서류는 기관 간 자동 연계된다. 결정까지 약 30일."),
}

SYSTEM = """당신은 한국 가구의 실제 행동을 시뮬레이션하는 응답자입니다.
주어진 가구 프로필의 가구주가 되어, 복지급여 자격이 있다고 가정할 때 현실적으로 어떻게 행동할지 답하세요.
한국의 비수급 연구에서 알려진 이유(정보 부족, 낙인감, 복잡한 절차, 가족·부양의무자 부담, 적은 급여액,
자격이 없다고 오해, 소득 노출 우려 등)를 고려하고, 가구 특성(연령·디지털 접근성·근로 여부·소득 수준)에 따라
사람마다 다르게 답하세요. 과장 없이, 같은 조건이면 비슷한 가구의 평균적 행동을 확률로 답합니다.

- apply_probability: 0~1 사이 확률(자격이 있을 때 실제로 신청해 받을 확률)
- reasons: 신청하지 않을 가능성의 주된 이유(정해진 목록에서 0~3개)
- work_change_hours_per_week: 급여를 받게 되면 주당 근로시간을 바꿀지(-10~10, 정수)
- work_change_probability: 그 변화가 실제로 일어날 확률(0~1)
- channel: 신청한다면 가장 가능성 높은 경로
- rationale: 한두 문장 근거"""

SCHEMA = {
    "type": "object",
    "properties": {
        "apply_probability": {"type": "number"},
        "reasons": {"type": "array", "items": {"type": "string", "enum": REASONS}},
        "work_change_hours_per_week": {"type": "integer"},
        "work_change_probability": {"type": "number"},
        "channel": {"type": "string", "enum": CHANNELS},
        "rationale": {"type": "string"},
    },
    "required": ["apply_probability", "reasons", "work_change_hours_per_week", "work_change_probability",
                 "channel", "rationale"],
    "additionalProperties": False,
}


SYSTEM_RESIDENT = """당신은 아래에 소개된 한국 사람입니다. 그 사람의 성격·생활·가족 관계와, 주어진 가구 경제 상황을 바탕으로
1인칭으로 답하세요. 복지급여 자격이 있다고 가정할 때 이 사람이 현실적으로 어떻게 행동할지 답합니다.
한국의 비수급 연구에서 알려진 이유(정보 부족, 낙인감, 복잡한 절차, 가족·부양의무자 부담, 적은 급여액,
자격이 없다고 오해, 소득 노출 우려 등)를 고려하되, 이 사람의 나이·디지털 접근성·근로 여부·가족·지역에 맞게 판단하세요.
가구 경제 상황(소득·주거·장애)은 실제 상황입니다 — 인물 소개와 어긋나면 경제 상황을 따르세요. 과장하지 마세요.

- apply_probability: 0~1 사이 확률(자격이 있을 때 이 사람이 실제로 신청해 받을 확률)
- reasons: 신청하지 않을 가능성의 주된 이유(정해진 목록에서 0~3개)
- work_change_hours_per_week: 급여를 받게 되면 주당 근로시간을 바꿀지(-10~10, 정수)
- work_change_probability: 그 변화가 실제로 일어날 확률(0~1)
- channel: 신청한다면 가장 가능성 높은 경로
- rationale: 1인칭 한두 문장("저는 …")"""


def household_facts(p: Persona) -> str:
    """클러스터(합성 인구)의 경제 상황을 한 사람의 가구 상황으로 — 비율 대신 대표값."""
    pr = p.profile
    tenure = pr["tenure_text"].split(",")[0].rsplit(" ", 1)[0]
    size = p.size_group.replace("인 이상", "명 이상").replace("인", "명")
    parts = [f"가구원 {size}", f"가구 연 소득 약 {pr['income_median_manwon']:,.0f}만원(소득 {p.income_quintile}분위)",
             f"주거 {tenure}", "장애인 가구원 있음" if p.has_disabled else "장애인 가구원 없음"]
    if pr["has_elderly_share"] >= 0.5:
        parts.append("65세 이상 가구원 있음")
    if pr["has_child_share"] >= 0.5:
        parts.append("18세 미만 자녀 있음")
    return ", ".join(parts)


def user_prompt_resident(p: Persona, r: dict, sc: Scenario, expected_benefit: int) -> str:
    job = "일하지 않음" if r["occupation"] == "무직" else r["occupation"]
    return (f"[나] {r['persona']} {r['family_persona']}\n"
            f"[기본 정보] {r['sex']} {int(r['age'])}세 · {r['marital_status']} · {r['family_type']} · "
            f"{r['district'].replace('-', ' ')} 거주 · 학력 {r['education_level']} · 직업 {job}\n"
            f"[우리 가구의 경제 상황 — 실제 상황] {household_facts(p)}\n\n"
            f"[급여] 기초생활보장(생계·주거·의료·교육급여). 우리 가구가 받을 수 있는 현금 급여는 월 약 {expected_benefit:,}원.\n"
            f"[신청 방식 — {sc.name}] {sc.process}\n\n"
            "이 사람으로서, 자격이 있다고 할 때 어떻게 행동할지 답하세요.")


def user_prompt(p: Persona, sc: Scenario, expected_benefit: int) -> str:
    return (f"[가구 프로필] {p.describe()}\n\n"
            f"[급여] 기초생활보장(생계·주거·의료·교육급여). 이 가구가 받을 수 있는 현금 급여는 월 약 {expected_benefit:,}원.\n"
            f"[신청 방식 — {sc.name}] {sc.process}\n\n"
            "이 가구가 자격이 있다고 할 때 어떻게 행동할지 답하세요.")


def ask(p: Persona, sc: Scenario, expected_benefit: int, sample: int) -> dict:
    r = call_json(SYSTEM, user_prompt(p, sc, expected_benefit), SCHEMA, prompt_version=PROMPT_VERSION,
                  role="agent", max_tokens=600, sample=sample)
    d = r.data
    return {"persona_id": p.persona_id, "scenario": sc.scenario_id, "sample": sample,
            "apply_probability": min(1.0, max(0.0, float(d["apply_probability"]))),
            "reasons": "|".join(d["reasons"]), "work_hours_delta": max(-10, min(10, int(d["work_change_hours_per_week"]))),
            "work_change_probability": min(1.0, max(0.0, float(d["work_change_probability"]))),
            "channel": d["channel"], "rationale": d["rationale"], "model": r.model, "cached": r.cached,
            "input_tokens": r.usage.get("input_tokens", 0), "output_tokens": r.usage.get("output_tokens", 0)}


def ask_resident(p: Persona, r: dict, sc: Scenario, expected_benefit: int) -> dict:
    res = call_json(SYSTEM_RESIDENT, user_prompt_resident(p, r, sc, expected_benefit), SCHEMA,
                    prompt_version=PROMPT_VERSION_RESIDENT, role="agent", max_tokens=600, sample=0)
    d = res.data
    return {"persona_id": p.persona_id, "scenario": sc.scenario_id, "sample": int(r["slot"]),
            "apply_probability": min(1.0, max(0.0, float(d["apply_probability"]))),
            "reasons": "|".join(d["reasons"]), "work_hours_delta": max(-10, min(10, int(d["work_change_hours_per_week"]))),
            "work_change_probability": min(1.0, max(0.0, float(d["work_change_probability"]))),
            "channel": d["channel"], "rationale": d["rationale"], "model": res.model, "cached": res.cached,
            "input_tokens": res.usage.get("input_tokens", 0), "output_tokens": res.usage.get("output_tokens", 0),
            "resident_uuid": r["uuid"]}


def expected_benefits(pid: pd.Series) -> dict[str, int]:
    """페르소나별 기초생활보장 현금급여(생계+주거) 평균 — 정적 시뮬레이션 결과에서. 대상이 없으면 전체 평균."""
    res = pd.read_parquet(ROOT / "artifacts" / "sim" / "static_2026.parquet")
    nb = res[res["program_id"].isin(["NBLS_LIVELIHOOD", "NBLS_HOUSING"])]
    per_hh = nb.groupby("hh_id").agg(cash=("benefit_monthly", "sum"), eligible=("eligible", "any"), w=("weight", "first"))
    per_hh = per_hh[per_hh["eligible"]].join(pid)
    overall = float((per_hh["cash"] * per_hh["w"]).sum() / per_hh["w"].sum())
    by = per_hh.groupby("persona_id").apply(lambda g: (g["cash"] * g["w"]).sum() / g["w"].sum())
    return {k: int(round(by.get(k, overall), -3)) for k in pid.unique()}


def run(samples: int = 3, limit: int | None = None, workers: int = 8, scenarios=("S0", "S1"),
        agents: str = "residents") -> pd.DataFrame:
    hh = pd.read_parquet(ROOT / "data" / "processed" / "households.parquet")
    persons = pd.read_parquet(ROOT / "data" / "processed" / "persons.parquet")
    personas, pid = build(hh, persons)
    personas = sorted(personas, key=lambda p: -p.weight)[:limit] if limit else personas
    benefit = expected_benefits(pid)
    if agents == "residents":       # v2: 클러스터마다 가상 개인 3명이 각자 답함
        res = pd.read_parquet(RESIDENTS)
        by = {k: g.to_dict("records") for k, g in res.groupby("persona_id")}
        jobs = [(p, r, SCENARIOS[s], benefit[p.persona_id]) for p in personas for s in scenarios for r in by[p.persona_id]]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(lambda j: ask_resident(*j), jobs))
    else:                           # v1: 클러스터 설명 하나를 표본 반복
        jobs = [(p, SCENARIOS[s], benefit[p.persona_id], k) for p in personas for s in scenarios for k in range(samples)]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(lambda j: ask(*j), jobs))
    df = pd.DataFrame(rows)
    meta = pd.DataFrame([{"persona_id": p.persona_id, "description": p.describe(), "weight": p.weight,
                          "n_households": p.n_households, "expected_benefit": benefit[p.persona_id]} for p in personas])
    return df.merge(meta, on="persona_id")


def cost_usd(df: pd.DataFrame) -> float:
    """claude-haiku-4-5 단가(USD/1M): 입력 1, 출력 5 — 캐시 적중 건은 0."""
    new = df[~df["cached"]]
    return float((new["input_tokens"] * 1 + new["output_tokens"] * 5).sum() / 1e6)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="페르소나 행동 응답 생성(LLM)")
    ap.add_argument("--agents", choices=["residents", "cluster"], default="residents",
                    help="residents: Nemotron 가상 개인 3명(v2, 기본) / cluster: 클러스터 설명 표본 반복(v1)")
    ap.add_argument("--samples", type=int, default=3, help="cluster 모드의 표본 수")
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "sim" / "behavior.parquet")
    ap.add_argument("--limit", type=int, help="가중치 큰 순으로 일부 페르소나만(파일럿)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    df = run(args.samples, args.limit, args.workers, agents=args.agents)
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    agg = df.groupby(["scenario"]).apply(lambda g: (g["apply_probability"] * g["weight"]).sum() / g["weight"].sum())
    print(f"{df['persona_id'].nunique()}개 페르소나 × {df['scenario'].nunique()}개 시나리오 × 3({args.agents}) = {len(df)}건, "
          f"신규 호출 {int((~df['cached']).sum())}건, 비용 ${cost_usd(df):.3f}")
    print("가중 평균 신청확률(보정 전):", {k: round(v, 3) for k, v in agg.items()})
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
