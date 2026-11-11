"""가상 국민(Nemotron 인물) 매칭 — 클러스터 조건을 지키는지, 결정적인지, 한 사람이 두 번 쓰이지 않는지."""

from pathlib import Path

import pandas as pd
import pytest

from welfaretwin.population.personas import AGE_GROUPS, build
from welfaretwin.population.residents import FAMILY, HIGH_STATUS, NEMOTRON, OUT, PER_PERSONA, match, province_of

ROOT = Path(__file__).resolve().parents[1]
POP = ROOT / "data" / "processed" / "households.parquet"


@pytest.fixture(scope="module")
def clusters():
    if not POP.exists():
        pytest.skip("합성 인구 없음")
    hh = pd.read_parquet(POP)
    persons = pd.read_parquet(ROOT / "data" / "processed" / "persons.parquet")
    personas, pid = build(hh, persons)
    return hh, personas, pid


@pytest.mark.skipif(not OUT.exists(), reason="residents.parquet 없음")
def test_saved_residents_respect_cluster_conditions(clusters):
    _, personas, _ = clusters
    r = pd.read_parquet(OUT)
    by = {p.persona_id: p for p in personas}
    assert set(r["persona_id"]) == set(by)
    assert (r.groupby("persona_id").size() == PER_PERSONA).all()
    assert not r["uuid"].duplicated().any()
    for pid, g in r.groupby("persona_id"):
        p = by[pid]
        lo, hi = next((a, b) for a, b, n in AGE_GROUPS if n == p.age_group)
        size = {"1인": 1, "2인": 2, "3인": 3, "4인 이상": 4}[p.size_group]
        strict = g[g["match_relaxed"] == 0]
        assert g["age"].between(max(lo, 19), hi).all(), pid
        assert g["family_type"].isin(FAMILY[size]).all(), pid
        if len(strict) == len(g):    # 조건을 하나도 풀지 않았으면 취업자 수·소득·지역까지 맞아야 함
            assert ((g["occupation"] != "무직").sum()) == round(PER_PERSONA * p.profile["employed_share"]), pid
            assert (g["province"] == g["target_province"]).all(), pid
            if p.income_quintile <= 2:
                assert not g["occupation"].str.contains(HIGH_STATUS).any(), pid


@pytest.mark.skipif(not NEMOTRON.exists(), reason="Nemotron 원본 없음(data/raw/nemotron)")
def test_matching_is_deterministic(clusters):
    from welfaretwin.population.residents import load_pool
    hh, personas, pid = clusters
    pool = load_pool()
    few = sorted(personas, key=lambda p: -p.weight)[:8]
    a = match(few, pid, hh, pool)
    b = match(few, pid, hh, pool)
    assert a["uuid"].tolist() == b["uuid"].tolist()


def test_province_names_cover_all_sido():
    for sido in ["서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시", "세종특별자치시",
                 "경기도", "강원특별자치도", "충청북도", "충청남도", "전북특별자치도", "전라남도", "경상북도", "경상남도", "제주특별자치도"]:
        assert province_of(sido)
