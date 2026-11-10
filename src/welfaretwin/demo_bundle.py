"""데모 번들 — 문서 10 §2 '데모 안전장치'.

    uv run python -m welfaretwin.demo_bundle

대시보드가 네트워크·원문 데이터(data/raw, git 제외) 없이 동작하도록 필요한 결과를 artifacts/demo/ 에 모은다.
- m4_rules.json        : 지자체 사업 원문 + 초안 A·B + 교차검증 + 테스트 수(화면 1)
- blindspot_scale.json : 합성 인구에서 사각지대 조건에 해당하는 가구 수 추정(화면 2 카운터)
- static_2026.json     : 정적 시뮬레이션 요약 복사
"""

from __future__ import annotations

import json
import shutil
import sys
from fractions import Fraction
from pathlib import Path

import pandas as pd
import yaml

from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.engine import _Ctx, load_ruleset
from welfaretwin.sim.static import load_population, to_households
from welfaretwin.verify.queries import INCOME_SUPPORT

ROOT = RULES_DB.parent
DEMO = ROOT / "artifacts" / "demo"


def bundle_rules() -> list[dict]:
    from welfaretwin.rules.extract import load_service
    out = []
    for rep_path in sorted((RULES_DB / "review").glob("LOCAL_*.report.json")):
        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        pid = rep_path.name.replace(".report.json", "")
        try:
            svc = load_service(rep["serv_id"])
            text = {"target": svc.target, "criteria": svc.criteria, "benefit": svc.benefit, "region": rep["region"]}
        except FileNotFoundError:
            text = None
        approved = RULES_DB / "approved" / f"{pid}.yaml"
        b_path = RULES_DB / "draft" / f"{pid}__B.yaml"
        tests = ROOT / "tests" / "rules" / f"{pid}.yaml"
        out.append({
            "id": pid, "serv_id": rep["serv_id"], "name": rep["name"], "text": text,
            "rule_approved": yaml.safe_load(approved.read_text(encoding="utf-8")) if approved.exists() else None,
            "rule_B": yaml.safe_load(b_path.read_text(encoding="utf-8")) if b_path.exists() else None,
            "crosscheck": rep.get("crosscheck"), "A": rep["A"], "B": rep["B"],
            "n_tests": len(yaml.safe_load(tests.read_text(encoding="utf-8"))["cases"]) if tests.exists() else 0,
            "verdict": rep["verdict"], "review_reasons": rep["review_reasons"], "llm_cost_usd": rep["llm_cost_usd"],
        })
    return out


def blindspot_scale(poverty_ratio: float = 0.40) -> dict:
    """소득인정액 ≤ 기준중위소득 × 40% 인데 생계급여·기초연금·근로장려금 모두 탈락하는 합성 가구의 가중 합."""
    rs = load_ruleset()
    hh, persons = load_population(ROOT / "data" / "processed")
    total, poor, blind = 0.0, 0.0, 0.0
    by_size: dict[int, float] = {}
    w = hh.set_index("hh_id")["weight"]
    for h in to_households(hh, persons):
        ctx = _Ctx(h, rs)
        total += w[h.hh_id]
        if ctx.derived("nbls_income_recognized", None) <= ctx.median_income(h.size) * Fraction(str(poverty_ratio)):
            poor += w[h.hh_id]
            if not any(ctx.eligible(p) for p in INCOME_SUPPORT):
                blind += w[h.hh_id]
                by_size[h.size] = by_size.get(h.size, 0.0) + w[h.hh_id]
    return {"poverty_ratio": poverty_ratio, "programs": INCOME_SUPPORT, "households_total": total,
            "poor_households": poor, "blindspot_households": blind,
            "blindspot_by_size": {str(k): v for k, v in sorted(by_size.items())},
            "note": "합성 인구(10만 가구, 가중) 기준 추정. 부양의무자·가구특성 지출 미반영 등 단순화 가정 포함."}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    DEMO.mkdir(parents=True, exist_ok=True)
    rules = bundle_rules()
    (DEMO / "m4_rules.json").write_text(json.dumps(rules, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"m4_rules.json: {len(rules)}건(원문 포함 {sum(r['text'] is not None for r in rules)}건)")
    scale = blindspot_scale()
    (DEMO / "blindspot_scale.json").write_text(json.dumps(scale, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"blindspot_scale.json: 빈곤 {scale['poor_households']:,.0f} 중 사각지대 {scale['blindspot_households']:,.0f}가구")
    src = ROOT / "artifacts" / "sim" / "static_2026.json"
    if src.exists():
        shutil.copy(src, DEMO / "static_2026.json")
    stats = ROOT / "data" / "processed" / "stats"
    if stats.exists():
        series = pd.read_csv(stats / "nbls_recipients.csv").merge(pd.read_csv(stats / "nbls_households_by_type.csv")[["year", "합계"]], on="year")
        series.to_csv(DEMO / "nbls_series.csv", index=False)
    print(f"→ {DEMO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
