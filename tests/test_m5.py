"""M5 — 시나리오 보정·레드팀 구성 요소(API 호출 없음)."""

import numpy as np
import pandas as pd
import pytest

from welfaretwin.model import Household
from welfaretwin.redteam.agents import RedTeamResult, _manipulations, admin_burden
from welfaretwin.redteam.report import render
from welfaretwin.rules.dsl import Program
from welfaretwin.sim.scenario import PolicyChange, build_ruleset, calibrate, metrics, take_up


@pytest.fixture
def table():
    return pd.DataFrame({
        "nbls_eligible": [True, True, True, False],
        "livelihood_eligible": [True, False, False, False],
        "weight": [100.0, 200.0, 300.0, 400.0],
        "size": [1, 2, 3, 1],
        "nbls_cash": [500_000, 300_000, 100_000, 0],
        "persona_id": ["A", "B", "C", "A"],
    }, index=["h1", "h2", "h3", "h4"])


@pytest.fixture
def behavior():
    return pd.DataFrame({"S0": [0.2, 0.5, 0.8], "S1": [0.4, 0.7, 0.9]}, index=["A", "B", "C"])


def test_calibration_hits_target(table, behavior):
    b = calibrate(table, behavior, target_households=400.0)
    m = metrics(table, behavior, "S0", b)
    assert m["expected_recipient_households"] == pytest.approx(400.0, rel=1e-6)
    assert m["eligible_households"] == 600.0 and m["non_take_up_households"] == pytest.approx(200.0)
    assert m["livelihood_eligible_households"] == 100.0


def test_same_intercept_keeps_scenario_ordering(table, behavior):
    b = calibrate(table, behavior, 300.0)
    assert metrics(table, behavior, "S1", b)["expected_recipient_households"] > \
        metrics(table, behavior, "S0", b)["expected_recipient_households"]
    assert (take_up(table, behavior, "S0", b + 1) > take_up(table, behavior, "S0", b)).all()


def test_policy_change_overrides_params():
    rs = build_ruleset(PolicyChange("t", {"selection_ratio_livelihood": 0.35}))
    assert rs.params.require("selection_ratio_livelihood") == 0.35
    with pytest.raises(KeyError):
        build_ruleset(PolicyChange("t", {"no_such_param": 1}))


def test_manipulations():
    h = Household(region_sido="대구광역시", housing="monthly_rent", rent_monthly=300_000, assets_financial=10_000_000,
                  members=[{"age": 45, "earned_income": 2_000_000}, {"age": 70}])
    kinds = [k for k, _ in _manipulations(h)]
    assert kinds == ["세대 분리", "소득 신고 시점 조정", "재산 형태 전환"]
    split = dict(_manipulations(h))["세대 분리"]
    assert split.size == 1 and split.members[0].age == 70 and split.assets_financial == 0


def test_admin_burden_and_render():
    prog = Program.model_validate({
        "id": "PROPOSAL_X", "name": "테스트 난방비", "level": "local", "region": {"sido": "대구광역시"},
        "source_refs": [{"doc": "t"}],
        "eligibility": {"all": [{"expr": "count(p.age >= 65) >= 1", "label": "65세 이상"},
                                {"expr": "nbls_income_recognized <= 1000000", "label": "소득"}]},
        "benefit": {"monthly": "50000"}, "not_modeled": [{"quote": "실거주", "reason": "x"}], "status": "draft"})
    ad = admin_burden(prog, {"ENERGY_VOUCHER": 0.8, "EITC": 0.1})
    assert ad["n_conditions"] == 2 and "소득 조사(사회보장정보시스템 연계)" in ad["checks"]
    assert ad["overlap_review"] == ["ENERGY_VOUCHER"] and ad["manual_checks"] == ["실거주"]
    r = RedTeamResult(
        proposal="테스트", program={"name": "테스트 난방비", "region": {"sido": "대구광역시"},
                                  "eligibility": {"all": [{"label": "65세 이상", "expr": "count(p.age >= 65) >= 1"}]},
                                  "benefit": {"monthly": "50000"}},
        extraction={"not_modeled": []},
        impact={"eligible_households": 1000.0, "monthly_budget_won_full_take_up": 5e7,
                "overlap_rates": {"ENERGY_VOUCHER": 0.8}, "sample_households": 100, "scope": "테스트"},
        admin=ad, timings={"total": 1.0, "extract": 0.5})
    md = render(r)
    for section in ("1. 영향", "2. 배제 위험", "3. 허점 위험", "4. 급여 절벽", "5. 행정 부담"):
        assert section in md
