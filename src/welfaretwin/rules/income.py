"""기초생활보장 소득인정액 모듈.

산식 자체는 rules_db/approved/_lib_nbls_income.yaml 에 DSL 로 정의돼 있다(평가기와 Z3 인코더가
같은 정의를 쓰도록). 이 모듈은 그 결과를 항목별로 꺼내 보여주는 API 와, 화면에 띄울
'단순화 가정' 목록을 제공한다.
"""

from __future__ import annotations

from welfaretwin.model import Household
from welfaretwin.rules.engine import RuleSet, derived_value

LIBRARY_ID = "NBLS_INCOME"

BREAKDOWN = [
    "nbls_work_income_net",
    "nbls_income_eval",
    "nbls_housing_after",
    "nbls_general_after",
    "nbls_financial_after",
    "nbls_property_income",
    "nbls_income_recognized",
    "nbls_income_recognized_medical",
]


def income_recognized(hh: Household, rs: RuleSet) -> dict[str, int | float]:
    """소득인정액과 구성 항목(원/월)."""
    return {name: derived_value(hh, rs, name) for name in BREAKDOWN}


def assumptions(rs: RuleSet) -> list[str]:
    return list(rs.libraries[LIBRARY_ID].assumptions)
