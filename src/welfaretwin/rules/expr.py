"""규칙 표현식 언어 — 문서 04 §2.3.

Python `ast` 로 파싱한 뒤 화이트리스트 노드만 허용한다. 같은 AST 를
① 평가기(engine.py) ② Z3 인코더(verify/encoder.py, M2) 가 공유한다.

허용
- 상수: 정수·실수·문자열·True/False
- 이름: `hh.<특성>`(가구), `p.<속성>`(sum/count 안의 가구원), `params.<이름>`(기준값),
  그 밖의 맨 이름은 derived(파생 변수) 참조
- 연산: + - * / (곱셈은 한쪽이 상수, 나눗셈은 상수로만 → 선형 유지), 비교(연쇄 허용),
  and / or / not, 단항 -
- 함수: min, max, if(c, a, b), round(x)(원 미만 반올림), floor(x), ceil(x),
  lookup('<params 이름>', key[, key2]), median_income(n),
  sum(<가구원 식>), count(<가구원 조건>), elig('<제도 ID>'), benefit('<제도 ID>')
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

HH_FEATURES = {
    "size", "n_elderly", "n_child", "n_disabled", "n_severe_disabled", "n_pregnant",
    "earned_income", "business_income", "other_income",
    "assets_housing", "assets_general", "assets_financial", "car_value", "debts",
    "rent_monthly", "housing", "region_sido", "region_sigungu", "region_group", "housing_zone", "bp_region",
    "single_parent", "obligor_over_limit", "obligor_medical_capable", "head_age",
}
PERSON_ATTRS = {
    "age", "sex", "disability", "is_student", "employed", "pregnant",
    "earned_income", "business_income", "other_income",
}
FUNCS = {
    "min": (2, None), "max": (2, None), "if_": (3, 3), "round": (1, 1), "floor": (1, 1),
    "ceil": (1, 1), "lookup": (2, 3), "median_income": (1, 1), "sum": (1, 1), "count": (1, 1),
    "elig": (1, 1), "benefit": (1, 1),
}
PERSON_SCOPE_FUNCS = {"sum", "count"}
STRING_ARG_FUNCS = {"lookup": 0, "elig": 0, "benefit": 0}

_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Constant, ast.Name, ast.Load, ast.Attribute,
    ast.Call, ast.IfExp,
)


class ExprError(ValueError):
    pass


@dataclass(frozen=True)
class Expr:
    src: str
    tree: ast.Expression
    names: frozenset[str]          # derived 참조
    params: frozenset[str]         # params.<이름> 및 lookup 테이블
    programs: frozenset[str]       # elig/benefit 참조

    def __str__(self) -> str:
        return self.src


def _normalize(src: str) -> str:
    # 문서 04 표기 `if(c, a, b)` 는 파이썬 예약어라 if_ 로 바꿔 파싱한다.
    return re.sub(r"\bif\s*\(", "if_(", src.strip())


def parse(src: str | int | float | bool) -> Expr:
    if isinstance(src, (int, float, bool)):
        src = repr(src)
    text = _normalize(str(src))
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"구문 오류: {src!r} ({e.msg})") from e
    v = _Validator(src)
    v.visit(tree)
    return Expr(str(src), tree, frozenset(v.names), frozenset(v.params), frozenset(v.programs))


def is_const(node: ast.AST) -> bool:
    """상수식 여부(params 값 포함). 선형성 검사용."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "params"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return is_const(node.operand)
    if isinstance(node, ast.BinOp):
        return is_const(node.left) and is_const(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "lookup":
        return all(isinstance(a, ast.Constant) for a in node.args)
    return False


class _Validator(ast.NodeVisitor):
    def __init__(self, src: str) -> None:
        self.src = src
        self.names: set[str] = set()
        self.params: set[str] = set()
        self.programs: set[str] = set()
        self._person_scope = 0

    def fail(self, msg: str) -> None:
        raise ExprError(f"{msg} — 식: {self.src!r}")

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, _ALLOWED):
            self.fail(f"허용되지 않는 구문 {type(node).__name__}")
        super().generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, (int, float, str, bool)):
            self.fail(f"허용되지 않는 상수 {node.value!r}")

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Mult) and not (is_const(node.left) or is_const(node.right)):
            self.fail("변수×변수 곱셈 금지(비선형)")
        if isinstance(node.op, ast.Div) and not is_const(node.right):
            self.fail("나눗셈은 상수로만")
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            self.fail("허용되지 않는 연산자")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not isinstance(node.value, ast.Name):
            self.fail("중첩 속성 접근 금지")
        base, attr = node.value.id, node.attr
        if base == "hh":
            if attr not in HH_FEATURES:
                self.fail(f"알 수 없는 가구 특성 hh.{attr}")
        elif base == "p":
            if not self._person_scope:
                self.fail("p.<속성> 은 sum()/count() 안에서만")
            if attr not in PERSON_ATTRS:
                self.fail(f"알 수 없는 가구원 속성 p.{attr}")
        elif base == "params":
            self.params.add(attr)
        else:
            self.fail(f"알 수 없는 네임스페이스 {base}")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in {"hh", "p", "params"}:
            self.fail(f"{node.id} 단독 사용 금지")
        if node.id in FUNCS:
            self.fail(f"{node.id} 는 함수")
        self.names.add(node.id)

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
            self.fail(f"허용되지 않는 함수 {ast.unparse(node.func)}")
        if node.keywords:
            self.fail("키워드 인자 금지")
        name = node.func.id
        lo, hi = FUNCS[name]
        if len(node.args) < lo or (hi is not None and len(node.args) > hi):
            self.fail(f"{name} 인자 개수 오류")
        if name in STRING_ARG_FUNCS:
            a = node.args[STRING_ARG_FUNCS[name]]
            if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
                self.fail(f"{name} 의 첫 인자는 문자열 상수")
            {"lookup": self.params, "elig": self.programs, "benefit": self.programs}[name].add(a.value)
        if name == "median_income":
            self.params.update({"median_income", "median_income_size7",
                                "median_income_increment_per_member"})
        if name in PERSON_SCOPE_FUNCS:
            if self._person_scope:
                self.fail("sum/count 중첩 금지")
            self._person_scope += 1
            self.visit(node.args[0])
            self._person_scope -= 1
            return
        for a in node.args:
            if name in STRING_ARG_FUNCS and a is node.args[STRING_ARG_FUNCS[name]]:
                continue
            self.visit(a)
