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
