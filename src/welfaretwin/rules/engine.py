"""규칙 평가기 — 문서 04 §3. 순수 함수, 결정론적.

    evaluate(hh, ruleset) -> list[Decision]

- 수치는 Fraction(정확한 유리수)으로 계산해 Z3 인코딩과 같은 결과를 낸다(부동소수 오차 없음).
- 1차: 제도별 독립 판정(elig()/benefit() 참조는 지연 평가) → 2차: exclusions(중복 제한) 적용.
- 모든 파생 변수·조건 평가값을 Decision.trace 에 남긴다.
- params 값이 미확인(null)이면 추측하지 않고 LookupError.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from welfaretwin.model import Decision, Household, Person, TraceItem
from welfaretwin.rules.dsl import (
    RULES_DB, AllCond, AnyCond, Cond, Derived, ExprCond, Library, NotCond, Program, load_yaml,
)
from welfaretwin.rules.expr import HH_FEATURES, Expr
from welfaretwin.rules.params import ParamSet, load_params


class RuleError(RuntimeError):
    pass


@dataclass
class RuleSet:
    programs: dict[str, Program]
    libraries: dict[str, Library]
    params: ParamSet
    lib_derived: dict[str, Derived] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for lib in self.libraries.values():
            for name, d in lib.derived.items():
                if name in self.lib_derived:
                    raise RuleError(f"라이브러리 파생 변수 이름 중복: {name}")
                self.lib_derived[name] = d
        self.validate()

    def validate(self) -> None:
        """이름·params·제도 참조를 로드 시점에 검사(값 null 여부는 평가 시점)."""
        known_params = set(self.params.params)
        for lib in self.libraries.values():
            for d in lib.derived.values():
                self._check(d.expr, set(), known_params, f"라이브러리 {lib.library}")
        for p in self.programs.values():
            for e in p.expressions():
                self._check(e, set(p.derived), known_params, p.id)
            for ex in p.exclusions:
                if ex.program not in self.programs:
                    raise RuleError(f"{p.id}: exclusions 의 {ex.program} 없음")

    def _check(self, e: Expr, local: set[str], known_params: set[str], where: str) -> None:
        for n in e.names:
            if n not in local and n not in self.lib_derived:
                raise RuleError(f"{where}: 정의되지 않은 이름 {n!r} ({e.src})")
        for prm in e.params:
            if prm not in known_params:
                raise RuleError(f"{where}: params 에 없는 {prm!r} ({e.src})")
        for pid in e.programs:
            if pid not in self.programs:
                raise RuleError(f"{where}: 없는 제도 {pid!r} ({e.src})")

    def lib_closure(self, program: Program) -> list[str]:
        """제도가 (간접적으로) 쓰는 라이브러리 파생 변수 — trace 표시용, 정의 순서."""
        seen: set[str] = set()
        stack = [n for e in program.expressions() for n in e.names if n not in program.derived]
        stack += [n for d in program.derived.values() for n in d.expr.names if n not in program.derived]
        while stack:
            n = stack.pop()
            if n in seen or n not in self.lib_derived:
                continue
            seen.add(n)
            stack.extend(self.lib_derived[n].expr.names)
        return [n for n in self.lib_derived if n in seen]


def load_ruleset(
    rules_dir: Path | None = None, params: ParamSet | None = None, year: int = 2026
) -> RuleSet:
    rules_dir = rules_dir or RULES_DB / "approved"
    programs, libraries = {}, {}
    for path in sorted(rules_dir.glob("*.yaml")):
        obj = load_yaml(path)
        if isinstance(obj, Library):
            libraries[obj.library] = obj
        else:
            if obj.id in programs:
                raise RuleError(f"제도 ID 중복: {obj.id}")
            programs[obj.id] = obj
    return RuleSet(programs, libraries, params or load_params(year))


# ---------------------------------------------------------------- 가구 특성

def region_group(sido: str, sigungu: str | None) -> str:
    """기본재산액 지역 구분: 서울 / 경기 / 광역·세종·창원 / 그 외 (사업안내 2026 p.5)."""
    if sido.startswith("서울"):
        return "seoul"
    if sido.startswith("경기"):
        return "gyeonggi"
    if sido.endswith("광역시") or sido.startswith("세종") or (sigungu or "").startswith("창원"):
        return "metro"
    return "other"


def housing_zone(sido: str, sigungu: str | None) -> int:
    """주거급여 급지: 1 서울 / 2 경기·인천 / 3 광역시·세종·수도권 외 특례시 / 4 그 외
    (사업안내 2026 p.193, 「주거급여 실시에 관한 고시」 제4조제1항 인용). 수도권 외 특례시 = 창원."""
    if sido.startswith("서울"):
        return 1
    if sido.startswith("경기") or sido.startswith("인천"):
        return 2
    if sido.endswith("광역시") or sido.startswith("세종") or (sigungu or "").startswith("창원"):
        return 3
    return 4


SPECIAL_CITIES = ("수원", "고양", "용인", "창원", "화성")  # 특례시(2026)


def bp_region(sido: str, sigungu: str | None) -> str:
    """기초연금 기본재산액 지역: 대도시(특별시·광역시, 특례시) / 중소도시(도의 시, 세종) / 농어촌(도의 군).
    고시 제2026-156호 제8조. 광역시의 군은 '도ㆍ농복합군 포함' 문구에 따라 대도시로 둔다(근사).
    도 지역에서 시군구를 모르면 중소도시로 둔다(근사)."""
    sgg = sigungu or ""
    if sido.startswith("서울") or sido.endswith("광역시") or sgg.startswith(SPECIAL_CITIES):
        return "metro"
    if sido.startswith("세종") or sgg.endswith("시") or not sgg:
        return "city"
    return "rural"


def household_features(hh: Household) -> dict[str, Any]:
    m = hh.members
    f: dict[str, Any] = {
        "size": len(m),
        "n_elderly": sum(p.age >= 65 for p in m),
        "n_child": sum(p.age < 18 for p in m),
        "n_disabled": sum(p.disability != "none" for p in m),
        "n_severe_disabled": sum(p.disability == "severe" for p in m),
        "n_pregnant": sum(p.pregnant for p in m),
        "earned_income": sum(p.earned_income for p in m),
        "business_income": sum(p.business_income for p in m),
        "other_income": sum(p.other_income for p in m),
        "assets_housing": hh.assets_housing,
        "assets_general": hh.assets_general,
        "assets_financial": hh.assets_financial,
        "car_value": hh.car_value,
        "debts": hh.debts,
        "rent_monthly": hh.rent_monthly,
        "housing": hh.housing,
        "region_sido": hh.region_sido,
        "region_sigungu": hh.region_sigungu or "",
        "region_group": region_group(hh.region_sido, hh.region_sigungu),
        "housing_zone": housing_zone(hh.region_sido, hh.region_sigungu),
        "bp_region": bp_region(hh.region_sido, hh.region_sigungu),
        "single_parent": hh.single_parent,
        "obligor_over_limit": hh.obligor_over_limit,
        "obligor_medical_capable": hh.obligor_medical_capable,
        "head_age": m[0].age,
    }
    assert set(f) == HH_FEATURES, set(f) ^ HH_FEATURES
    return f


# ---------------------------------------------------------------- 평가

def _num(v: Any) -> Any:
    if isinstance(v, bool) or isinstance(v, str) or isinstance(v, Fraction):
        return v
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, float):
        return Fraction(str(v))   # 0.32 → 8/25 (정확히)
    return v


def round_half_up(x: Fraction) -> int:
    return math.floor(x + Fraction(1, 2))


def show(v: Any) -> Any:
    if isinstance(v, Fraction):
        return int(v) if v.denominator == 1 else float(round(v, 4))
    return v


_CMP = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
}


class _Ctx:
    def __init__(self, hh: Household, rs: RuleSet) -> None:
        self.hh, self.rs = hh, rs
        self.features = {k: _num(v) for k, v in household_features(hh).items()}
        self.lib_cache: dict[str, Any] = {}
        self.prog_cache: dict[tuple[str, str], Any] = {}
        self.elig_cache: dict[str, bool] = {}
        self.benefit_cache: dict[str, int] = {}
        self.cond_values: dict[str, list[tuple[ExprCond, bool]]] = {}
        self._in_progress: set[str] = set()

    # --- 이름 해석
    def derived(self, name: str, program: Program | None) -> Any:
        if program is not None and name in program.derived:
            key = (program.id, name)
            if key not in self.prog_cache:
                self.prog_cache[key] = self._guarded(
                    f"{program.id}.{name}", lambda: self.eval(program.derived[name].expr, program))
            return self.prog_cache[key]
        if name not in self.lib_cache:
            d = self.rs.lib_derived[name]
            self.lib_cache[name] = self._guarded(name, lambda: self.eval(d.expr, None))
        return self.lib_cache[name]

    def _guarded(self, key: str, fn):
        if key in self._in_progress:
            raise RuleError(f"순환 참조: {key}")
        self._in_progress.add(key)
        try:
            return fn()
        finally:
            self._in_progress.discard(key)

    def param(self, name: str) -> Any:
        v = self.rs.params.require(name)
        if isinstance(v, dict):
            raise RuleError(f"params.{name} 은 표 — lookup('{name}', key) 로 조회")
        return _num(v)

    def lookup(self, table: str, *keys: Any) -> Any:
        t = self.rs.params.require(table)
        for key in keys:
            if not isinstance(t, dict):
                raise RuleError(f"params.{table} 의 차원이 키 개수보다 적음")
            k = int(key) if isinstance(key, Fraction) and key.denominator == 1 else key
            for cand in (k, str(k)):
                if cand in t:
                    t = t[cand]
                    break
            else:
                raise RuleError(f"params.{table} 에 키 {k!r} 없음")
        if isinstance(t, dict):
            raise RuleError(f"params.{table} 조회 결과가 표 — 키가 부족함")
        return _num(t)

    def median_income(self, n: Any) -> Fraction:
        n = int(n)
        if n < 1:
            raise RuleError("가구원 수는 1 이상")
        table = self.rs.params.require("median_income")
        if n in table:
            return _num(table[n])
        size7 = _num(self.rs.params.require("median_income_size7"))
        if n == 7:
            return size7
        return size7 + (n - 7) * _num(self.rs.params.require("median_income_increment_per_member"))

    # --- 제도 판정
    def eligible(self, pid: str) -> bool:
        if pid not in self.elig_cache:
            prog = self.rs.programs[pid]

            def run():
                results: list[tuple[ExprCond, bool]] = []
                ok = self.cond(prog.eligibility, prog, results)
                if prog.region is not None:
                    in_region = self.hh.region_sido == prog.region.sido
                    results.insert(0, (region_cond(prog), in_region))
                    ok = ok and in_region
                self.cond_values[pid] = results
                return ok
            self.elig_cache[pid] = self._guarded(f"elig:{pid}", run)
        return self.elig_cache[pid]

    def benefit(self, pid: str) -> int:
        if pid not in self.benefit_cache:
            prog = self.rs.programs[pid]
            if not self.eligible(pid):
                self.benefit_cache[pid] = 0
            else:
                b = prog.benefit
                v = self._guarded(f"benefit:{pid}",
                                  lambda: self.eval(b.monthly or b.annual, prog))
                if b.annual is not None:
                    v = v / 12
                self.benefit_cache[pid] = max(0, round_half_up(v))
        return self.benefit_cache[pid]

    def cond(self, c: Cond, prog: Program, out: list) -> bool:
        # 단락 평가 없이 모든 조건을 평가 → trace 에 탈락 사유가 전부 남는다.
        if isinstance(c, ExprCond):
            v = self.eval(c.expr, prog)
            if not isinstance(v, bool):
                raise RuleError(f"{prog.id}: 조건식이 bool 이 아님: {c.expr.src}")
            out.append((c, v))
            return v
        if isinstance(c, AllCond):
            vals = [self.cond(x, prog, out) for x in c.all]
            return all(vals)
        if isinstance(c, AnyCond):
            vals = [self.cond(x, prog, out) for x in c.any]
            return any(vals)
        if isinstance(c, NotCond):
            return not self.cond(c.not_, prog, out)
        raise TypeError(c)

    # --- 식 평가
    def eval(self, e: Expr, prog: Program | None, person: Person | None = None) -> Any:
        return self._ev(e.tree.body, prog, person)

    def _ev(self, n: ast.AST, prog: Program | None, person: Person | None) -> Any:
        ev = lambda x: self._ev(x, prog, person)  # noqa: E731
        if isinstance(n, ast.Constant):
            return _num(n.value)
        if isinstance(n, ast.Name):
            return self.derived(n.id, prog)
        if isinstance(n, ast.Attribute):
            base = n.value.id
            if base == "hh":
                return self.features[n.attr]
            if base == "p":
                return _num(getattr(person, n.attr))
            return self.param(n.attr)
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else (-v if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            op = type(n.op)
            return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b,
                    ast.Mult: lambda: a * b, ast.Div: lambda: a / b}[op]()
        if isinstance(n, ast.BoolOp):
            vals = [ev(x) for x in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right_n in zip(n.ops, n.comparators):
                right = ev(right_n)
                if not _CMP[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.Call):
            return self._call(n, prog, person)
        raise RuleError(f"평가 불가 노드 {type(n).__name__}")

    def _call(self, n: ast.Call, prog: Program | None, person: Person | None) -> Any:
        name, args = n.func.id, n.args
        ev = lambda x: self._ev(x, prog, person)  # noqa: E731
        if name == "min":
            return min(ev(a) for a in args)
        if name == "max":
            return max(ev(a) for a in args)
        if name == "if_":
            return ev(args[1]) if ev(args[0]) else ev(args[2])
        if name == "round":
            return Fraction(round_half_up(ev(args[0])))
        if name == "floor":
            return Fraction(math.floor(ev(args[0])))
        if name == "ceil":
            return Fraction(math.ceil(ev(args[0])))
        if name == "lookup":
            return self.lookup(args[0].value, *(ev(a) for a in args[1:]))
        if name == "median_income":
            return self.median_income(ev(args[0]))
        if name == "sum":
            return sum((self._ev(args[0], prog, p) for p in self.hh.members), Fraction(0))
        if name == "count":
            return Fraction(sum(bool(self._ev(args[0], prog, p)) for p in self.hh.members))
        if name == "elig":
            return self.eligible(args[0].value)
        if name == "benefit":
            return Fraction(self.benefit(args[0].value))
        raise RuleError(f"알 수 없는 함수 {name}")


def region_cond(prog: Program) -> ExprCond:
    """지자체 사업의 지역(시도) 조건 — trace 표시용 가상 조건."""
    from welfaretwin.rules.expr import parse
    label = f"{prog.region.sido}{' ' + prog.region.sigungu if prog.region.sigungu else ''} 거주(시도 단위 판정)"
    return ExprCond(expr=parse(f"hh.region_sido == '{prog.region.sido}'"), label=label)


def evaluate(hh: Household, rs: RuleSet, program_ids: list[str] | None = None) -> list[Decision]:
    ctx = _Ctx(hh, rs)
    ids = program_ids or list(rs.programs)

    # 1차: 독립 판정(참조된 제도도 지연 평가로 함께 계산됨)
    for pid in ids:
        ctx.eligible(pid)
        ctx.benefit(pid)

    # 2차: 중복 제한 — 1차 결과 기준으로 한 번에 적용
    dropped: dict[str, str] = {}
    for pid in ids:
        for ex in rs.programs[pid].exclusions:
            if ctx.eligible(pid) and ctx.eligible(ex.program):
                dropped[pid] = ex.program

    out = []
    for pid in ids:
        prog = rs.programs[pid]
        trace: list[TraceItem] = []
        for name in rs.lib_closure(prog):
            d = rs.lib_derived[name]
            trace.append(TraceItem(kind="derived", name=d.label or name, expr=d.expr.src,
                                   value=show(ctx.derived(name, None))))
        for name, d in prog.derived.items():
            trace.append(TraceItem(kind="derived", name=d.label or name, expr=d.expr.src,
                                   value=show(ctx.derived(name, prog)),
                                   source_ref=_ref(prog, d.ref)))
        for c, v in ctx.cond_values[pid]:
            trace.append(TraceItem(kind="condition", name=c.label or c.expr.src, expr=c.expr.src,
                                   value=v, passed=v, source_ref=_ref(prog, c.ref)))
        eligible, benefit = ctx.eligible(pid), ctx.benefit(pid)
        if pid in dropped:
            trace.append(TraceItem(kind="exclusion", name=f"{dropped[pid]} 와 중복 제한",
                                   expr=f"elig('{dropped[pid]}')", value=True, passed=False))
            eligible, benefit = False, 0
        b = prog.benefit
        trace.append(TraceItem(kind="benefit", name="급여액(월)", expr=(b.monthly or b.annual).src
                               + (" ÷ 12" if b.annual is not None else ""),
                               value=benefit, source_ref=_ref(prog, b.ref)))
        out.append(Decision(program_id=pid, program_name=prog.name, eligible=eligible,
                            benefit_monthly=benefit, trace=trace))
    return out


def _ref(prog: Program, idx: int | None) -> dict | None:
    return prog.source_refs[idx].model_dump(exclude_none=True) if idx is not None else None


def derived_value(hh: Household, rs: RuleSet, name: str, program_id: str | None = None) -> Any:
    """파생 변수 하나의 값(테스트·설명용). program_id 를 주면 그 제도의 derived 도 찾는다."""
    ctx = _Ctx(hh, rs)
    prog = rs.programs[program_id] if program_id else None
    return show(ctx.derived(name, prog))
