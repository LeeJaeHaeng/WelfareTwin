"""params 파일 규칙: 모든 값에 출처·verified 가 있고, 미확인 값은 추측 없이 오류를 낸다."""

from decimal import ROUND_HALF_UP, Decimal

import pytest
import yaml
from pydantic import ValidationError

from welfaretwin.rules.params import PARAMS_DIR, Param, load_params


def test_all_param_files_load():
    files = sorted(PARAMS_DIR.glob("*.yaml"))
    assert files
    for f in files:
        ps = load_params(path=f)
        for name, p in ps.params.items():
            assert p.source.doc.strip(), f"{f.name}:{name} 출처 문서명 없음"


def test_2026_median_income_has_sizes_1_to_6():
    ps = load_params(2026)
    assert set(ps.params["median_income"].value) == set(range(1, 7))


def test_unverified_value_is_not_guessed():
    ps = load_params(2026)
    unverified = [k for k in ps.unverified() if ps.params[k].value is None]
    if not unverified:
        pytest.skip("미확인 값 없음")
    with pytest.raises(LookupError):
        ps.require(unverified[0])


@pytest.mark.parametrize("program", ["livelihood", "medical", "housing", "education"])
def test_2026_published_thresholds_match_median_times_ratio(program):
    """보도자료 표의 선정기준액 = 기준중위소득 × 비율, 원 미만 반올림(공표값에서 추론한 규칙)."""
    ps = load_params(2026)
    median = ps.require("median_income")
    ratio = Decimal(str(ps.require(f"selection_ratio_{program}")))
    published = ps.require(f"selection_threshold_published_{program}")
    for size, amount in published.items():
        calc = (Decimal(median[size]) * ratio).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        assert calc == amount, f"{program} {size}인: 계산 {calc} != 공표 {amount}"


def test_verified_requires_value_and_page_or_url():
    base = {"unit": "x", "source": {"doc": "고시"}, "verified": True}
    with pytest.raises(ValidationError):
        Param.model_validate({**base, "value": 1})            # 쪽수·URL 없음
    with pytest.raises(ValidationError):
        Param.model_validate({**base, "value": None, "source": {"doc": "고시", "page": 3}})
    Param.model_validate({**base, "value": 1, "source": {"doc": "고시", "page": 3}})


def test_raw_yaml_entries_have_source_and_verified():
    for f in PARAMS_DIR.glob("*.yaml"):
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        for name, entry in data["params"].items():
            assert "source" in entry and "verified" in entry, f"{f.name}:{name}"
