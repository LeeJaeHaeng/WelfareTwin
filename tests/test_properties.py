"""속성 기반 테스트(hypothesis) — 문서 04 §5, 문서 09 '속성' 계층.

- 소득·재산이 늘면 소득인정액은 줄지 않는다(단조성)
- 급여는 음수가 아니다, 자격이 없으면 급여는 0
- 생계급여 = 선정기준 − 소득인정액 을 10원 단위로 올림한 값
"""

import math
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from welfaretwin.model import Household, Person
from welfaretwin.rules.engine import _Ctx, derived_value, evaluate, load_ruleset
from welfaretwin.rules.income import income_recognized

RS = load_ruleset()
SIDO = ["서울특별시", "경기도", "인천광역시", "대구광역시", "세종특별자치시", "경상북도", "전라남도"]
WON = st.integers(min_value=0, max_value=3_000_000)

persons = st.builds(
    Person,
    age=st.integers(0, 90),
    disability=st.sampled_from(["none", "mild", "severe"]),
    is_student=st.booleans(),
    earned_income=WON,
    business_income=st.integers(0, 1_000_000),
    other_income=st.integers(0, 1_000_000),
)
households = st.builds(
    Household,
    region_sido=st.sampled_from(SIDO),
    members=st.lists(persons, min_size=1, max_size=6),
    housing=st.sampled_from(["own", "jeonse", "monthly_rent", "other"]),
    rent_monthly=st.integers(0, 1_000_000),
    assets_housing=st.integers(0, 400_000_000),
    assets_general=st.integers(0, 200_000_000),
    assets_financial=st.integers(0, 100_000_000),
    car_value=st.integers(0, 50_000_000),
    debts=st.integers(0, 100_000_000),
    single_parent=st.booleans(),
)


@settings(max_examples=150, deadline=None)
@given(households)
def test_benefits_non_negative_and_zero_when_ineligible(hh):
    for d in evaluate(hh, RS):
        assert d.benefit_monthly >= 0
        if not d.eligible:
            assert d.benefit_monthly == 0, d.program_id


@settings(max_examples=150, deadline=None)
@given(households, st.integers(0, 6), st.integers(1, 2_000_000))
def test_income_recognized_monotone_in_earned_income(hh, idx, delta):
    i = idx % hh.size
    richer = hh.model_copy(deep=True)
    richer.members[i].earned_income += delta
    for name in ("nbls_income_recognized", "nbls_income_recognized_medical"):
        assert derived_value(richer, RS, name) >= derived_value(hh, RS, name), name


@settings(max_examples=150, deadline=None)
@given(households, st.sampled_from(["assets_housing", "assets_general", "assets_financial", "car_value"]),
       st.integers(1, 100_000_000))
def test_income_recognized_monotone_in_assets(hh, field, delta):
    richer = hh.model_copy(update={field: getattr(hh, field) + delta})
    assert derived_value(richer, RS, "nbls_income_recognized") >= derived_value(hh, RS, "nbls_income_recognized")


@settings(max_examples=150, deadline=None)
@given(households)
def test_livelihood_benefit_formula(hh):
    d = {x.program_id: x for x in evaluate(hh, RS)}["NBLS_LIVELIHOOD"]
    if d.eligible:
        ctx = _Ctx(hh, RS)  # 표시용 반올림 없이 정확한 유리수로 비교
        sel = ctx.derived("sel_line", RS.programs["NBLS_LIVELIHOOD"])
        ir = ctx.derived("nbls_income_recognized", None)
        assert isinstance(ir, Fraction)
        assert d.benefit_monthly == math.ceil((sel - ir) / 10) * 10


@settings(max_examples=100, deadline=None)
@given(households)
def test_income_breakdown_adds_up(hh):
    b = income_recognized(hh, RS)
    assert abs(b["nbls_income_recognized"] - (b["nbls_income_eval"] + b["nbls_property_income"])) < 1e-6
