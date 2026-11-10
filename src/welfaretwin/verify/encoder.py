"""DSL → Z3 인코더 — 문서 05 §2~3.

평가기(rules/engine.py)와 같은 AST·같은 params 를 Z3 식으로 옮긴다. 금액은 원 단위,
수식은 실수(Real) 산술 + round/floor/ceil 은 ToInt 로 표현한다(선형 산술 유지).

기호 가구(SymHousehold)
- 가구원 슬롯 MAX_MEMBERS 개, i < size 인 슬롯만 활성(sum/count 는 활성 슬롯만 합산)
- 지역은 REGION_PROFILES 중 하나(지역 구분 함수는 engine 의 것을 그대로 재사용)
- 문자열 값(주거 형태, 지역 구분, 가구유형 등)은 '조건 → 값' 목록(SymEnum)으로 다룬다
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import z3

from welfaretwin.model import Household, Person
from welfaretwin.rules.dsl import AllCond, AnyCond, Cond, ExprCond, NotCond, Program
from welfaretwin.rules.engine import RuleSet, bp_region, housing_zone, region_group
from welfaretwin.rules.expr import Expr

MAX_MEMBERS = 6
MAX_INCOME = 20_000_000          # 가구원 월 소득 상한(원) — 문서 05 §2 (20,000천원)
MAX_ASSET = 2_000_000_000        # 재산 상한(원) — 문서 05 §2 (2,000,000천원)
MAX_RENT = 3_000_000

HOUSING = ["own", "jeonse", "monthly_rent", "other"]
DISABILITY = ["none", "mild", "severe"]
SEX = ["M", "F"]

# (표시 이름, 시도, 시군구) — 지역 구분 3종(기본재산액·주거급여 급지·기초연금)의 조합을 대표
REGION_PROFILES: list[tuple[str, str, str | None]] = [
    ("서울", "서울특별시", None),
    ("경기 일반시", "경기도", "남양주시"),
    ("경기 특례시", "경기도", "수원시"),
    ("경기 군", "경기도", "가평군"),
    ("인천", "인천광역시", None),
    ("대구", "대구광역시", None),
    ("부산", "부산광역시", None),
    ("세종", "세종특별자치시", None),
    ("창원", "경상남도", "창원시"),
    ("경북 시", "경상북도", "포항시"),
    ("경북 군", "경상북도", "의성군"),
    ("전남 시", "전라남도", "목포시"),
    ("전남 군", "전라남도", "해남군"),
]


class EncodeError(RuntimeError):
    pass


def R(v: Any) -> z3.ArithRef:
    """숫자 → Z3 실수."""
    if isinstance(v, z3.ArithRef):
        return z3.ToReal(v) if v.is_int() else v
    if isinstance(v, bool):
        raise EncodeError("bool 을 숫자로 쓸 수 없음")
    if isinstance(v, (int, Fraction)):
        f = Fraction(v)
        return z3.RealVal(f"{f.numerator}/{f.denominator}")
    if isinstance(v, float):
        return R(Fraction(str(v)))
    if isinstance(v, SymEnum):
        return v.as_num()
    raise EncodeError(f"숫자가 아님: {v!r}")


def B(v: Any) -> z3.BoolRef:
    if isinstance(v, z3.BoolRef):
        return v
    if isinstance(v, bool):
        return z3.BoolVal(v)
    raise EncodeError(f"bool 이 아님: {v!r}")


@dataclass
class SymEnum:
    """서로 배타적인 (조건, 값) 목록. 값은 str 또는 int."""

    cases: list[tuple[z3.BoolRef, Any]]

    @staticmethod
    def const(v: Any) -> "SymEnum":
        return SymEnum([(z3.BoolVal(True), v)])

    def eq(self, c: Any) -> z3.BoolRef:
        if isinstance(c, SymEnum):
            return z3.Or([z3.And(a, b) for a, x in self.cases for b, y in c.cases if x == y] or [False])
        return z3.Or([cond for cond, v in self.cases if v == c] or [False])

    def as_num(self) -> z3.ArithRef:
        out = R(self.cases[-1][1])
        for cond, v in reversed(self.cases[:-1]):
            out = z3.If(cond, R(v), out)
        return out


def ite(c: z3.BoolRef, a: Any, b: Any) -> Any:
    if isinstance(a, (str, SymEnum)) or isinstance(b, (str, SymEnum)):
        ea = a if isinstance(a, SymEnum) else SymEnum.const(a)
        eb = b if isinstance(b, SymEnum) else SymEnum.const(b)
        return SymEnum([(z3.And(c, x), v) for x, v in ea.cases] + [(z3.And(z3.Not(c), x), v) for x, v in eb.cases])
    if isinstance(a, (bool, z3.BoolRef)) and isinstance(b, (bool, z3.BoolRef)):
        return z3.If(c, B(a), B(b))
    return z3.If(c, R(a), R(b))


def zmin(xs: list) -> z3.ArithRef:
    out = R(xs[0])
    for x in xs[1:]:
        x = R(x)
        out = z3.If(x < out, x, out)
    return out


def zmax(xs: list) -> z3.ArithRef:
    out = R(xs[0])
    for x in xs[1:]:
        x = R(x)
        out = z3.If(x > out, x, out)
    return out


def zfloor(x) -> z3.ArithRef:
    return z3.ToReal(z3.ToInt(R(x)))


class SymPerson:
    def __init__(self, prefix: str) -> None:
        self.age = z3.Int(f"{prefix}_age")
        self.sex = z3.Int(f"{prefix}_sex")
        self.disability = z3.Int(f"{prefix}_disability")
        self.is_student = z3.Bool(f"{prefix}_is_student")
        self.employed = z3.Bool(f"{prefix}_employed")
        self.pregnant = z3.Bool(f"{prefix}_pregnant")
        self.earned_income = z3.Int(f"{prefix}_earned_income")
        self.business_income = z3.Int(f"{prefix}_business_income")
        self.other_income = z3.Int(f"{prefix}_other_income")

    def attr(self, name: str) -> Any:
        if name == "sex":
            return SymEnum([(self.sex == i, v) for i, v in enumerate(SEX)])
        if name == "disability":
            return SymEnum([(self.disability == i, v) for i, v in enumerate(DISABILITY)])
        return getattr(self, name)

    def domain(self) -> list[z3.BoolRef]:
        return [
            self.age >= 0, self.age <= 100, self.sex >= 0, self.sex <= 1,
            self.disability >= 0, self.disability <= 2,
            *[0 <= v for v in (self.earned_income, self.business_income, self.other_income)],
            *[v <= MAX_INCOME for v in (self.earned_income, self.business_income, self.other_income)],
        ]

    def realism(self) -> list[z3.BoolRef]:
        """반례를 사람이 납득할 수 있게 하는 최소 제약(법 규정이 아님)."""
        return [
            z3.Implies(self.pregnant, z3.And(self.sex == 1, self.age >= 15, self.age <= 49)),
            z3.Implies(self.is_student, z3.And(self.age >= 6, self.age <= 30)),
            z3.Implies(self.earned_income > 0, self.age >= 15),
            z3.Implies(self.business_income > 0, self.age >= 19),
            self.employed == (self.earned_income > 0),
            self.other_income <= 3_000_000,
        ]

    def plain(self) -> list[z3.BoolRef]:
        """'평범한' 가구원 선호(소프트 제약용)."""
        return [self.disability == 0, z3.Not(self.pregnant), self.business_income == 0, self.other_income == 0]


class SymHousehold:
    def __init__(self, prefix: str = "hh") -> None:
        self.prefix = prefix
        self.size = z3.Int(f"{prefix}_size")
        self.region = z3.Int(f"{prefix}_region")
        self.housing = z3.Int(f"{prefix}_housing")
        self.rent_monthly = z3.Int(f"{prefix}_rent_monthly")
        self.assets_housing = z3.Int(f"{prefix}_assets_housing")
        self.assets_general = z3.Int(f"{prefix}_assets_general")
        self.assets_financial = z3.Int(f"{prefix}_assets_financial")
        self.car_value = z3.Int(f"{prefix}_car_value")
        self.debts = z3.Int(f"{prefix}_debts")
        self.single_parent = z3.Bool(f"{prefix}_single_parent")
        self.obligor_over_limit = z3.Bool(f"{prefix}_obligor_over_limit")
        self.obligor_medical_capable = z3.Bool(f"{prefix}_obligor_medical_capable")
        self.members = [SymPerson(f"{prefix}_m{i}") for i in range(MAX_MEMBERS)]

    def active(self, i: int) -> z3.BoolRef:
        return z3.IntVal(i) < self.size

    def domain(self) -> list[z3.BoolRef]:
        c = [self.size >= 1, self.size <= MAX_MEMBERS,
             self.region >= 0, self.region < len(REGION_PROFILES),
             self.housing >= 0, self.housing < len(HOUSING),
             self.rent_monthly >= 0, self.rent_monthly <= MAX_RENT]
        for v in (self.assets_housing, self.assets_general, self.assets_financial, self.car_value, self.debts):
            c += [v >= 0, v <= MAX_ASSET]
        for m in self.members:
            c += m.domain()
        return c

    def realism(self) -> list[z3.BoolRef]:
        c = [z3.Implies(self.housing != HOUSING.index("monthly_rent"), self.rent_monthly == 0),
             z3.Implies(self.housing == HOUSING.index("other"), self.assets_housing == 0),
             self.rent_monthly <= 1_500_000,
             self.assets_financial <= 300_000_000,
             self.debts <= self.assets_housing + self.assets_general + self.assets_financial,
             self.members[0].age >= 19,                       # 가구주는 성인
             z3.Implies(self.single_parent, self.feature("n_child") >= 1)]
        for i, m in enumerate(self.members):
            c += [z3.Implies(self.active(i), z) for z in m.realism()]
        return c

    def plain(self) -> list[tuple[z3.BoolRef, int]]:
        """'평범한' 가구 선호 — (제약, 가중치). 탈락 원인을 꾸밈 없이 드러내기 위한 소프트 제약."""
        c = [(z3.Not(self.obligor_over_limit), 5), (z3.Not(self.obligor_medical_capable), 5),
             (z3.Not(self.single_parent), 2), (self.debts == 0, 3), (self.car_value == 0, 3),
             (self.assets_financial <= 5_000_000, 2), (self.assets_general == 0, 2),
             (self.assets_housing <= 300_000_000, 2), (self.feature("n_elderly") <= 2, 2)]
        for i, m in enumerate(self.members):
            c += [(z3.Implies(self.active(i), x), 1) for x in m.plain()]
        return c

    def _region_enum(self, fn) -> SymEnum:
        return SymEnum([(self.region == i, fn(sido, sgg)) for i, (_, sido, sgg) in enumerate(REGION_PROFILES)])

    def feature(self, name: str) -> Any:
        ms = list(enumerate(self.members))
        cnt = lambda pred: z3.Sum([z3.If(z3.And(self.active(i), pred(m)), 1, 0) for i, m in ms])  # noqa: E731
        tot = lambda f: z3.Sum([z3.If(self.active(i), getattr(m, f), 0) for i, m in ms])  # noqa: E731
        match name:
            case "size": return self.size
            case "n_elderly": return cnt(lambda m: m.age >= 65)
            case "n_child": return cnt(lambda m: m.age < 18)
            case "n_disabled": return cnt(lambda m: m.disability != 0)
            case "n_severe_disabled": return cnt(lambda m: m.disability == 2)
            case "n_pregnant": return cnt(lambda m: m.pregnant)
            case "earned_income" | "business_income" | "other_income": return tot(name)
            case "housing": return SymEnum([(self.housing == i, v) for i, v in enumerate(HOUSING)])
            case "region_sido": return self._region_enum(lambda s, g: s)
            case "region_sigungu": return self._region_enum(lambda s, g: g or "")
            case "region_group": return self._region_enum(region_group)
            case "housing_zone": return self._region_enum(housing_zone)
            case "bp_region": return self._region_enum(bp_region)
            case "head_age": return self.members[0].age
            case _: return getattr(self, name)

    # --- 구체 가구와의 변환
    def bind(self, hh: Household) -> list[tuple[z3.ExprRef, z3.ExprRef]]:
        """구체 가구 값으로 치환할 (변수, 값) 목록."""
        idx = region_index(hh)
        pairs = [(self.size, z3.IntVal(hh.size)), (self.region, z3.IntVal(idx)),
                 (self.housing, z3.IntVal(HOUSING.index(hh.housing)))]
        for f in ("rent_monthly", "assets_housing", "assets_general", "assets_financial", "car_value", "debts"):
            pairs.append((getattr(self, f), z3.IntVal(getattr(hh, f))))
        for f in ("single_parent", "obligor_over_limit", "obligor_medical_capable"):
            pairs.append((getattr(self, f), z3.BoolVal(getattr(hh, f))))
        for i, m in enumerate(self.members):
            p = hh.members[i] if i < hh.size else Person(age=0)
            pairs += [(m.age, z3.IntVal(p.age)), (m.sex, z3.IntVal(SEX.index(p.sex))),
                      (m.disability, z3.IntVal(DISABILITY.index(p.disability)))]
            for f in ("is_student", "employed", "pregnant"):
                pairs.append((getattr(m, f), z3.BoolVal(getattr(p, f))))
            for f in ("earned_income", "business_income", "other_income"):
                pairs.append((getattr(m, f), z3.IntVal(getattr(p, f))))
        return pairs

    def decode(self, model: z3.ModelRef, hh_id: str = "counterexample") -> Household:
        iv = lambda v: model.eval(v, model_completion=True).as_long()  # noqa: E731
        bv = lambda v: z3.is_true(model.eval(v, model_completion=True))  # noqa: E731
        _, sido, sgg = REGION_PROFILES[iv(self.region)]
        members = []
        for m in self.members[: iv(self.size)]:
            members.append(Person(
                age=iv(m.age), sex=SEX[iv(m.sex)], disability=DISABILITY[iv(m.disability)],
                is_student=bv(m.is_student), employed=bv(m.employed), pregnant=bv(m.pregnant),
                earned_income=iv(m.earned_income), business_income=iv(m.business_income),
                other_income=iv(m.other_income)))
        return Household(
            hh_id=hh_id, region_sido=sido, region_sigungu=sgg, members=members,
            housing=HOUSING[iv(self.housing)], rent_monthly=iv(self.rent_monthly),
            assets_housing=iv(self.assets_housing), assets_general=iv(self.assets_general),
            assets_financial=iv(self.assets_financial), car_value=iv(self.car_value), debts=iv(self.debts),
            single_parent=bv(self.single_parent), obligor_over_limit=bv(self.obligor_over_limit),
            obligor_medical_capable=bv(self.obligor_medical_capable))


def region_index(hh: Household) -> int:
    key = (region_group(hh.region_sido, hh.region_sigungu), housing_zone(hh.region_sido, hh.region_sigungu),
           bp_region(hh.region_sido, hh.region_sigungu), hh.region_sido)
    for i, (_, sido, sgg) in enumerate(REGION_PROFILES):
        if (region_group(sido, sgg), housing_zone(sido, sgg), bp_region(sido, sgg), sido) == key:
            return i
    raise EncodeError(f"지역 프로필에 없는 지역: {hh.region_sido} {hh.region_sigungu}")


class Encoder:
    """규칙 세트를 기호 가구 하나에 대해 Z3 식으로 인코딩한다(결과 캐시)."""

    def __init__(self, rs: RuleSet, shh: SymHousehold) -> None:
        self.rs, self.h = rs, shh
        self._lib: dict[str, Any] = {}
        self._prog: dict[tuple[str, str], Any] = {}
        self._elig0: dict[str, z3.BoolRef] = {}
        self._benefit: dict[str, z3.ArithRef] = {}
        self._busy: set[str] = set()

    # --- 공개 API
    def eligible0(self, pid: str) -> z3.BoolRef:
        """1차(중복 제한 전) 자격."""
        if pid not in self._elig0:
            prog = self.rs.programs[pid]

            def run():
                c = self.cond(prog.eligibility, prog)
                if prog.region is not None:
                    c = z3.And(self.h.feature("region_sido").eq(prog.region.sido), c)
                return c
            self._elig0[pid] = self._guard(f"elig:{pid}", run)
        return self._elig0[pid]

    def eligible(self, pid: str) -> z3.BoolRef:
        """최종 자격(exclusions 적용, 평가기 2차와 동일)."""
        prog = self.rs.programs[pid]
        dropped = [z3.And(self.eligible0(pid), self.eligible0(ex.program)) for ex in prog.exclusions]
        return z3.And(self.eligible0(pid), z3.Not(z3.Or(dropped))) if dropped else self.eligible0(pid)

    def benefit0(self, pid: str) -> z3.ArithRef:
        if pid not in self._benefit:
            prog = self.rs.programs[pid]
            b = prog.benefit

            def run():
                v = R(self.ev((b.monthly or b.annual).tree.body, prog, None))
                if b.annual is not None:
                    v = v / 12
                return z3.If(self.eligible0(pid), zmax([0, zfloor(v + R(Fraction(1, 2)))]), R(0))
            self._benefit[pid] = self._guard(f"benefit:{pid}", run)
        return self._benefit[pid]

    def benefit(self, pid: str) -> z3.ArithRef:
        return z3.If(self.eligible(pid), self.benefit0(pid), R(0))

    def derived(self, name: str, program: Program | None = None) -> Any:
        if program is not None and name in program.derived:
            key = (program.id, name)
            if key not in self._prog:
                self._prog[key] = self._guard(
                    f"{program.id}.{name}", lambda: self.ev(program.derived[name].expr.tree.body, program, None))
            return self._prog[key]
        if name not in self._lib:
            d = self.rs.lib_derived[name]
            self._lib[name] = self._guard(name, lambda: self.ev(d.expr.tree.body, None, None))
        return self._lib[name]

    def expr(self, e: Expr, program: Program | None = None) -> Any:
        return self.ev(e.tree.body, program, None)

    # --- 내부
    def _guard(self, key, fn):
        if key in self._busy:
            raise EncodeError(f"순환 참조: {key}")
        self._busy.add(key)
        try:
            return fn()
        finally:
            self._busy.discard(key)

    def cond(self, c: Cond, prog: Program) -> z3.BoolRef:
        if isinstance(c, ExprCond):
            return B(self.ev(c.expr.tree.body, prog, None))
        if isinstance(c, AllCond):
            return z3.And([self.cond(x, prog) for x in c.all])
        if isinstance(c, AnyCond):
            return z3.Or([self.cond(x, prog) for x in c.any])
        if isinstance(c, NotCond):
            return z3.Not(self.cond(c.not_, prog))
        raise TypeError(c)

    def param(self, name: str) -> Any:
        v = self.rs.params.require(name)
        if isinstance(v, dict):
            raise EncodeError(f"params.{name} 은 표")
        return v

    def lookup(self, table: Any, keys: list[Any], name: str) -> Any:
        if not keys:
            if isinstance(table, dict):
                raise EncodeError(f"params.{name} 조회 키 부족")
            return R(table)
        key, rest = keys[0], keys[1:]
        if not isinstance(table, dict):
            raise EncodeError(f"params.{name} 차원 초과")
        if isinstance(key, (str, int, Fraction)) and not isinstance(key, bool):
            k = int(key) if isinstance(key, Fraction) and key.denominator == 1 else key
            for cand in (k, str(k)):
                if cand in table:
                    return self.lookup(table[cand], rest, name)
            raise EncodeError(f"params.{name} 에 키 {k!r} 없음")
        if isinstance(key, SymEnum):
            out = None
            for cond, v in reversed(key.cases):
                sub = self.lookup(table, [v] + rest, name)
                out = sub if out is None else z3.If(cond, sub, out)
            return out
        # 숫자형 기호 키: 표의 정수 키에 대해 If 사슬(범위 밖이면 마지막 키 값)
        keyed = [(k, v) for k, v in table.items() if isinstance(k, int) or str(k).isdigit()]
        if not keyed:
            raise EncodeError(f"params.{name}: 숫자 키 표가 아님")
        kz = R(key)
        out = self.lookup(keyed[-1][1], rest, name)
        for k, v in reversed(keyed[:-1]):
            out = z3.If(kz == R(int(k)), self.lookup(v, rest, name), out)
        return out

    def median_income(self, n: Any) -> z3.ArithRef:
        p = self.rs.params
        table = p.require("median_income")
        s7 = Fraction(p.require("median_income_size7"))
        inc = Fraction(p.require("median_income_increment_per_member"))
        nz = R(n)
        out = R(s7) + (nz - 7) * R(inc)
        out = z3.If(nz == 7, R(s7), out)
        for k in sorted(table, reverse=True):
            out = z3.If(nz == k, R(table[k]), out)
        return out

    def ev(self, n: ast.AST, prog: Program | None, person: SymPerson | None) -> Any:
        ev = lambda x: self.ev(x, prog, person)  # noqa: E731
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.Name):
            return self.derived(n.id, prog)
        if isinstance(n, ast.Attribute):
            base = n.value.id
            if base == "hh":
                return self.h.feature(n.attr)
            if base == "p":
                return person.attr(n.attr)
            return self.param(n.attr)
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.Not):
                return z3.Not(B(v))
            return -R(v) if isinstance(n.op, ast.USub) else R(v)
        if isinstance(n, ast.BinOp):
            a, b = R(ev(n.left)), R(ev(n.right))
            return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b}[type(n.op)]
        if isinstance(n, ast.BoolOp):
            vals = [B(ev(x)) for x in n.values]
            return z3.And(vals) if isinstance(n.op, ast.And) else z3.Or(vals)
        if isinstance(n, ast.Compare):
            parts, left = [], ev(n.left)
            for op, rn in zip(n.ops, n.comparators):
                right = ev(rn)
                parts.append(self._cmp(op, left, right))
                left = right
            return z3.And(parts) if len(parts) > 1 else parts[0]
        if isinstance(n, ast.IfExp):
            return ite(B(ev(n.test)), ev(n.body), ev(n.orelse))
        if isinstance(n, ast.Call):
            return self._call(n, prog, person)
        raise EncodeError(f"인코딩 불가 노드 {type(n).__name__}")

    def _cmp(self, op: ast.cmpop, a: Any, b: Any) -> z3.BoolRef:
        is_enum = lambda x: isinstance(x, (str, SymEnum))  # noqa: E731
        if is_enum(a) or is_enum(b):
            if isinstance(a, SymEnum) and all(isinstance(v, (int, Fraction)) for _, v in a.cases) \
                    and not is_enum(b):
                a = a.as_num()
            elif isinstance(b, SymEnum) and all(isinstance(v, (int, Fraction)) for _, v in b.cases) \
                    and not is_enum(a):
                b = b.as_num()
            else:
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    if isinstance(a, str) and isinstance(b, str):
                        eq = z3.BoolVal(a == b)
                    else:
                        ea, eb = (a, b) if isinstance(a, SymEnum) else (b, a)
                        eq = ea.eq(eb)
                    return eq if isinstance(op, ast.Eq) else z3.Not(eq)
                raise EncodeError("문자열은 == / != 만 비교 가능")
        if isinstance(a, (bool, z3.BoolRef)) or isinstance(b, (bool, z3.BoolRef)):
            if isinstance(op, ast.Eq):
                return B(a) == B(b)
            if isinstance(op, ast.NotEq):
                return B(a) != B(b)
            raise EncodeError("bool 크기 비교 불가")
        a, b = R(a), R(b)
        return {ast.Eq: a == b, ast.NotEq: a != b, ast.Lt: a < b, ast.LtE: a <= b,
                ast.Gt: a > b, ast.GtE: a >= b}[type(op)]

    def _call(self, n: ast.Call, prog: Program | None, person: SymPerson | None) -> Any:
        name, args = n.func.id, n.args
        ev = lambda x: self.ev(x, prog, person)  # noqa: E731
        if name == "min":
            return zmin([ev(a) for a in args])
        if name == "max":
            return zmax([ev(a) for a in args])
        if name == "if_":
            return ite(B(ev(args[0])), ev(args[1]), ev(args[2]))
        if name == "round":
            return zfloor(R(ev(args[0])) + R(Fraction(1, 2)))
        if name == "floor":
            return zfloor(ev(args[0]))
        if name == "ceil":
            return -zfloor(-R(ev(args[0])))
        if name == "lookup":
            table_name = args[0].value
            return self.lookup(self.rs.params.require(table_name), [ev(a) for a in args[1:]], table_name)
        if name == "median_income":
            return self.median_income(ev(args[0]))
        if name == "sum":
            return z3.Sum([z3.If(self.h.active(i), R(self.ev(args[0], prog, m)), R(0))
                           for i, m in enumerate(self.h.members)])
        if name == "count":
            return R(z3.Sum([z3.If(z3.And(self.h.active(i), B(self.ev(args[0], prog, m))), 1, 0)
                             for i, m in enumerate(self.h.members)]))
        if name == "elig":
            return self.eligible0(args[0].value)
        if name == "benefit":
            return self.benefit0(args[0].value)
        raise EncodeError(f"알 수 없는 함수 {name}")


def concrete(term: Any, shh: SymHousehold, hh: Household) -> Any:
    """기호 식에 구체 가구를 대입해 파이썬 값(Fraction/bool/str)으로 만든다(교차 일관성 검사용)."""
    pairs = shh.bind(hh)
    if isinstance(term, SymEnum):
        for cond, v in term.cases:
            if z3.is_true(z3.simplify(z3.substitute(cond, *pairs))):
                return v
        raise EncodeError("SymEnum 어느 경우에도 해당하지 않음")
    if isinstance(term, (str, int, float, Fraction, bool)):
        return term
    v = z3.simplify(z3.substitute(term, *pairs))
    if z3.is_true(v):
        return True
    if z3.is_false(v):
        return False
    if z3.is_rational_value(v):
        return Fraction(v.numerator_as_long(), v.denominator_as_long())
    if z3.is_int_value(v):
        return Fraction(v.as_long())
    raise EncodeError(f"구체값으로 단순화되지 않음: {v}")
