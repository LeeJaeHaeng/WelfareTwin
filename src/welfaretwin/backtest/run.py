"""백테스트 — 문서 06 §3 (B2: 기준중위소득 연도별 인상).

    uv run python -m welfaretwin.backtest.run --base 2024 --target 2025

1. 기준 연도 params 로 합성 인구를 평가하고, 기준 연도 실제 수급 가구 수에 맞춰 신청 확률 절편 b 를 보정
2. 같은 b 로 비교 연도 params 를 적용해 수급 가구·인원을 예측
3. 예측 증가분 vs 실제 증가분(e-나라지표) → 오차율 = |예측 증가 − 실제 증가| / 실제 증가 (목표 ±10%, 미달이어도 그대로 보고)

연도별 기준값: rules_db/params/backtest_<연도>.yaml 에 2026 대비 바뀐 값만(출처 포함) 두고 나머지는 2026 값을 쓴다.
인구 보정(rules_db/params/backtest_growth.yaml, 출처 포함): 합성 인구는 '2024년 소득 + 2025년 3월 자산'이므로
기준 연도 2024 는 자산·부채를 2024년 3월 수준으로 되돌리고, 비교 연도 2025 는 소득을 2025년 수준으로 올린다.
--no-growth 로 끄면 '기준값 변화만의 효과'(이전 방식)를 본다.
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


def growth() -> tuple[dict[str, float], list[dict]]:
    path = RULES_DB / "params" / "backtest_growth.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    vals, src = {}, []
    for name, entry in data["params"].items():
        p = Param.model_validate(entry)
        vals[name] = float(p.value)
        src.append({"param": name, **p.source.model_dump()})
    return vals, src


def population_scales(year: int, base: int, target: int, g: dict[str, float]) -> dict[str, float]:
    """합성 인구(2024 소득, 2025.3 자산)를 해당 연도 수준으로."""
    if year == base == 2024:
        return {"assets_housing": 1 / g["asset_growth_real"], "assets_general": 1 / g["asset_growth_real"],
                "assets_financial": 1 / g["asset_growth_financial"], "debts": 1 / g["debt_growth"]}
    if year == target == 2025:
        return {"earned_income": g["income_growth_earned"], "business_income": g["income_growth_business"],
                "other_income": g["income_growth_other"]}
    raise ValueError("인구 보정은 2024→2025 백테스트만 지원")


def actual_by_year() -> dict[int, dict]:
    stats = ROOT / "data" / "processed" / "stats"
    p = pd.read_csv(stats / "nbls_recipients.csv").set_index("year")["수급자수"] * 1000
    h = pd.read_csv(stats / "nbls_households_by_type.csv").set_index("year")["합계"]
    return {int(y): {"households": float(h[y]), "persons": float(p[y])} for y in h.index}


def run(base: int, target: int, sample: int | None = None, workers: int = 6, scenario: str = "S0",
        use_growth: bool = True) -> dict:
    actual = actual_by_year()
    behavior = load_behavior()
    g, g_src = growth() if use_growth else ({}, [])
    tables, sources = {}, {}
    for year in (base, target):
        ov, src = year_overrides(year)
        scale = population_scales(year, base, target, g) if use_growth else {}
        res, hh, persons = evaluate_population(PolicyChange(f"{year} 기준값", ov, population_scale=scale), NBLS,
                                               sample=sample, workers=workers)
        tables[year], sources[year] = household_table(res, hh, persons), src
    b = calibrate(tables[base], behavior, actual[base]["households"], scenario)
    m = {y: metrics(tables[y], behavior, scenario, b) for y in (base, target)}
    pred_dh = m[target]["expected_recipient_households"] - m[base]["expected_recipient_households"]
    act_dh = actual[target]["households"] - actual[base]["households"]
    pred_dp = m[target]["expected_recipient_persons"] - m[base]["expected_recipient_persons"]
    act_dp = actual[target]["persons"] - actual[base]["persons"]
    return {
        "id": f"B2_{base}_{target}" + ("" if use_growth else "_fixedpop"),
        "description": ("기준중위소득·기초연금 기준값 + 가구 소득·자산 증가(가계동향·가계금융복지조사) 반영" if use_growth
                        else "기준중위소득·기초연금 기준값의 연도별 변화만 반영(인구 소득·재산 고정)"),
        "population_growth": g, "population_growth_sources": g_src,
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
            ("소득·자산 증가는 전국 평균 증가율을 모든 가구에 같은 비율로 적용(저소득층의 다른 증가율 미반영)" if use_growth
             else "합성 인구의 소득·재산은 고정(가계금융복지조사 2025 = 2024년 소득) — 소득 증가로 인한 탈락은 반영 안 됨"),
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
    ap.add_argument("--no-growth", action="store_true", help="인구 소득·자산 보정 없이(기준값 변화만)")
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "backtest")
    args = ap.parse_args(argv)
    r = run(args.base, args.target, args.sample, args.workers, use_growth=not args.no_growth)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{r['id']}.json"
    path.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    demo_name = "backtest_B2_2024_2025.json" if not args.no_growth else f"backtest_{r['id']}.json"
    (ROOT / "artifacts" / "demo" / demo_name).write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    h, p = r["households"], r["persons"]
    print(f"{r['id']}: 보정 절편 b={r['intercept_b']:.3f}")
    print(f"  수급 가구 증가 — 예측 {h['predicted_increase']:+,.0f} / 실제 {h['actual_increase']:+,.0f} → 오차율 {h['error_rate']:.1%}")
    print(f"  수급 인원 증가 — 예측 {p['predicted_increase']:+,.0f} / 실제 {p['actual_increase']:+,.0f} → 오차율 {p['error_rate']:.1%}")
    print(f"  (자격 가구 증가 {r['eligible_increase_households']:+,.0f})  → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
