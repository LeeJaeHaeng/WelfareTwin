"""표현식 화이트리스트·선형성 검사 — 같은 AST 를 Z3 로 옮길 수 있어야 한다(문서 04 §2.3)."""

import pytest

from welfaretwin.rules.expr import ExprError, parse


@pytest.mark.parametrize("src", [
    "hh.size >= 2 and not hh.single_parent",
    "round(median_income(hh.size) * params.selection_ratio_livelihood)",
    "if(hh.size == 1, 'single', 'multi')",
    "sum(p.earned_income * 0.7)",
    "count(p.age >= 65 or p.disability != 'none') >= 1",
    "lookup('housing_standard_rent', hh.housing_zone, min(hh.size, 6))",
    "x * (lookup('a', 'k') / lookup('b', 'k'))",
    "elig('NBLS_LIVELIHOOD') or benefit('EITC') > 0",
    "ceil(x / 10) * 10",
    "0 <= hh.head_age < 65",
])
def test_allowed(src):
    parse(src)


@pytest.mark.parametrize("src,msg", [
    ("hh.earned_income * hh.size", "비선형"),
    ("x * y", "비선형"),
    ("x / hh.size", "나눗셈"),
    ("__import__('os')", "함수"),
    ("open('f')", "함수"),
    ("hh.__class__", "가구 특성"),
    ("hh.nonexistent", "가구 특성"),
    ("p.age >= 65", "count.. 안에서만"),
    ("sum(count(p.age > 1))", "중첩"),
    ("lookup(hh.region_group, 1)", "문자열 상수"),
    ("[1, 2]", "허용되지 않는 구문"),
    ("x ** 2", "연산자"),
    ("lambda: 1", "허용되지 않는 구문"),
    ("max(1)", "인자 개수"),
])
def test_rejected(src, msg):
    with pytest.raises(ExprError, match=msg):
        parse(src)


def test_collects_references():
    e = parse("nbls_income_recognized <= round(median_income(hh.size) * params.selection_ratio_medical)"
              " and elig('NBLS_LIVELIHOOD')")
    assert e.names == {"nbls_income_recognized"}
    assert "selection_ratio_medical" in e.params and "median_income" in e.params
    assert e.programs == {"NBLS_LIVELIHOOD"}
