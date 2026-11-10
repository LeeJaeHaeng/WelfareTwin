"""AI 정책 레드팀 — 문서 07.

신규 사업안(자연어 또는 규칙 dict)을 출시 전에 점검한다. 존재 여부·규모는 Z3·평가기가 판정하고,
LLM 은 사업안 → 규칙 추출에만 쓴다(M5 단계 모델: WT_LLM_MODEL_AGENT).
- 영향: 대상 가구·월 재정(합성 인구), 기존 제도와의 중복률
- 배제: 조건 하나 때문에 탈락하는 '평범한' 가구 유형(Z3)
- 급여 절벽: 소득 1만원 증가로 이 사업을 잃는 가구(Z3, 문서 05 Q2)
- 허점(방어 목적 점검): 세대 분리·소득 신고 시점·재산 형태 전환으로 새로 자격을 얻는 가구 수(평가기) + 보완안
- 행정 부담: 확인해야 할 자격 요소·수작업 확인 조건·중복 확인 대상 제도
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pandas as pd

from welfaretwin.model import Household, Person
from welfaretwin.rules.dsl import Program, iter_conditions
from welfaretwin.rules.engine import RuleSet, evaluate
from welfaretwin.rules.extract import extract, from_proposal, to_program
from welfaretwin.rules.testgen import generate
from welfaretwin.sim.scenario import PolicyChange, build_ruleset, evaluate_population
from welfaretwin.sim.static import to_households
from welfaretwin.verify import queries as q
from welfaretwin.verify.explain import describe_household, failed_reasons

OVERLAP_CANDIDATES = ["ENERGY_VOUCHER", "BASIC_PENSION", "NBLS_LIVELIHOOD", "NBLS_HOUSING", "EITC"]

# 규칙에서 쓰는 요소 → 담당자가 확인해야 할 사항(행정 부담 추정)
CHECKS = [
    (("nbls_income_recognized", "earned_income", "business_income", "other_income", "bp_income_recognized"),
     "소득 조사(사회보장정보시스템 연계)"),
    (("assets_", "car_value", "debts"), "재산·부채 조사"),
    (("elig(",), "타 제도 수급 자격 확인"),
    (("p.age", "head_age", "n_elderly", "n_child"), "연령·가구 구성 확인(주민등록)"),
    (("disability", "n_disabled"), "장애 등록 확인"),
    (("single_parent",), "한부모가족 증명 확인"),
    (("housing", "rent_monthly"), "주거 형태·임대차계약 확인"),
    (("is_student",), "재학 확인"),
]


@dataclass
class RedTeamResult:
    proposal: str
    program: dict
    extraction: dict
    impact: dict = field(default_factory=dict)
    exclusion: list[dict] = field(default_factory=list)
    cliff: list[dict] = field(default_factory=list)
    loopholes: list[dict] = field(default_factory=list)
    admin: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)


def program_from_proposal(text: str) -> tuple[Program, dict, dict]:
    svc = from_proposal(text)
    d = extract(svc, "A", role="agent")
    if not d.ok:
        raise ValueError(f"사업안 규칙 추출 실패: {d.errors}")
    prog_dict = to_program(d.raw, svc, "A", d.llm[-1]["model"])
    prog_dict["id"] = f"PROPOSAL_{svc.serv_id}"
    prog_dict["status"] = "draft"
    info = {"quote_issues": d.quote_issues, "llm": d.llm, "not_modeled": d.raw["not_modeled"]}
    return Program.model_validate(prog_dict), prog_dict, info


def _region(prog_dict: dict) -> tuple[str, str | None] | None:
    r = prog_dict.get("region") or {}
    return (r["sido"], r.get("sigungu") or None) if r.get("sido") else None


def impact(prog_dict: dict, sample: int, workers: int) -> dict:
    """지역 사업이면 그 지역 합성 가구 전부를, 아니면 전국 표본을 평가."""
    change = PolicyChange("사업안", extra_programs=[prog_dict])
    pid = prog_dict["id"]
    region = _region(prog_dict)
    res, hh, _ = evaluate_population(change, [pid, *OVERLAP_CANDIDATES], sample=sample, workers=workers,
                                     region=region)
    piv = res.pivot_table(index="hh_id", columns="program_id", values="eligible", aggfunc="first")
    ben = res[res["program_id"] == pid].set_index("hh_id")["benefit_monthly"]
    w = hh.set_index("hh_id")["weight"]
    e = piv[piv[pid]].index
    out = {"eligible_households": float(w.loc[e].sum()),
           "monthly_budget_won_full_take_up": float((w.loc[e] * ben.loc[e]).sum()),
           "by_sido": {k: float(v) for k, v in hh.set_index("hh_id").loc[e].groupby("region_sido")["weight"].sum().items()},
           "overlap_rates": {}}
    for other in OVERLAP_CANDIDATES:
        if len(e):
            out["overlap_rates"][other] = float((w.loc[e] * piv.loc[e, other]).sum() / w.loc[e].sum())
    out["sample_households"] = len(hh)
    out["scope"] = (f"{region[0]} {region[1] or ''}".strip() + " 합성 가구 전체") if region else f"전국 표본 {sample:,}가구"
    return out


def exclusion_types(prog: Program, rs: RuleSet) -> list[dict]:
    """조건 하나만 어긋나 탈락하는 가구(Z3) — '취지상 대상인데 탈락' 후보."""
    out = []
    for case in generate(prog, rs, plain=True):
        if case["expect"]["eligible"]:
            continue
        hh = Household.model_validate(case["household"])
        reasons = failed_reasons(hh, rs, prog.id)
        out.append({"household_text": describe_household(hh), "reasons": reasons,
                    "condition": case["name"].replace("자동 — ", "")})
    return out


def cliffs(rs: RuleSet, pid: str, budget_sec: float) -> list[dict]:
    r = q.cliff(rs, n=1, programs=[pid], budget_sec=budget_sec)
    from welfaretwin.verify.explain import explain
    return [{"text": it["text"], "loss": it["facts"]["loss"]} for it in explain(r, rs)]


def _manipulations(h: Household) -> list[tuple[str, Household]]:
    out = []
    if h.size >= 2:   # 세대 분리: 가구원 한 명이 따로 1인 가구가 되는 경우
        for i, m in enumerate(h.members[1:], start=1):
            if m.age >= 19:
                out.append(("세대 분리", h.model_copy(update={"members": [m], "assets_housing": 0, "assets_general": 0,
                                                              "assets_financial": 0, "car_value": 0, "debts": 0,
                                                              "housing": "other", "rent_monthly": 0})))
    if any(m.earned_income for m in h.members):   # 소득 신고 시점 조정(근로소득 10% 감소로 보이게)
        out.append(("소득 신고 시점 조정", h.model_copy(update={"members": [
            m.model_copy(update={"earned_income": int(m.earned_income * 0.9)}) for m in h.members]})))
    if h.assets_financial > 0 and h.housing in ("jeonse", "monthly_rent"):   # 금융재산 → 임차보증금
        out.append(("재산 형태 전환", h.model_copy(update={"assets_housing": h.assets_housing + h.assets_financial,
                                                         "assets_financial": 0})))
    return out


REMEDIES = {
    "세대 분리": "신청일 기준 일정 기간(예: 6개월) 이전의 세대 구성으로 판정하거나 실거주 확인 조건을 추가",
    "소득 신고 시점 조정": "소득 산정 기간을 최근 3~12개월 평균으로 하고 급격한 감소는 사유 확인",
    "재산 형태 전환": "신청 전 1년 내 재산 형태 변경(금융재산 → 보증금 등)을 확인해 변경 전 기준으로 산정",
}


def loopholes(prog: Program, rs: RuleSet, sample: int) -> list[dict]:
    """방어 목적: 작은 조작으로 자격을 얻게 되는 가구 수 — 규칙상 허점과 보완안만 서술한다.
    지역 사업이면 그 지역 합성 가구 전부(가구 가중치로 환산), 아니면 전국 표본."""
    from welfaretwin.sim.static import ROOT, load_population
    hh, persons = load_population(ROOT / "data" / "processed")
    if prog.region:
        mask = hh["region_sido"] == prog.region.sido
        if prog.region.sigungu:
            mask &= hh["region_sigungu"] == prog.region.sigungu
        hh = hh[mask]
        scale = float(hh["weight"].iloc[0]) if len(hh) else 0.0
    else:
        hh = hh.sample(min(sample, len(hh)), random_state=1)
        scale = 22_494_934 / len(hh) if len(hh) else 0   # 2025 일반가구 수 / 표본
    persons = persons[persons["hh_id"].isin(hh["hh_id"])]
    found: dict[str, dict] = {}
    for h in to_households(hh, persons):
        if {d.program_id: d.eligible for d in evaluate(h, rs, [prog.id])}[prog.id]:
            continue
        for kind, h2 in _manipulations(h):
            if {d.program_id: d.eligible for d in evaluate(h2, rs, [prog.id])}[prog.id]:
                f = found.setdefault(kind, {"kind": kind, "households": 0.0, "example": None})
                f["households"] += scale
                if f["example"] is None:
                    f["example"] = {"before": describe_household(h), "after": describe_household(h2)}
                break
    out = sorted(found.values(), key=lambda x: -x["households"])
    for f in out:
        f["remedy"] = REMEDIES[f["kind"]]
    return out


def admin_burden(prog: Program, overlap: dict) -> dict:
    srcs = [c.expr.src for c in iter_conditions(prog.eligibility)]
    checks = [label for keys, label in CHECKS if any(k in s for s in srcs for k in keys)]
    return {"n_conditions": len(srcs), "checks": checks, "n_checks": len(checks),
            "manual_checks": [n.quote for n in prog.not_modeled],
            "overlap_review": [k for k, v in overlap.items() if v >= 0.2]}


def run(proposal: str, sample: int = 20_000, loophole_sample: int = 6_000, cliff_budget: float = 8.0,
        workers: int = 6) -> RedTeamResult:
    t = {}
    t0 = time.perf_counter()
    prog, prog_dict, info = program_from_proposal(proposal)
    t["extract"] = time.perf_counter() - t0
    rs = build_ruleset(PolicyChange("사업안", extra_programs=[prog_dict]))
    r = RedTeamResult(proposal, prog_dict, info)
    s = time.perf_counter()
    r.impact = impact(prog_dict, sample, workers)
    t["impact"] = time.perf_counter() - s
    s = time.perf_counter()
    r.exclusion = exclusion_types(prog, rs)
    t["exclusion"] = time.perf_counter() - s
    s = time.perf_counter()
    r.cliff = cliffs(rs, prog.id, cliff_budget)
    t["cliff"] = time.perf_counter() - s
    s = time.perf_counter()
    r.loopholes = loopholes(prog, rs, loophole_sample)
    t["loopholes"] = time.perf_counter() - s
    r.admin = admin_burden(prog, r.impact["overlap_rates"])
    t["total"] = time.perf_counter() - t0
    r.timings = {k: round(v, 1) for k, v in t.items()}
    return r


def to_frame(r: RedTeamResult) -> pd.DataFrame:   # 대시보드용
    return pd.DataFrame([{"항목": k, "초": v} for k, v in r.timings.items()])
