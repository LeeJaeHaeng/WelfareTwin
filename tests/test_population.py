"""합성 인구 — 주변분포 ±2%p(문서 06 §1.2), 평가기 입력으로 변환 가능.

KOSIS 원문 캐시(data/raw/kosis/)가 없으면 건너뛴다(키·네트워크 필요: python -m welfaretwin.population.synth).
"""

import pytest

from welfaretwin.data.kosis import RAW_DIR
from welfaretwin.rules.engine import evaluate, load_ruleset

pytestmark = pytest.mark.skipif(not any(RAW_DIR.glob("DT_1JC1511__*.json")) if RAW_DIR.exists() else True,
                                reason="KOSIS 원문 캐시 없음")


@pytest.fixture(scope="module")
def population():
    from welfaretwin.population.synth import generate
    return generate(n=3000, seed=7)


def test_marginals_within_2pp(population):
    _, _, meta = population
    for key in ("sido", "size", "tenure", "head_age_band"):
        assert meta["validation"][key]["max_abs_pp"] <= 2.0, key


def test_deterministic_with_seed():
    from welfaretwin.population.synth import generate
    a, _, _ = generate(n=300, seed=11)
    b, _, _ = generate(n=300, seed=11)
    assert a.drop(columns=[]).equals(b)


def test_households_evaluate(population):
    from welfaretwin.sim.static import to_households
    hh, persons, _ = population
    rs = load_ruleset()
    for h in to_households(hh.head(50), persons):
        assert len(evaluate(h, rs)) == len(rs.programs)
        assert 1 <= h.size <= 6 and h.members[0].age >= 18
