"""정책 손잡이 격자 — 웹 '가상 대한민국'에서 슬라이더·토글을 움직이면 바로 결과가 바뀌도록 미리 계산한다.

생계급여 선정기준(기준중위소득 대비 25~40%, 1%p 간격) × 신청 방식(S0 현행 신청 / S1 자동 안내) 마다
합성 인구 10만 가구 전체를 평가해(신청 방식은 자격과 무관하므로 선정기준당 평가 1회) 시나리오 엔진과 같은 식
(scenario.metrics — 유형별 LLM 응답 평균을 현행·S0 기준 공통 절편 b 로 보정)으로 지표를 낸다.
  national : 전국 지표(scenario.metrics 그대로)
  province : 시도별 기대 수급·생계급여 수급·월 재정·자격 가구
  age      : 가구주 연령대별 기대 수급·생계급여 수급
  liv_share: 유형별 '기초생활보장 자격 가구 중 생계급여 자격' 비율(웹 지도의 점 상태용)

    uv run python -m welfaretwin.sim.policy_grid            # → artifacts/demo/policy_grid.json (수 분)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import pandas as pd

from welfaretwin.population.personas import AGE_GROUPS
from welfaretwin.population.residents import province_of
from welfaretwin.sim.scenario import (
    NBLS, ROOT, PolicyChange, actual_recipients, calibrate, evaluate_population, household_table, load_behavior, metrics,
    take_up,
)

OUT = ROOT / "artifacts" / "demo" / "policy_grid.json"
RATIOS = [round(x / 100, 2) for x in range(25, 41)]
BEHAVIORS = ("S0", "S1")


def _group(e: pd.DataFrame, q: pd.Series, key: pd.Series) -> dict:
    d = pd.DataFrame({"k": key, "w": e["weight"], "q": q, "liv": e["livelihood_eligible"].fillna(False).astype(bool),
                      "cash": e["nbls_cash"]})
    out = {}
    for k, g in d.groupby("k"):
        out[str(k)] = {"eligible": round(float(g["w"].sum())), "recipients": round(float((g["w"] * g["q"]).sum())),
                       "livelihood_recipients": round(float((g["w"] * g["q"])[g["liv"]].sum())),
                       "budget": round(float((g["w"] * g["q"] * g["cash"]).sum()))}
    return out


def run(workers: int = 6) -> dict:
    behavior = load_behavior()
    tables = {}
    t0 = time.perf_counter()
    for r in RATIOS:
        res, hh, persons = evaluate_population(PolicyChange("grid", {"selection_ratio_livelihood": r}), NBLS, workers=workers)
        tables[r] = household_table(res, hh, persons)
        print(f"  {r:.0%} 평가 완료 ({time.perf_counter() - t0:.0f}s)", flush=True)
    base = tables[0.32]
    b = calibrate(base, behavior, actual_recipients()["households"], "S0")
    out = {"ratios": RATIOS, "behaviors": list(BEHAVIORS), "intercept_b": b, "base": {"ratio": 0.32, "behavior": "S0"},
           "age_groups": [name for *_, name in AGE_GROUPS], "national": {}, "province": {}, "age": {}, "liv_share": {}}
    for r, t in tables.items():
        key = f"{r:.2f}"
        e = t[t["nbls_eligible"]]
        prov = e["region_sido"].map(province_of)
        age = e["persona_id"].str[1].astype(int).map(lambda i: AGE_GROUPS[i][2])
        out["national"][key], out["province"][key], out["age"][key] = {}, {}, {}
        for s in BEHAVIORS:
            q = take_up(e, behavior, s, b)
            out["national"][key][s] = metrics(t, behavior, s, b)
            out["province"][key][s] = _group(e, q, prov)
            out["age"][key][s] = _group(e, q, age)
        sh = e.assign(lw=e["weight"] * e["livelihood_eligible"].fillna(False).astype(float)) \
            .groupby("persona_id")[["weight", "lw"]].sum()
        out["liv_share"][key] = {pid: round(float(x.lw / x.weight), 4) for pid, x in sh.iterrows() if x.weight > 0}
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="생계급여 선정기준 × 신청 방식 격자(웹 슬라이더용)")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    out = run(a.workers)
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    n = out["national"]
    print(f"b={out['intercept_b']:.3f} | 32% S0 수급 {n['0.32']['S0']['expected_recipient_households']:,.0f} "
          f"| 35% S1 수급 {n['0.35']['S1']['expected_recipient_households']:,.0f}")
    print(f"→ {OUT} ({time.perf_counter() - t0:.0f}s, {OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
