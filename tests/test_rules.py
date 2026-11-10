"""규칙 정답 테스트 — tests/rules/<제도 ID>.yaml 의 가구 → 기대 자격·급여(문서 09 §1)."""

from pathlib import Path

import pytest
import yaml

from welfaretwin.model import Household
from welfaretwin.rules.engine import derived_value, evaluate, load_ruleset

CASE_DIR = Path(__file__).parent / "rules"


def _cases():
    for f in sorted(CASE_DIR.glob("*.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        for c in spec["cases"]:
            yield pytest.param(spec["program"], c, id=f"{spec['program']}:{c['name']}")


@pytest.fixture(scope="module")
def rs():
    return load_ruleset()


@pytest.mark.parametrize("program,case", list(_cases()))
def test_rule_case(rs, program, case):
    hh = Household.model_validate(case["household"])
    d = {x.program_id: x for x in evaluate(hh, rs)}[program]
    exp = case["expect"]
    why = [f"{t.name}={t.value}" for t in d.trace]
    if "eligible" in exp:
        assert d.eligible == exp["eligible"], why
    if "benefit_monthly" in exp:
        assert d.benefit_monthly == exp["benefit_monthly"], why
    for name, val in (case.get("expect_derived") or {}).items():
        assert derived_value(hh, rs, name, program) == val, name


def test_every_approved_program_has_cases(rs):
    covered = {yaml.safe_load(f.read_text(encoding="utf-8"))["program"] for f in CASE_DIR.glob("*.yaml")}
    assert set(rs.programs) <= covered, set(rs.programs) - covered


def test_case_count_at_least_30():
    n = sum(len(yaml.safe_load(f.read_text(encoding="utf-8"))["cases"]) for f in CASE_DIR.glob("*.yaml"))
    assert n >= 30, n
