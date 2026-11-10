"""규칙 DSL 스키마(Pydantic) — 문서 04 §2.2.

규칙 파일: rules_db/{approved,review,draft}/<ID>.yaml
공용 라이브러리: rules_db/approved/_lib_*.yaml (여러 제도가 공유하는 파생 변수, 예: 소득인정액)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from welfaretwin.rules.expr import Expr, ExprError, parse

RULES_DB = Path(__file__).resolve().parents[3] / "rules_db"


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str
    page: int | str | None = None
    clause: str | None = None
    url: str | None = None


class ExprCond(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    expr: Expr
    ref: int | None = None      # source_refs 인덱스
    label: str | None = None    # 사람이 읽는 조건 이름

    @field_validator("expr", mode="before")
    @classmethod
    def _parse(cls, v):
        return v if isinstance(v, Expr) else parse(v)


class AllCond(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list["Cond"] = Field(min_length=1)


class AnyCond(BaseModel):
    model_config = ConfigDict(extra="forbid")
    any: list["Cond"] = Field(min_length=1)


class NotCond(BaseModel):
    model_config = ConfigDict(extra="forbid")
    not_: "Cond" = Field(alias="not")


Cond = Union[AllCond, AnyCond, NotCond, ExprCond]
AllCond.model_rebuild()
AnyCond.model_rebuild()
NotCond.model_rebuild()


class Benefit(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    monthly: Expr | None = None
    annual: Expr | None = None   # 연 단위 제도(근로장려금 등) → 월로 환산(÷12)
    ref: int | None = None

    @field_validator("monthly", "annual", mode="before")
    @classmethod
    def _parse(cls, v):
        return v if v is None or isinstance(v, Expr) else parse(v)

    @model_validator(mode="after")
    def _one(self) -> "Benefit":
        if (self.monthly is None) == (self.annual is None):
            raise ValueError("benefit 은 monthly 또는 annual 중 하나만")
        return self


class Exclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    program: str
    effect: Literal["drop_self"] = "drop_self"   # 둘 다 자격이면 이 제도를 탈락 처리
    note: str | None = None


class Derived(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    expr: Expr
    label: str | None = None
    ref: int | None = None


def _parse_derived(v: dict) -> dict[str, Derived]:
    out = {}
    for name, d in (v or {}).items():
        if isinstance(d, Derived):
            out[name] = d
        elif isinstance(d, dict):
            out[name] = Derived(expr=parse(d["expr"]), label=d.get("label"), ref=d.get("ref"))
        else:
            out[name] = Derived(expr=parse(d))
    return out


class Library(BaseModel):
    """여러 제도가 공유하는 파생 변수 묶음(예: 기초생활보장 소득인정액)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    library: str
    name: str
    source_refs: list[SourceRef] = []
    assumptions: list[str] = []
    derived: dict[str, Derived]

    @field_validator("derived", mode="before")
    @classmethod
    def _d(cls, v):
        return _parse_derived(v)


class Program(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    id: str
    name: str
    level: Literal["central", "local"]
    region: str | None = None
    unit: Literal["household", "person"] = "household"
    source_refs: list[SourceRef] = Field(min_length=1)
    assumptions: list[str] = []          # 단순화 가정(화면에 표시)
    derived: dict[str, Derived] = {}
    eligibility: Cond
    benefit: Benefit
    exclusions: list[Exclusion] = []
    status: Literal["draft", "review", "approved"]
    tests: str | None = None

    @field_validator("derived", mode="before")
    @classmethod
    def _d(cls, v):
        return _parse_derived(v)

    @model_validator(mode="after")
    def _refs_in_range(self) -> "Program":
        n = len(self.source_refs)
        for c in iter_conditions(self.eligibility):
            if c.ref is not None and not 0 <= c.ref < n:
                raise ValueError(f"{self.id}: 조건 ref {c.ref} 가 source_refs 범위 밖")
        return self

    def expressions(self):
        for d in self.derived.values():
            yield d.expr
        for c in iter_conditions(self.eligibility):
            yield c.expr
        yield self.benefit.monthly or self.benefit.annual


def iter_conditions(c: Cond):
    if isinstance(c, ExprCond):
        yield c
    elif isinstance(c, AllCond):
        for x in c.all:
            yield from iter_conditions(x)
    elif isinstance(c, AnyCond):
        for x in c.any:
            yield from iter_conditions(x)
    elif isinstance(c, NotCond):
        yield from iter_conditions(c.not_)


def load_yaml(path: Path) -> Program | Library:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return Library.model_validate(data) if "library" in data else Program.model_validate(data)
    except ExprError as e:
        raise ValueError(f"{path.name}: {e}") from e
