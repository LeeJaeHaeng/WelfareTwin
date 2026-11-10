"""평가 단위(가구·가구원)와 판정 결과 스키마 — 문서 03 §3.2, §3.3.

금액은 모두 원 단위 정수, 월 기준(문서 02 §4 단위 통일).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Disability = Literal["none", "mild", "severe"]
Housing = Literal["own", "jeonse", "monthly_rent", "other"]


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid")

    age: int = Field(ge=0, le=120)
    sex: Literal["M", "F"] = "M"
    disability: Disability = "none"
    is_student: bool = False
    employed: bool = False
    pregnant: bool = False
    earned_income: int = Field(0, ge=0)    # 월 근로소득(원)
    business_income: int = Field(0, ge=0)  # 월 사업소득(원)
    other_income: int = Field(0, ge=0)     # 재산·사적이전 등(공적이전 제외)


class Household(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hh_id: str = "hh"
    region_sido: str                      # 예: "서울특별시", "대구광역시", "경상북도"
    region_sigungu: str | None = None     # 예: "창원시" (광역·세종·창원 구분용)
    members: list[Person] = Field(min_length=1)
    housing: Housing = "other"
    rent_monthly: int = Field(0, ge=0)    # 실제 월 임차료(원)
    assets_housing: int = Field(0, ge=0)  # 주거용재산: 자가 주택가액, 전·월세 보증금
    assets_general: int = Field(0, ge=0)  # 주거용 외 일반재산(토지·건축물 등)
    assets_financial: int = Field(0, ge=0)
    # 소득환산율 100% 자동차 가액. 4.17%(일반재산) 적용 차량은 assets_general 에 포함해 입력.
    car_value: int = Field(0, ge=0)
    debts: int = Field(0, ge=0)
    single_parent: bool = False
    # 부양의무자: MVP 는 범주형 근사(문서 03). 생계급여 부양의무자 기준(고소득·고재산) 초과 여부.
    obligor_over_limit: bool = False
    # 의료급여 부양의무자 기준: 부양능력 있는 부양의무자가 있음(판정 근사).
    obligor_medical_capable: bool = False

    @property
    def size(self) -> int:
        return len(self.members)


class TraceItem(BaseModel):
    kind: Literal["derived", "condition", "benefit", "exclusion"]
    name: str
    expr: str
    value: Any
    passed: bool | None = None
    source_ref: dict | None = None


class Decision(BaseModel):
    program_id: str
    program_name: str
    eligible: bool
    benefit_monthly: int
    trace: list[TraceItem]
