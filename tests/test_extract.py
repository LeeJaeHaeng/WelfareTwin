"""M4 추출 파이프라인 — API 호출 없이 검증 단계만 시험한다."""

import json

import pytest

from welfaretwin.agents import llm
from welfaretwin.rules.crosscheck import compare
from welfaretwin.rules.dsl import Program
from welfaretwin.rules.engine import RuleSet, load_ruleset
from welfaretwin.rules.extract import ServiceText, check_quotes, to_program, validate
from welfaretwin.rules.testgen import generate

BASE = load_ruleset()
SVC = ServiceText(
    serv_id="TEST0001", name="테스트 청년 월세 지원", sido="대구광역시", sigungu="", dept="",
    target="만 19세 ~ 39세 이하 청년\n기준 중위소득 120% 이하", criteria="주택 소유자 제외",
    benefit="월세 200,000원 지원", cycle="월", provision="현금지급")


def raw(conds, benefit_expr="200000", nm=()):
    return {"region": {"sido": "대구광역시", "sigungu": ""},
            "conditions": [{"label": l, "expr": e, "quote": q, "uncertain": False} for l, e, q in conds],
            "benefit": {"period": "monthly", "expr": benefit_expr, "quote": "월세 200,000원", "uncertain": False},
            "not_modeled": [{"quote": q, "reason": "test"} for q in nm], "notes": ""}


YOUTH = ("청년", "count(p.age >= 19 and p.age <= 39) >= 1", "만 19세 ~ 39세 이하 청년")
INCOME = ("소득", "nbls_income_recognized <= round(median_income(hh.size) * 120 / 100)", "기준 중위소득 120% 이하")
NOT_OWN = ("무주택", "hh.housing != 'own'", "주택 소유자 제외")


def prog(conds, **kw) -> Program:
    p, errs = validate(to_program(raw(conds, **kw), SVC, "A", "test"), BASE)
    assert not errs, errs
    return p


def test_quotes_must_be_verbatim():
    ok = raw([YOUTH, INCOME])
    assert check_quotes(ok, SVC) == []
    bad = raw([("청년", YOUTH[1], "만 19세 이상 34세 이하 청년")])   # 원문에 없는 인용(환각)
    assert len(check_quotes(bad, SVC)) == 1


def test_validate_rejects_bad_dsl():
    p, errs = validate(to_program(raw([("x", "hh.earned_income * hh.size > 1", "청년")]), SVC, "A", "t"), BASE)
    assert p is None and errs
    p, errs = validate(to_program(raw([("x", "unknown_name > 1", "청년")]), SVC, "A", "t"), BASE)
    assert p is None and errs


def test_crosscheck_proves_equivalence_of_reordered_rules():
    a = prog([YOUTH, INCOME, NOT_OWN])
    b = prog([NOT_OWN, INCOME, ("청년", "count(p.age <= 39 and p.age >= 19) > 0", "만 19세 ~ 39세 이하 청년")])
    r = compare(a, b, BASE)
    assert r["eligibility"]["equivalent"] is True and r["agree"]


def test_crosscheck_finds_counterexample_for_missing_condition():
    a = prog([YOUTH, INCOME, NOT_OWN])
    b = prog([YOUTH, INCOME])        # 무주택 조건 누락
    r = compare(a, b, BASE)
    e = r["eligibility"]
    assert e["equivalent"] is False and not e["A_eligible"] and e["B_eligible"]
    assert e["household"]["housing"] == "own"


def test_region_condition_applies():
    p = prog([YOUTH])
    rs = RuleSet({**BASE.programs, p.id: p}, BASE.libraries, BASE.params)
    from welfaretwin.model import Household
    from welfaretwin.rules.engine import evaluate
    hh = dict(members=[{"age": 25}])
    assert {d.program_id: d.eligible for d in evaluate(Household(region_sido="대구광역시", **hh), rs)}[p.id]
    assert not {d.program_id: d.eligible for d in evaluate(Household(region_sido="경상북도", **hh), rs)}[p.id]


def test_testgen_positive_and_one_negative_per_condition():
    p = prog([YOUTH, INCOME, NOT_OWN])
    rs = RuleSet({**BASE.programs, p.id: p}, BASE.libraries, BASE.params)
    cases = generate(p, rs)
    assert [c["expect"]["eligible"] for c in cases] == [True, False, False, False]
    assert all(c["household"]["region_sido"] == "대구광역시" for c in cases)


def test_llm_cache_hit_skips_api(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("WT_LLM_MODEL", "claude-opus-5")
    key = llm._key("claude-opus-5", "v", "s", "u", {"type": "object"})
    (tmp_path / f"{key}.json").write_text(json.dumps({"data": {"x": 1}, "model": "claude-opus-5", "usage": {}}),
                                          encoding="utf-8")

    class NoCall:
        def __getattr__(self, _):
            raise AssertionError("API 를 호출하면 안 됨")

    r = llm.call_json("s", "u", {"type": "object"}, prompt_version="v", client=NoCall())
    assert r.cached and r.data == {"x": 1}
