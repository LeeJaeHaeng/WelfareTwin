"""웹 '가상 대한민국' 슬라이더용 정책 격자 — 시나리오 엔진(scenario_demo.json)과 같은 숫자를 내는지."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "artifacts" / "demo" / "policy_grid.json"
SCEN = ROOT / "artifacts" / "demo" / "scenario_demo.json"
WORLD = ROOT / "web" / "data" / "world.json"


@pytest.mark.skipif(not (GRID.exists() and SCEN.exists()), reason="격자·시나리오 결과 없음")
def test_grid_matches_scenario_engine():
    g, sc = json.loads(GRID.read_text(encoding="utf-8")), json.loads(SCEN.read_text(encoding="utf-8"))
    assert g["intercept_b"] == pytest.approx(sc["calibration"]["intercept_b"], rel=1e-9)
    for s in sc["scenarios"]:
        ratio = s["params"].get("selection_ratio_livelihood", 0.32)
        got = g["national"][f"{ratio:.2f}"][s["behavior"]]
        for k, v in s["metrics"].items():
            assert got[k] == pytest.approx(v, rel=1e-9, abs=1e-6), (s["name"], k)


@pytest.mark.skipif(not GRID.exists(), reason="격자 없음")
def test_grid_breakdowns_add_up():
    g = json.loads(GRID.read_text(encoding="utf-8"))
    for key in g["ratios"]:
        k = f"{key:.2f}"
        for s in g["behaviors"]:
            n = g["national"][k][s]
            for part in ("province", "age"):
                rec = sum(v["recipients"] for v in g[part][k][s].values())
                assert rec == pytest.approx(n["expected_recipient_households"], abs=len(g[part][k][s]))
                liv = sum(v["livelihood_recipients"] for v in g[part][k][s].values())
                assert liv == pytest.approx(n["livelihood_expected_recipients"], abs=len(g[part][k][s]))
    # 생계 선정기준을 올리면 생계급여 자격은 줄지 않는다
    liv = [g["national"][f"{r:.2f}"]["S0"]["livelihood_eligible_households"] for r in g["ratios"]]
    assert all(b >= a - 1e-6 for a, b in zip(liv, liv[1:]))


@pytest.mark.skipif(not WORLD.exists(), reason="world.json 없음")
def test_world_expected_matches_scenario():
    w = json.loads(WORLD.read_text(encoding="utf-8"))
    for s in ("S0", "S1"):
        assert w["expected"][s] == pytest.approx(w["scenario_expected"][s], rel=1e-9)
    assert len(w["agents"]) == 714 and all(a["c"] in w["clusters"] for a in w["agents"])
