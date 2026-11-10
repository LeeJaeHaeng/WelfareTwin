"""백테스트 — 문서 06 §3 (B2: 기준중위소득 연도별 인상).

    uv run python -m welfaretwin.backtest.run --base 2024 --target 2025

1. 기준 연도 params 로 합성 인구를 평가하고, 기준 연도 실제 수급 가구 수에 맞춰 신청 확률 절편 b 를 보정
2. 같은 b 로 비교 연도 params 를 적용해 수급 가구·인원을 예측
3. 예측 증가분 vs 실제 증가분(e-나라지표) → 오차율 = |예측 증가 − 실제 증가| / 실제 증가 (목표 ±10%, 미달이어도 그대로 보고)

연도별 기준값: rules_db/params/backtest_<연도>.yaml 에 2026 대비 바뀐 값만(출처 포함) 두고 나머지는 2026 값을 쓴다.
한계(화면·발표에 명시): 합성 인구의 소득·재산은 고정(2024년 소득 기준) — 실제 증가에는 소득 변화·경기·제도 운영 변화가
섞여 있다. 시뮬레이션은 '기준값 변화만의 효과'를 본다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import yaml

from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.params import Param
from welfaretwin.sim.scenario import (
    NBLS, PolicyChange, actual_recipients, calibrate, evaluate_population, household_table, load_behavior, metrics,
)

ROOT = RULES_DB.parent


def year_overrides(year: int) -> tuple[dict, list[dict]]:
    """backtest_<year>.yaml → (params 덮어쓰기 dict, 출처 목록). 2026 이면 덮어쓰기 없음."""
    if year == 2026:
        return {}, []
    path = RULES_DB / "params" / f"backtest_{year}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out, sources = {}, []
    for name, entry in data["params"].items():
        p = Param.model_validate(entry)
        if not p.verified:
            raise ValueError(f"{path.name}: {name} 미확인 값은 백테스트에 쓰지 않음")
        out[name] = p.value
        sources.append({"param": name, **p.source.model_dump()})
    return out, sources


def actual_by_year() -> dict[int, dict]:
    stats = ROOT / "data" / "processed" / "stats"
    p = pd.read_csv(stats / "nbls_recipients.csv").set_index("year")["수급자수"] * 1000
    h = pd.read_csv(stats / "nbls_households_by_type.csv").set_index("year")["합계"]
    return {int(y): {"households": float(h[y]), "persons": float(p[y])} for y in h.index}


def run(base: int, target: int, sample: int | None = None, workers: int = 6, scenario: str = "S0") -> dict:
    actual = actual_by_year()
    behavior = load_behavior()
    tables, sources = {}, {}
    for year in (base, target):
        ov, src = year_overrides(year)
        res, hh, persons = evaluate_population(PolicyChange(f"{year} 기준값", ov), NBLS, sample=sample, workers=workers)
        tables[year], sources[year] = household_table(res, hh, persons), src
    b = calibrate(tables[base], behavior, actual[base]["households"], scenario)
    m = {y: metrics(tables[y], behavior, scenario, b) for y in (base, target)}
    pred_dh = m[target]["expected_recipient_households"] - m[base]["expected_recipient_households"]
    act_dh = actual[target]["households"] - actual[base]["households"]
    pred_dp = m[target]["expected_recipient_persons"] - m[base]["expected_recipient_persons"]
    act_dp = actual[target]["persons"] - actual[base]["persons"]
    return {
        "id": f"B2_{base}_{target}",
        "description": "기준중위소득·기초연금 기준값의 연도별 변화만 반영(인구 소득·재산 고정)",
        "base_year": base, "target_year": target, "intercept_b": b,
        "metrics": {str(y): m[y] for y in m},
        "actual": {str(y): actual[y] for y in (base, target)},
        "households": {"predicted_increase": pred_dh, "actual_increase": act_dh,
                       "error_rate": abs(pred_dh - act_dh) / abs(act_dh) if act_dh else None},
        "persons": {"predicted_increase": pred_dp, "actual_increase": act_dp,
                    "error_rate": abs(pred_dp - act_dp) / abs(act_dp) if act_dp else None},
        "eligible_increase_households": m[target]["eligible_households"] - m[base]["eligible_households"],
        "series_actual": {str(y): v for y, v in actual.items()},
        "sources": {str(k): v for k, v in sources.items()},
        "limitations": [
            "합성 인구의 소득·재산은 고정(가계금융복지조사 2025 = 2024년 소득) — 소득 증가로 인한 탈락은 반영 안 됨",
            "재산 기본공제·근로소득공제 등 연도별 값이 확인되지 않은 항목은 2026 값을 사용",
            "실제 수급자 증감에는 경기·신청 행태·제도 운영 변화가 섞여 있음",
            "B1(2021.10 생계급여 부양의무자 기준 폐지)은 가족관계 데이터 부재로 MVP 미수행",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="백테스트(B2)")
    ap.add_argument("--base", type=int, default=2024)
    ap.add_argument("--target", type=int, default=2025)
    ap.add_argument("--sample", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "backtest")
    args = ap.parse_args(argv)
    r = run(args.base, args.target, args.sample, args.workers)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{r['id']}.json"
    path.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "artifacts" / "demo" / f"backtest_{r['id']}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    h, p = r["households"], r["persons"]
    print(f"{r['id']}: 보정 절편 b={r['intercept_b']:.3f}")
    print(f"  수급 가구 증가 — 예측 {h['predicted_increase']:+,.0f} / 실제 {h['actual_increase']:+,.0f} → 오차율 {h['error_rate']:.1%}")
    print(f"  수급 인원 증가 — 예측 {p['predicted_increase']:+,.0f} / 실제 {p['actual_increase']:+,.0f} → 오차율 {p['error_rate']:.1%}")
    print(f"  (자격 가구 증가 {r['eligible_increase_households']:+,.0f})  → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
