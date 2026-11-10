"""정책 시나리오 엔진 — 문서 06 §2.3~2.4.

    uv run python -m welfaretwin.sim.scenario --preset demo
    uv run python -m welfaretwin.sim.scenario --param selection_ratio_livelihood=0.35 --behavior S0

보정(가장 중요): 현행 규칙 + S0(현행 신청주의) 행동에서 '기대 수급 가구 수'가 실제 기초생활보장 수급 가구 수
(e-나라지표, 최근 연도)와 같아지도록 공통 로지스틱 절편 b 하나를 맞춘다.
  P(수급 | 자격, 페르소나 k, 시나리오 s) = σ(logit(p_LLM[k, s]) + b)
변경 시나리오에도 같은 b 를 쓴다 → LLM 은 '변화의 방향·상대 크기', 보정은 '수준'을 맡는다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from welfaretwin.population.personas import assign
from welfaretwin.rules.dsl import RULES_DB, Program
from welfaretwin.rules.engine import RuleSet, evaluate, load_ruleset
from welfaretwin.sim.static import load_population, to_households

ROOT = RULES_DB.parent
NBLS = ["NBLS_LIVELIHOOD", "NBLS_MEDICAL", "NBLS_HOUSING", "NBLS_EDUCATION"]
NBLS_CASH = ["NBLS_LIVELIHOOD", "NBLS_HOUSING", "NBLS_EDUCATION"]


@dataclass
class PolicyChange:
    name: str = "현행"
    params: dict[str, object] = field(default_factory=dict)       # params 값 덮어쓰기
    extra_programs: list[dict] = field(default_factory=list)       # 신규 사업(규칙 dict)
    population_scale: dict[str, float] = field(default_factory=dict)  # 인구 금액 열 배율(백테스트 연도 보정)


def build_ruleset(change: PolicyChange, year: int = 2026) -> RuleSet:
    base = load_ruleset(year=year)
    ps = base.params.model_copy(deep=True)
    for k, v in change.params.items():
        if k not in ps.params:
            raise KeyError(f"params 에 없는 이름: {k}")
        ps.params[k] = ps.params[k].model_copy(update={"value": v})
    progs = dict(base.programs)
    for d in change.extra_programs:
        p = Program.model_validate(d)
        progs[p.id] = p
    return RuleSet(progs, base.libraries, ps)


def _eval_chunk(args) -> list[tuple]:
    hhs, change, program_ids = args
    rs = build_ruleset(change)
    return [(h.hh_id, d.program_id, d.eligible, d.benefit_monthly)
            for h in hhs for d in evaluate(h, rs, program_ids)]


def evaluate_population(change: PolicyChange, program_ids: list[str], sample: int | None = None,
                        workers: int = 6, seed: int = 0,
                        region: tuple[str, str | None] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """region=(시도, 시군구) 이면 그 지역 가구만 전부 평가(표본 추출 없음 — 지자체 사업 영향 분석용)."""
    hh, persons = load_population(ROOT / "data" / "processed")
    if region:
        mask = hh["region_sido"] == region[0]
        if region[1]:
            mask &= hh["region_sigungu"] == region[1]
        hh = hh[mask]
        persons = persons[persons["hh_id"].isin(hh["hh_id"])]
        sample = None
    if sample and sample < len(hh):
        total = len(hh)
        hh = hh.sample(sample, random_state=seed).copy()
        hh["weight"] = hh["weight"] * (total / sample)
        persons = persons[persons["hh_id"].isin(hh["hh_id"])]
    if change.population_scale:
        hh, persons = hh.copy(), persons.copy()
        for col, f in change.population_scale.items():
            df = persons if col in persons.columns else hh
            df[col] = (df[col] * f).round().astype("int64")
    objs = to_households(hh, persons)
    chunks = [(objs[i:i + 1500], change, program_ids) for i in range(0, len(objs), 1500)]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        rows = [r for part in ex.map(_eval_chunk, chunks) for r in part]
    res = pd.DataFrame(rows, columns=["hh_id", "program_id", "eligible", "benefit_monthly"])
    return res, hh, persons


def household_table(res: pd.DataFrame, hh: pd.DataFrame, persons: pd.DataFrame) -> pd.DataFrame:
    """가구별: 기초생활보장 자격(any), 현금급여(생계+주거+교육), 가중치, 페르소나, 인원."""
    nb = res[res["program_id"].isin(NBLS)]
    t = nb.groupby("hh_id").agg(nbls_eligible=("eligible", "any"))
    t = t.join(res[res["program_id"] == "NBLS_LIVELIHOOD"].set_index("hh_id")["eligible"].rename("livelihood_eligible"))
    cash = res[res["program_id"].isin(NBLS_CASH)].groupby("hh_id")["benefit_monthly"].sum().rename("nbls_cash")
    t = t.join(cash)
    t = t.join(hh.set_index("hh_id")[["weight", "region_sido"]])
    t = t.join(persons.groupby("hh_id").size().rename("size"))
    t = t.join(assign(hh, persons))
    return t


def load_behavior() -> pd.DataFrame:
    b = pd.read_parquet(ROOT / "artifacts" / "sim" / "behavior.parquet")
    return b.groupby(["persona_id", "scenario"])["apply_probability"].mean().unstack("scenario")


def _logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def take_up(t: pd.DataFrame, behavior: pd.DataFrame, scenario: str, b: float) -> pd.Series:
    p = t["persona_id"].map(behavior[scenario]).fillna(behavior[scenario].mean())
    return pd.Series(_sigmoid(_logit(p.to_numpy()) + b), index=t.index)


def calibrate(t: pd.DataFrame, behavior: pd.DataFrame, target_households: float, scenario: str = "S0") -> float:
    e = t[t["nbls_eligible"]]
    lo, hi = -15.0, 15.0
    for _ in range(80):
        mid = (lo + hi) / 2
        got = float((e["weight"] * take_up(e, behavior, scenario, mid)).sum())
        lo, hi = (mid, hi) if got < target_households else (lo, mid)
    return (lo + hi) / 2


def actual_recipients() -> dict:
    stats = ROOT / "data" / "processed" / "stats"
    p = pd.read_csv(stats / "nbls_recipients.csv")
    h = pd.read_csv(stats / "nbls_households_by_type.csv")
    y = int(h["year"].max())
    return {"year": y, "households": float(h.loc[h["year"] == y, "합계"].iloc[0]),
            "persons": float(p.loc[p["year"] == y, "수급자수"].iloc[0]) * 1000,
            "source": "e-나라지표 2760 국민기초생활보장 수급 현황"}


def metrics(t: pd.DataFrame, behavior: pd.DataFrame, scenario: str, b: float) -> dict:
    e = t[t["nbls_eligible"]]
    q = take_up(e, behavior, scenario, b)
    w = e["weight"]
    rec = float((w * q).sum())
    return {
        "eligible_households": float(w.sum()),
        "eligible_persons": float((w * e["size"]).sum()),
        "expected_recipient_households": rec,
        "expected_recipient_persons": float((w * q * e["size"]).sum()),
        "non_take_up_households": float(w.sum()) - rec,
        "take_up_rate": rec / float(w.sum()) if len(e) else 0.0,
        "monthly_budget_won": float((w * q * e["nbls_cash"]).sum()),
        "livelihood_eligible_households": float(w[e["livelihood_eligible"]].sum()),
        "livelihood_expected_recipients": float((w * q)[e["livelihood_eligible"]].sum()),
    }


def changed_types(t0: pd.DataFrame, t1: pd.DataFrame, top: int = 10) -> dict:
    """새로 포함/새로 탈락 가구를 페르소나(가구 유형)별로 — 문서 05 Q4 와 연결."""
    desc = pd.read_parquet(ROOT / "artifacts" / "sim" / "behavior.parquet")[["persona_id", "description"]] \
        .drop_duplicates().set_index("persona_id")["description"]
    j = t0[["nbls_eligible", "weight", "persona_id"]].join(t1[["nbls_eligible"]].rename(
        columns={"nbls_eligible": "nbls_eligible_new"}))
    out = {}
    for key, mask in (("newly_included", ~j["nbls_eligible"] & j["nbls_eligible_new"]),
                      ("newly_excluded", j["nbls_eligible"] & ~j["nbls_eligible_new"])):
        g = j[mask].groupby("persona_id")["weight"].sum().sort_values(ascending=False).head(top)
        out[key] = {"households": float(j.loc[mask, "weight"].sum()),
                    "top_types": [{"type": desc.get(k, k), "households": float(v)} for k, v in g.items()]}
    return out


def compare(changes: list[tuple[PolicyChange, str]], sample: int | None = None, workers: int = 6) -> dict:
    """changes[0] 은 기준(현행 규칙 + S0)이어야 한다. 기준에서 보정한 b 를 모든 시나리오에 적용."""
    t_start = time.perf_counter()
    behavior = load_behavior()
    actual = actual_recipients()
    tables, results = {}, []
    for change, scen in changes:
        key = json.dumps({"p": change.params, "x": [d["id"] for d in change.extra_programs],
                          "s": change.population_scale}, sort_keys=True)
        if key not in tables:
            res, hh, persons = evaluate_population(change, NBLS, sample=sample, workers=workers)
            tables[key] = household_table(res, hh, persons)
        results.append((change, scen, tables[key]))
    base_change, base_scen, t0 = results[0]
    b = calibrate(t0, behavior, actual["households"], base_scen)
    out = {"calibration": {"intercept_b": b, "target": actual, "behavior_model_scenarios": list(behavior.columns)},
           "scenarios": []}
    for change, scen, t in results:
        m = metrics(t, behavior, scen, b)
        entry = {"name": f"{change.name} / {scen}", "params": change.params, "behavior": scen, "metrics": m}
        if t is not t0:
            entry["changes_vs_base"] = changed_types(t0, t)
        out["scenarios"].append(entry)
    base_m = out["scenarios"][0]["metrics"]
    for s in out["scenarios"]:
        s["delta_vs_base"] = {k: s["metrics"][k] - base_m[k] for k in base_m}
    out["elapsed_sec"] = round(time.perf_counter() - t_start, 1)
    out["sample_households"] = sample
    return out


PRESETS = {
    "demo": [
        (PolicyChange("현행"), "S0"),
        (PolicyChange("현행"), "S1"),
        (PolicyChange("생계급여 선정기준 32%→35%", {"selection_ratio_livelihood": 0.35}), "S0"),
        (PolicyChange("생계급여 선정기준 32%→35%", {"selection_ratio_livelihood": 0.35}), "S1"),
    ],
}


def _parse_value(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="정책 시나리오 비교(행동 보정 포함)")
    ap.add_argument("--preset", choices=list(PRESETS))
    ap.add_argument("--param", action="append", default=[], help="이름=값 (예: selection_ratio_livelihood=0.35)")
    ap.add_argument("--behavior", default="S0")
    ap.add_argument("--sample", type=int, help="가구 표본 수(빠른 실행)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "demo" / "scenario_demo.json")
    args = ap.parse_args(argv)
    if args.preset:
        changes = PRESETS[args.preset]
    else:
        params = dict(kv.split("=", 1) for kv in args.param)
        changes = [(PolicyChange("현행"), "S0"),
                   (PolicyChange("변경안", {k: _parse_value(v) for k, v in params.items()}), args.behavior)]
    out = compare(changes, sample=args.sample, workers=args.workers)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    c = out["calibration"]
    print(f"보정: 절편 b = {c['intercept_b']:.3f} (목표 {c['target']['year']}년 수급 {c['target']['households']:,.0f}가구)")
    for s in out["scenarios"]:
        m, d = s["metrics"], s["delta_vs_base"]
        print(f"- {s['name']}: 자격 {m['eligible_households']:,.0f}가구, 기대 수급 {m['expected_recipient_households']:,.0f}가구 "
              f"(수급률 {m['take_up_rate']:.1%}), 미신청 {m['non_take_up_households']:,.0f}가구, "
              f"월 재정 {m['monthly_budget_won'] / 1e8:,.0f}억원, 생계급여 자격 {m['livelihood_eligible_households']:,.0f}가구 "
              f"| 기준 대비 수급 {d['expected_recipient_households']:+,.0f}가구, "
              f"재정 {d['monthly_budget_won'] / 1e8:+,.0f}억원")
    print(f"({out['elapsed_sec']}초) → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
