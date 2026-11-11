"""정적 웹(Vercel) 데이터 내보내기 — Streamlit 대시보드의 '사전 계산본' 모드를 서버 없이 보여주기 위함.

    uv run python -m welfaretwin.web_export        # → web/data/*.json

Vercel 은 상시 실행 서버(Streamlit)를 띄울 수 없으므로, 대시보드가 보여주는 결과를 JSON 으로 내보내고
web/index.html 이 이를 읽어 같은 5개 화면을 그린다. 급여 절벽 그래프는 프리셋 가구별 소득 스윕을 미리 계산한다.
실시간 실행(Z3·LLM·사업안 입력)은 정적 웹에서 제외 — 로컬 Streamlit(app/dashboard.py)에서만.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from welfaretwin.model import Household
from welfaretwin.rules.dsl import RULES_DB
from welfaretwin.rules.engine import evaluate, load_ruleset

ROOT = RULES_DB.parent
DEMO = ROOT / "artifacts" / "demo"
OUT = ROOT / "web" / "data"

GROUPS = ["NBLS_LIVELIHOOD", "NBLS_HOUSING", "NBLS_EDUCATION", "BASIC_PENSION", "EITC", "ENERGY_VOUCHER"]
PRESETS = {
    "youth_disabled": ("알바 시작한 장애 청년 1인가구(대구 남구, 월세)", dict(
        region_sido="대구광역시", region_sigungu="남구", housing="monthly_rent", rent_monthly=300_000,
        assets_housing=5_000_000, members=[{"age": 24, "disability": "mild"}])),
    "elderly_couple": ("노인 부부(경북 의성군, 자가)", dict(
        region_sido="경상북도", region_sigungu="의성군", housing="own", assets_housing=80_000_000,
        members=[{"age": 72}, {"age": 70, "sex": "F"}])),
    "single_parent": ("한부모 + 중학생 자녀(서울, 월세)", dict(
        region_sido="서울특별시", housing="monthly_rent", rent_monthly=450_000, assets_housing=10_000_000,
        single_parent=True, members=[{"age": 40, "sex": "F"}, {"age": 14, "is_student": True}])),
    "youth_worker": ("청년 근로자 1인(경북 영천시, 월세)", dict(
        region_sido="경상북도", region_sigungu="영천시", housing="monthly_rent", rent_monthly=350_000,
        assets_housing=5_000_000, members=[{"age": 30}])),
}


def load(name: str):
    p = DEMO / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def sweeps(max_income: int = 3_500_000, step: int = 10_000) -> dict:
    rs = load_ruleset()
    names = {pid: p.name for pid, p in rs.programs.items()}
    out = {}
    for key, (label, base) in PRESETS.items():
        xs, own, series = [], [], {g: [] for g in [*GROUPS, "LOCAL"]}
        local_names: set[str] = set()
        for inc in range(0, max_income + 1, step):
            members = [dict(m) for m in base["members"]]
            members[0]["earned_income"] = inc
            members[0]["employed"] = inc > 0
            hh = Household(**{**base, "members": members})
            vals = {g: 0 for g in series}
            for d in evaluate(hh, rs):
                g = d.program_id if d.program_id in GROUPS else "LOCAL"
                if g == "LOCAL" and d.benefit_monthly:
                    local_names.add(names[d.program_id])
                if g in vals:
                    vals[g] += d.benefit_monthly
            xs.append(inc)
            own.append(sum(m.earned_income + m.business_income + m.other_income for m in hh.members))
            for g in series:
                series[g].append(vals[g])
        out[key] = {"label": label, "household": Household(**base).model_dump(), "earned": xs, "own_income": own,
                    "series": {g: v for g, v in series.items() if any(v)}, "local_programs": sorted(local_names)}
    return out


def agents(n: int = 24) -> dict | None:
    """가상 국민 카드 — Nemotron 가상 개인의 1인칭 응답(behavior v2). 정책 대상에 가까운 소득 1~2분위 유형을
    연령대별로 돌아가며 n개 유형에서 한 명씩(결정적). 신청 확률은 LLM 원값(보정 전)이다."""
    import hashlib

    from welfaretwin.agents.behavior import household_facts
    from welfaretwin.population.personas import AGE_GROUPS, build
    from welfaretwin.population.residents import SOURCE
    sim = ROOT / "artifacts" / "sim"
    if not (sim / "behavior.parquet").exists() or not (sim / "residents.parquet").exists():
        return None
    b = pd.read_parquet(sim / "behavior.parquet")
    if "resident_uuid" not in b:          # v1(클러스터 설명) 응답이면 카드 없음
        return None
    res = pd.read_parquet(sim / "residents.parquet").set_index("uuid")
    hh = pd.read_parquet(ROOT / "data" / "processed" / "households.parquet")
    persons = pd.read_parquet(ROOT / "data" / "processed" / "persons.parquet")
    personas = {p.persona_id: p for p in build(hh, persons)[0]}
    w = b.groupby("persona_id")["weight"].first()
    mean = {sc: float((g.groupby("persona_id")["apply_probability"].mean() * w).sum() / w.sum())
            for sc, g in b.groupby("scenario")}
    s0 = b[b["scenario"] == "S0"].assign(rw=lambda d: d["persona_id"].map(w) / 3)
    rs = s0.assign(r=s0["reasons"].str.split("|")).explode("r")
    rs = rs[rs["r"].fillna("") != ""]
    reasons = (rs.groupby("r")["rw"].sum() / s0["rw"].sum()).sort_values(ascending=False)
    # 소득 1~2분위 유형, 연령대(persona_id 2번째 글자)별 가중치 큰 순 → 연령대를 돌아가며
    low = [p for p in personas.values() if p.income_quintile <= 2]
    by_age = {a: sorted([p for p in low if p.persona_id[1] == str(a)], key=lambda p: -p.weight) for a in range(len(AGE_GROUPS))}
    picks = []
    while len(picks) < n and any(by_age.values()):
        for a in range(len(AGE_GROUPS)):
            if by_age[a] and len(picks) < n:
                picks.append(by_age[a].pop(0))
    cards = []
    for p in picks:
        g = b[b["persona_id"] == p.persona_id]
        slot = int(hashlib.sha1(p.persona_id.encode()).hexdigest(), 16) % 3
        rows = g[g["sample"] == slot]
        uuid = rows["resident_uuid"].iloc[0]
        r = res.loc[uuid]
        cards.append({
            "persona_id": p.persona_id, "households": round(float(p.weight)), "household": household_facts(p),
            "resident": {k: (int(r[k]) if k == "age" else r[k]) for k in
                         ("sex", "age", "marital_status", "family_type", "district", "education_level", "occupation", "persona")},
            "answers": {row["scenario"]: {"p": round(float(row["apply_probability"]), 2), "channel": row["channel"],
                                          "reasons": [x for x in row["reasons"].split("|") if x], "rationale": row["rationale"]}
                        for _, row in rows.iterrows()},
        })
    return {"n_residents": int(b["resident_uuid"].nunique()), "n_clusters": int(b["persona_id"].nunique()),
            "mean_apply_raw": mean, "reasons_s0": {k: round(float(v), 3) for k, v in reasons.head(5).items()},
            "model": b["model"].iloc[0], "source": SOURCE, "cards": cards}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    rs = load_ruleset()
    series = pd.read_csv(DEMO / "nbls_series.csv") if (DEMO / "nbls_series.csv").exists() else None
    static = load("static_2026.json") or {}
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "program_names": {pid: p.name for pid, p in rs.programs.items()},
        "rules": load("m4_rules.json"),
        "blindspot": load("verify_blindspot.json"),
        "blindspot_scale": load("blindspot_scale.json"),
        "cliff_z3": load("verify_cliff.json"),
        "overlap": load("verify_overlap.json"),
        "scenario": load("scenario_demo.json"),
        "redteam": load("redteam_heating.json"),
        "backtest": load("backtest_B2_2024_2025.json"),
        "backtest_fixed": load("backtest_B2_2024_2025_fixedpop.json"),
        "agents": agents(),
        "nbls_series": series.to_dict("records") if series is not None else [],
        "static_calibration": static.get("calibration"),
        "nbls_assumptions": rs.libraries["NBLS_INCOME"].assumptions,
        "population": json.loads((ROOT / "data" / "processed" / "population_meta.json").read_text(encoding="utf-8"))
        if (ROOT / "data" / "processed" / "population_meta.json").exists() else None,
    }
    for name, obj in bundle.items():
        if obj is None:
            print(f"경고: {name} 없음")
    (OUT / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, default=str), encoding="utf-8")
    (OUT / "sweeps.json").write_text(json.dumps(sweeps(), ensure_ascii=False), encoding="utf-8")
    for f in OUT.glob("*.json"):
        print(f"{f.name}: {f.stat().st_size / 1024:,.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
