"""평가기 ↔ Z3 인코더 교차 일관성 — 문서 05 §3, 문서 09 '엔진–Z3 일관성'.

무작위 가구에 대해 engine.evaluate 결과와, Z3 식에 같은 가구를 대입한 값이 일치해야 한다.
기본 200가구, WT_CONSISTENCY_N 환경변수로 1,000가구 실행.
"""

import os
import random
from fractions import Fraction

import pytest

from welfaretwin.model import Household, Person
from welfaretwin.rules.engine import _Ctx, evaluate, load_ruleset
from welfaretwin.verify.encoder import REGION_PROFILES, Encoder, SymHousehold, concrete

N = int(os.environ.get("WT_CONSISTENCY_N", "200"))
RS = load_ruleset()
SHH = SymHousehold()
ENC = Encoder(RS, SHH)


def random_household(rng: random.Random, i: int) -> Household:
    def money(hi, p_zero=0.4):
        return 0 if rng.random() < p_zero else rng.randint(0, hi)

    size = rng.randint(1, 6)
    members = []
    for k in range(size):
        age = rng.randint(19, 90) if k == 0 else rng.randint(0, 90)
        members.append(Person(
            age=age, sex=rng.choice("MF"), disability=rng.choice(["none", "none", "mild", "severe"]),
            is_student=6 <= age <= 25 and rng.random() < 0.6,
            pregnant=False,
            earned_income=money(3_500_000) if age >= 15 else 0,
            business_income=money(1_000_000, 0.8) if age >= 19 else 0,
            other_income=money(1_500_000, 0.7)))
    _, sido, sgg = rng.choice(REGION_PROFILES)
    housing = rng.choice(["own", "jeonse", "monthly_rent", "other"])
    return Household(
        hh_id=f"r{i}", region_sido=sido, region_sigungu=sgg, members=members, housing=housing,
        rent_monthly=money(800_000) if housing == "monthly_rent" else 0,
        assets_housing=money(400_000_000), assets_general=money(150_000_000, 0.6),
        assets_financial=money(60_000_000), car_value=money(50_000_000, 0.7),
        debts=money(80_000_000, 0.6), single_parent=rng.random() < 0.1,
        obligor_over_limit=rng.random() < 0.05, obligor_medical_capable=rng.random() < 0.1)


@pytest.fixture(scope="module")
def households():
    rng = random.Random(20261110)
    return [random_household(rng, i) for i in range(N)]


def test_engine_and_z3_agree(households):
    mismatches = []
    for hh in households:
        for d in evaluate(hh, RS):
            z_elig = concrete(ENC.eligible(d.program_id), SHH, hh)
            z_ben = concrete(ENC.benefit(d.program_id), SHH, hh)
            if z_elig != d.eligible or z_ben != d.benefit_monthly:
                mismatches.append((hh.hh_id, d.program_id, d.eligible, z_elig, d.benefit_monthly, z_ben))
    assert not mismatches, mismatches[:5]


def test_income_recognized_agrees(households):
    for hh in households[:50]:
        ctx = _Ctx(hh, RS)
        for name in ("nbls_income_recognized", "nbls_income_recognized_medical"):
            assert concrete(ENC.derived(name), SHH, hh) == Fraction(ctx.derived(name, None)), (hh.hh_id, name)


def test_decode_roundtrip(households):
    import z3
    hh = households[0]
    s = z3.Solver()
    s.add([v == x for v, x in SHH.bind(hh)])
    assert s.check() == z3.sat
    back = SHH.decode(s.model(), hh_id=hh.hh_id)
    assert back.model_dump() == hh.model_dump()
