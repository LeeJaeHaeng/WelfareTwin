"""실시간 계산 API — 정적 웹 사전 계산본·평가기와 같은 결과를 내는지."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from welfaretwin.api.main import app
from welfaretwin.model import Household
from welfaretwin.rules.engine import evaluate, load_ruleset
from welfaretwin.web_export import PRESETS

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)


def test_health_and_programs():
    h = client.get("/health").json()
    assert h["ok"] and h["llm"] is False and h["programs"] >= 19
    ids = {p["id"] for p in client.get("/programs").json()}
    assert {"NBLS_LIVELIHOOD", "EITC", "LOCAL_WLF00005691"} <= ids


def test_evaluate_matches_engine_and_hides_other_regions():
    label, base = PRESETS["youth_worker"]
    hh = Household(**base)
    r = client.post("/evaluate", json=hh.model_dump()).json()
    got = {d["program_id"]: (d["eligible"], d["benefit_monthly"]) for d in r["decisions"]}
    want = {d.program_id: (d.eligible, d.benefit_monthly) for d in evaluate(hh, load_ruleset())}
    assert all(want[k] == v for k, v in got.items())
    rs = load_ruleset()
    for pid in got:   # 지자체 사업은 영천시 사업만
        p = rs.programs[pid]
        assert p.level == "central" or (p.region.sido == "경상북도" and p.region.sigungu in (None, "영천시"))
    assert any(d["reasons"] for d in r["decisions"] if not d["eligible"])
    assert r["disposable"] == r["own_income"] + sum(d["benefit_monthly"] for d in r["decisions"])


def test_sweep_matches_static_web_data():
    sweeps = json.loads((ROOT / "web" / "data" / "sweeps.json").read_text(encoding="utf-8"))
    label, base = PRESETS["youth_disabled"]
    r = client.post("/sweep", json={"household": Household(**base).model_dump()}).json()
    s = sweeps["youth_disabled"]
    assert r["earned"] == s["earned"] and r["own_income"] == s["own_income"]
    assert r["series"] == s["series"]


def test_input_validation():
    assert client.post("/evaluate", json={"region_sido": "서울특별시", "members": []}).status_code == 422
    hh = Household(**PRESETS["youth_disabled"][1]).model_dump()
    assert client.post("/sweep", json={"household": hh, "member": 3}).status_code == 422
    assert client.post("/scenario", json={"selection_ratio_livelihood": 0.9}).status_code == 422


def test_blindspot_live():
    r = client.post("/verify/blindspot", json={"n": 1}).json()
    assert r["status"] == "sat" and len(r["items"]) == 1
    assert r["items"][0]["programs"] and r["items"][0]["facts"]["median_pct"] <= 40


@pytest.mark.skipif(not (ROOT / "data" / "processed" / "households.parquet").exists(), reason="합성 인구 없음")
def test_population_single_worker_matches_pool():
    """Vercel(1 vCPU)용 프로세스 풀 없는 경로가 기존 다중 프로세스 경로와 같은 결과를 낸다."""
    from welfaretwin.sim.scenario import NBLS, PolicyChange, evaluate_population
    change = PolicyChange("생계 35%", {"selection_ratio_livelihood": 0.35})
    one, hh1, _ = evaluate_population(change, NBLS, sample=3200, workers=1)   # 1,500가구 단위 3묶음 — 순서까지 비교
    two, hh2, _ = evaluate_population(change, NBLS, sample=3200, workers=2)
    assert len(one) == len(two) > 0
    assert one.equals(two) and hh1["weight"].equals(hh2["weight"])


@pytest.mark.skipif(not (ROOT / "data" / "processed" / "households.parquet").exists(), reason="합성 인구 없음")
def test_scenario_baseline_preload_matches(tmp_path, monkeypatch):
    """배포 묶음의 미리 계산한 기준선을 써도 시나리오 결과가 같고, 표본이 다른 기준선은 쓰지 않는다."""
    from collections import OrderedDict

    import pandas as pd

    import welfaretwin.api.main as m
    from welfaretwin.sim.scenario import PolicyChange, compare, table_key
    monkeypatch.setattr(m, "SCENARIO_SAMPLE", 1000)
    monkeypatch.setattr(m, "WORKERS", 1)
    body = {"selection_ratio_livelihood": 0.35, "behavior": "S1"}

    def fresh_state(path):
        monkeypatch.setattr(m, "BASELINE_FILE", path)
        monkeypatch.setattr(m, "_scenario_cache", OrderedDict())
        monkeypatch.setattr(m, "_baseline_tried", False)

    fresh_state(tmp_path / "없음.pkl")
    plain = client.post("/scenario", json=body).json()

    cache: dict = {}
    compare([(PolicyChange("현행"), "S0")], sample=1000, workers=1, cache=cache)
    key = table_key(PolicyChange("현행"), 1000)
    good = tmp_path / "base.pkl"
    pd.to_pickle({"key": key, "table": cache[key]}, good)
    fresh_state(good)
    pre = client.post("/scenario", json=body).json()
    assert list(m._scenario_cache)[0] == key
    plain.pop("elapsed_sec"), pre.pop("elapsed_sec")
    assert pre == plain

    bad = tmp_path / "bad.pkl"                       # 다른 가구로 된 기준선
    pd.to_pickle({"key": key, "table": cache[key].iloc[1:]}, bad)
    fresh_state(bad)
    assert m._preload_baseline() is False and key not in m._scenario_cache
