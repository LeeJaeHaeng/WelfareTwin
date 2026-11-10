"""제도 기준값(rules_db/params/{year}.yaml) 로더.

미확인 값(value: null)은 그대로 None 으로 둔다. 엔진은 None 을 만나면
추측값으로 대체하지 말고 오류를 내야 한다(`require`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

PARAMS_DIR = Path(__file__).resolve().parents[3] / "rules_db" / "params"


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc: str
    page: int | str | None = None
    url: str | None = None


class Param(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None
    unit: str
    source: SourceRef
    verified: bool
    note: str | None = None

    @model_validator(mode="after")
    def _verified_needs_evidence(self) -> "Param":
        if self.verified:
            if _has_null(self.value):
                raise ValueError("verified: true 인데 value 에 null 이 있음")
            if self.source.page is None and not self.source.url:
                raise ValueError("verified: true 이면 source.page 또는 source.url 필요")
        return self


def _has_null(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, dict):
        return any(_has_null(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_null(x) for x in v)
    return False


class ParamSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meta: dict[str, Any]
    params: dict[str, Param]

    def require(self, name: str) -> Any:
        """값을 꺼낸다. 미확인(null)이면 추측하지 않고 오류."""
        p = self.params[name]
        if _has_null(p.value):
            raise LookupError(f"params.{name} 값이 아직 확인되지 않았습니다(null). 출처 확인 후 입력.")
        return p.value

    def unverified(self) -> list[str]:
        return [k for k, p in self.params.items() if not p.verified]


def load_params(year: int = 2026, path: Path | None = None) -> ParamSet:
    path = path or PARAMS_DIR / f"{year}.yaml"
    with path.open(encoding="utf-8") as f:
        return ParamSet.model_validate(yaml.safe_load(f))
