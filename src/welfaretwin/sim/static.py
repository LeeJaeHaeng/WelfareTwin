"""정적 시뮬레이션 — 합성 인구 전 가구를 평가해 제도별 대상자 수·평균 급여·월 재정을 낸다(문서 06 §1.2).

    uv run python -m welfaretwin.sim.static [--population data/processed] [--workers 4]

입력: data/processed/households.parquet, persons.parquet (population/synth.py 산출)
출력: artifacts/sim/static_<year>.parquet (가구 × 제도 판정), artifacts/sim/static_<year>.json (요약)
가구 가중치(weight)는 '합성 가구 1개가 대표하는 실제 가구 수'.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from welfaretwin.model import Household, Person
from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.engine import evaluate, load_ruleset

ROOT = RULES_DB.parent
HH_FIELDS = ["hh_id", "region_sido", "region_sigungu", "housing", "rent_monthly", "assets_housing",
             "assets_general", "assets_financial", "car_value", "debts", "single_parent",
             "obligor_over_limit", "obligor_medical_capable"]
P_FIELDS = ["age", "sex", "disability", "is_student", "employed", "pregnant",
            "earned_income", "business_income", "other_income"]


def load_population(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    return pd.read_parquet(folder / "households.parquet"), pd.read_parquet(folder / "persons.parquet")


def to_households(hh: pd.DataFrame, persons: pd.DataFrame) -> list[Household]:
    by_hh = {k: g.sort_values("idx") for k, g in persons.groupby("hh_id", sort=False)}
    out = []
    for row in hh[HH_FIELDS].to_dict("records"):
        members = [Person(**{f: m[f] for f in P_FIELDS}) for m in by_hh[row["hh_id"]].to_dict("records")]
        row["region_sigungu"] = row["region_sigungu"] or None
        out.append(Household(members=members, **row))
    return out


def _evaluate_chunk(args) -> list[dict]:
    hhs, year = args
    rs = load_ruleset(year=year)
    rows = []
    for h in hhs:
        for d in evaluate(h, rs):
            rows.append({"hh_id": h.hh_id, "program_id": d.program_id, "eligible": d.eligible,
                         "benefit_monthly": d.benefit_monthly})
    return rows


def run(folder: Path, year: int = 2026, workers: int = 4, chunk: int = 2000) -> tuple[pd.DataFrame, dict]:
    t0 = time.perf_counter()
    hh_df, p_df = load_population(folder)
    hhs = to_households(hh_df, p_df)
    chunks = [(hhs[i:i + chunk], year) for i in range(0, len(hhs), chunk)]
    rows: list[dict] = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(_evaluate_chunk, chunks):
                rows.extend(part)
    else:
        for c in chunks:
            rows.extend(_evaluate_chunk(c))
    res = pd.DataFrame(rows).merge(hh_df[["hh_id", "weight", "region_sido"]], on="hh_id")
    sizes = p_df.groupby("hh_id").size().rename("size")
    res = res.join(sizes, on="hh_id")
    summary = summarize(res, year)
    summary["elapsed_sec"] = round(time.perf_counter() - t0, 1)
    summary["population"] = {"households": len(hh_df), "weighted_households": float(hh_df["weight"].sum()),
                             "source": str(folder)}
    return res, summary


def summarize(res: pd.DataFrame, year: int) -> dict:
    rs = load_ruleset(year=year)
    out = {"year": year, "programs": {}}
    for pid, g in res.groupby("program_id"):
        e = g[g["eligible"]]
        w = e["weight"]
        out["programs"][pid] = {
            "name": rs.programs[pid].name,
            "eligible_households": float(w.sum()),
            "eligible_persons": float((w * e["size"]).sum()),
            "eligible_share": float(w.sum() / g["weight"].sum()),
            "avg_benefit_monthly": float((w * e["benefit_monthly"]).sum() / w.sum()) if len(e) else 0.0,
            "monthly_budget": float((w * e["benefit_monthly"]).sum()),
            "by_sido": {k: float(v) for k, v in e.groupby("region_sido")["weight"].sum().items()},
        }
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="정적 시뮬레이션(전 가구 평가)")
    ap.add_argument("--population", type=Path, default=ROOT / "data" / "processed")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "sim")
    args = ap.parse_args(argv)
    res, summary = run(args.population, args.year, args.workers)
    args.out.mkdir(parents=True, exist_ok=True)
    res.to_parquet(args.out / f"static_{args.year}.parquet", index=False)
    (args.out / f"static_{args.year}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                                       encoding="utf-8")
    print(f"{summary['population']['households']:,}가구 평가 ({summary['elapsed_sec']}초)")
    for pid, s in summary["programs"].items():
        print(f"  {s['name']}: 대상 {s['eligible_households']:,.0f}가구 / {s['eligible_persons']:,.0f}명 "
              f"({s['eligible_share']:.1%}), 평균 {s['avg_benefit_monthly']:,.0f}원, 월 재정 {s['monthly_budget'] / 1e8:,.0f}억원")
    return 0


if __name__ == "__main__":
    sys.exit(main())
