"""형식검증 질의(Q1~Q3) — 반례가 실제로 조건을 만족하는지, 결함 규칙을 잡아내는지."""

import time

import pytest

from welfaretwin.rules.dsl import Program
from welfaretwin.rules.engine import RuleSet, _Ctx, evaluate, load_ruleset
from welfaretwin.verify import queries as q
from welfaretwin.verify.explain import explain

RS = load_ruleset()


def _with_extra(*programs: dict) -> RuleSet:
    progs = dict(RS.programs)
    for p in programs:
        prog = Program.model_validate(p)
        progs[prog.id] = prog
    return RuleSet(progs, RS.libraries, RS.params)


def test_blindspot_findings_are_real_and_diverse():
    t0 = time.perf_counter()
    r = q.blindspot(RS, n=4)
    assert r.status == "sat" and len(r.findings) == 4
    assert time.perf_counter() - t0 < 10          # 문서 05 §6 성능 목표
    keys = set()
    for f in r.findings:
        hh = f.household
        ctx = _Ctx(hh, RS)
        assert ctx.derived("nbls_income_recognized", None) <= ctx.median_income(hh.size) * 0.4 + 1e-9
        assert not any(d.eligible for d in evaluate(hh, RS, q.INCOME_SUPPORT))
        keys.add((hh.size, any(p.age >= 65 for p in hh.members), any(p.age < 18 for p in hh.members)))
    assert len(keys) == 4                          # 서로 다른 유형
    texts = [x["text"] for x in explain(r, RS)]
    assert all("사각지대 유형" in t and "근거" in t for t in texts)


def test_blindspot_unsat_when_universal_program_exists():
    universal = {
        "id": "UNIVERSAL_BASIC", "name": "보편 기본소득(가상)", "level": "central",
        "source_refs": [{"doc": "테스트"}], "eligibility": {"expr": "hh.size >= 1"},
        "benefit": {"monthly": "100000"}, "status": "draft",
    }
    rs = _with_extra(universal)
    r = q.blindspot(rs, programs=q.INCOME_SUPPORT + ["UNIVERSAL_BASIC"], n=1)
    assert r.status == "unsat" and not r.findings


def test_cliff_basic_pension():
    t0 = time.perf_counter()
    r = q.cliff(RS, n=1, programs=["BASIC_PENSION"], budget_sec=5)
    assert time.perf_counter() - t0 < 15
    assert r.status == "sat"
    f = r.findings[0]
    assert "BASIC_PENSION" in f.facts["lost_programs"]
    assert f.facts["loss"] >= 300_000              # 기준연금액 349,700원을 한꺼번에 잃는다
    assert f.facts["disposable_before"] - f.facts["disposable_after"] == f.facts["loss"]
    assert "급여 절벽" in explain(r, RS)[0]["text"]


def test_overlap_detects_dead_rule_and_declared_exclusion():
    dead = {
        "id": "DEAD_RULE", "name": "모순 규칙(가상)", "level": "local",
        "source_refs": [{"doc": "테스트"}],
        "eligibility": {"all": [{"expr": "hh.n_elderly >= 1"}, {"expr": "hh.head_age < 30"},
                                {"expr": "hh.size == 1"}]},
        "benefit": {"monthly": "50000"}, "status": "draft",
    }
    dup = {
        "id": "LOCAL_DUP", "name": "기초생활수급자 제외 사업(가상)", "level": "local",
        "source_refs": [{"doc": "테스트"}], "eligibility": {"expr": "hh.n_elderly >= 1"},
        "benefit": {"monthly": "30000"}, "exclusions": [{"program": "NBLS_LIVELIHOOD"}], "status": "draft",
    }
    rs = _with_extra(dead, dup)
    r = q.overlap(rs)
    assert r.options["dead_rules"] == ["DEAD_RULE"]
    assert [f.facts["programs"] for f in r.findings] == [["LOCAL_DUP", "NBLS_LIVELIHOOD"]]
    hh = r.findings[0].household
    first = {d.program_id: d.eligible for d in evaluate(hh, rs)}
    assert first["NBLS_LIVELIHOOD"] and not first["LOCAL_DUP"]   # 평가기 2차에서 중복 제한 적용


def test_current_rules_have_no_dead_rule():
    r = q.overlap(RS)
    assert r.options["dead_rules"] == []
