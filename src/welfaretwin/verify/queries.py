"""형식검증 질의 — 문서 05 §4.

Q1 blindspot : 빈곤선 이하인데 어떤 소득보장 제도에서도 탈락하는 가구(서로 다른 유형 N개)
Q2 cliff     : 근로소득이 Δ 늘었는데 가처분소득(소득 + 현금급여)이 줄어드는 가구(손실 큰 순)
Q3 overlap   : 배제 관계인데 둘 다 받을 수 있는 가구 / 어떤 가구도 받을 수 없는 사문화 규칙

반례는 항상 평가기(engine)로 다시 계산해 확인한다(Z3 와 평가기가 어긋나면 오류).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations

import z3

from welfaretwin.model import Household
from welfaretwin.rules.engine import RuleSet, _Ctx, evaluate
from welfaretwin.verify.encoder import R, Encoder, SymHousehold

# 현금 소득보장 제도(Q1 기본값). 의료급여는 현물이라 제외.
INCOME_SUPPORT = ["NBLS_LIVELIHOOD", "BASIC_PENSION", "EITC"]


@dataclass
class Finding:
    household: Household
    facts: dict = field(default_factory=dict)


@dataclass
class QueryResult:
    query: str
    status: str                      # sat | unsat | unknown
    findings: list[Finding]
    elapsed_sec: float
    options: dict


def _base_solver(shh: SymHousehold, realism: bool, readable: bool, timeout_ms: int,
                 optimize: bool = False) -> z3.Solver | z3.Optimize:
    s = z3.Optimize() if optimize else z3.Solver()
    s.set("timeout", timeout_ms)
    s.add(shh.domain())
    if realism:
        s.add(shh.realism())
    if readable:
        # 사람이 읽기 쉬운 반례: 소득은 만원, 재산은 백만원 단위
        for m in shh.members:
            for v in (m.earned_income, m.business_income, m.other_income):
                s.add(v % 10_000 == 0)
        for v in (shh.assets_housing, shh.assets_general, shh.assets_financial, shh.car_value, shh.debts):
            s.add(v % 1_000_000 == 0)
        s.add(shh.rent_monthly % 10_000 == 0)
    return s


def _check_engine(hh: Household, rs: RuleSet, expect: dict[str, bool]) -> None:
    got = {d.program_id: d.eligible for d in evaluate(hh, rs, list(expect))}
    if got != expect:
        raise AssertionError(f"Z3 반례와 평가기 판정 불일치: z3={expect} engine={got} hh={hh}")


# ------------------------------------------------------------------ Q1

def blindspot(
    rs: RuleSet,
    programs: list[str] | None = None,
    poverty_ratio: float = 0.40,
    basis: str = "income_recognized",
    n: int = 8,
    timeout_ms: int = 10_000,
    realism: bool = True,
    readable: bool = True,
    plain: bool = True,
) -> QueryResult:
    """빈곤선(기준중위소득 × poverty_ratio) 이하인데 programs 모두에서 탈락하는 가구 유형을 열거.

    basis: income_recognized(기초생활 소득인정액) | actual_income(실제 월 소득 합계, 재산 미반영)
    """
    programs = programs or INCOME_SUPPORT
    t0 = time.perf_counter()
    shh = SymHousehold(rs=rs)
    enc = Encoder(rs, shh)
    s = _base_solver(shh, realism, readable, timeout_ms, optimize=plain)
    if plain:   # 가장 '평범한' 반례부터 — 탈락 원인이 꾸밈 없이 드러나게
        for c, w in shh.plain():
            s.add_soft(c, w)
    if basis == "income_recognized":
        income = R(enc.derived("nbls_income_recognized"))
    elif basis == "actual_income":
        income = R(shh.feature("earned_income")) + R(shh.feature("business_income")) + R(shh.feature("other_income"))
    else:
        raise ValueError(basis)
    line = enc.median_income(shh.size) * R(Fraction(str(poverty_ratio)))
    s.add(income <= line)
    s.add(z3.And([z3.Not(enc.eligible(p)) for p in programs]))

    # 서로 다른 '유형'을 얻기 위한 차단 키: 가구원 수 × 노인 유무 × 아동 유무
    n_elderly, n_child = shh.feature("n_elderly"), shh.feature("n_child")
    keys = [shh.size, n_elderly > 0, n_child > 0]
    findings, status = [], "unsat"
    while len(findings) < n:
        r = s.check()
        if r != z3.sat:
            if not findings:
                status = str(r)
            break
        status = "sat"
        m = s.model()
        hh = shh.decode(m, hh_id=f"blindspot_{len(findings) + 1}")
        _check_engine(hh, rs, {p: False for p in programs})
        ctx = _Ctx(hh, rs)
        ir = ctx.derived("nbls_income_recognized", None)
        median = ctx.median_income(hh.size)
        findings.append(Finding(hh, {
            "income_recognized": round(float(ir)),
            "median_income": int(median),
            "median_pct": round(float(ir / median * 100), 1),
            "actual_income": sum(p.earned_income + p.business_income + p.other_income for p in hh.members),
        }))
        s.add(z3.Not(z3.And([k == m.eval(k, model_completion=True) for k in keys])))
    return QueryResult("blindspot", status, findings, time.perf_counter() - t0,
                       {"programs": programs, "poverty_ratio": poverty_ratio, "basis": basis, "n": n})


# ------------------------------------------------------------------ Q2

def _disposable(enc: Encoder, shh: SymHousehold, programs: list[str]) -> z3.ArithRef:
    income = R(shh.feature("earned_income")) + R(shh.feature("business_income")) + R(shh.feature("other_income"))
    return income + z3.Sum([enc.benefit(p) for p in programs])


def cliff(
    rs: RuleSet,
    delta: int = 10_000,
    n: int = 3,
    min_loss: int = 1,
    timeout_ms: int = 1_500,
    realism: bool = True,
    readable: bool = True,
    max_iter: int = 8,
    programs: list[str] | None = None,
    budget_sec: float = 8.0,
) -> QueryResult:
    """가구주의 월 근로소득이 delta 늘 때 가처분소득(소득 + 현금급여)이 min_loss 이상 줄어드는 가구.

    제도 P 마다 'P 를 잃으면서 손실이 나는' 반례를 찾고 손실을 키워 가며(최대 max_iter 회) 최대에
    가깝게 만든다. 손실이 큰 순으로 상위 n개를 돌려준다(원인 제도별로 서로 다른 절벽).
    전체 시간 예산 budget_sec 를 제도별로 나눠 쓰고, 예산 안에 '더 큰 손실 없음'을 증명하지 못하면
    proven_max=False 로 표시한다(찾은 손실 자체는 평가기로 재확인한 실제 값).
    """
    t0 = time.perf_counter()
    all_programs = list(rs.programs)
    cash = [pid for pid, prog in rs.programs.items()
            if (prog.benefit.monthly or prog.benefit.annual).src.strip() != "0"]
    h1, h2 = SymHousehold("a", rs), SymHousehold("b", rs)
    e1, e2 = Encoder(rs, h1), Encoder(rs, h2)
    s = _base_solver(h1, realism, readable, timeout_ms)
    s.add(h2.domain())
    # h2 = h1 과 같고 가구주 근로소득만 + delta
    same = [(h1.size, h2.size), (h1.region, h2.region), (h1.housing, h2.housing),
            (h1.rent_monthly, h2.rent_monthly), (h1.assets_housing, h2.assets_housing),
            (h1.assets_general, h2.assets_general), (h1.assets_financial, h2.assets_financial),
            (h1.car_value, h2.car_value), (h1.debts, h2.debts), (h1.single_parent, h2.single_parent),
            (h1.obligor_over_limit, h2.obligor_over_limit),
            (h1.obligor_medical_capable, h2.obligor_medical_capable)]
    for i, (m1, m2) in enumerate(zip(h1.members, h2.members)):
        for f in ("age", "sex", "disability", "is_student", "pregnant", "business_income", "other_income"):
            same.append((getattr(m1, f), getattr(m2, f)))
        if i == 0:
            s.add(m2.earned_income == m1.earned_income + delta, m2.employed == z3.BoolVal(True))
        else:
            same += [(m1.earned_income, m2.earned_income), (m1.employed, m2.employed)]
    s.add([a == b for a, b in same])
    loss = _disposable(e1, h1, cash) - _disposable(e2, h2, cash)
    s.add(loss >= min_loss)

    def as_frac(m, t):
        v = m.eval(t, model_completion=True)
        return Fraction(v.numerator_as_long(), v.denominator_as_long())

    targets = programs or cash
    per_program = budget_sec / len(targets)
    plain_hard = [c for c, _ in h1.plain()]   # '평범한 가구' 조건(1단계에서 강제)

    def search(deadline: float) -> tuple[z3.ModelRef | None, Fraction | None, bool]:
        """현재 제약에서 손실을 키워 가며 반례 탐색 → (모델, 손실, 최대임을 증명했는지)."""
        left_ms = lambda: max(100, int((deadline - time.perf_counter()) * 1000))  # noqa: E731
        best, bound = None, None
        for _ in range(max_iter):
            if time.perf_counter() > deadline and best is not None:
                break
            s.set("timeout", min(timeout_ms, left_ms()) if best is not None else left_ms())
            s.push()
            if bound is not None:   # 손실을 25% 이상 키운 해를 먼저 시도
                s.add(loss >= R(bound * Fraction(5, 4) + 1))
            r = s.check()
            if r != z3.sat:
                s.pop()
                if bound is None:
                    return None, None, r == z3.unsat
                s.push()
                s.add(loss > R(bound))
                s.set("timeout", min(timeout_ms, left_ms()))
                r = s.check()
                if r != z3.sat:
                    s.pop()
                    return best, bound, r == z3.unsat
            best = s.model()
            bound = as_frac(best, loss)
            s.pop()
        return best, bound, False

    candidates, proven, plain_found = [], {}, {}
    for pid in targets:
        t_start = time.perf_counter()
        s.push()
        s.add(e1.eligible(pid), z3.Not(e2.eligible(pid)))
        # 1단계: 평범한 가구(장애·부양의무자·부채·자동차·사업·기타소득 없음)에서,
        #        이 제도 하나만 자격이 바뀌는 '순수한' 절벽을 먼저 찾는다(제도별 절벽 크기)
        s.push()
        s.add(plain_hard)
        s.add([e1.eligible(o) == e2.eligible(o) for o in all_programs if o != pid])
        best, bound, proven[pid] = search(t_start + per_program * 0.6)
        s.pop()
        plain_found[pid] = best is not None
        if best is None:   # 2단계: 조건 해제
            best, bound, proven[pid] = search(t_start + per_program)
        s.pop()
        if best is not None:
            candidates.append((bound, pid, best))

    findings = []
    for bound, pid, best in sorted(candidates, key=lambda c: -c[0])[:n]:
        hh1, hh2 = h1.decode(best, f"cliff_{pid}_before"), h2.decode(best, f"cliff_{pid}_after")
        d1 = {d.program_id: d for d in evaluate(hh1, rs)}
        d2 = {d.program_id: d for d in evaluate(hh2, rs)}
        disp = lambda hh, d: sum(p.earned_income + p.business_income + p.other_income  # noqa: E731
                                 for p in hh.members) + sum(d[p].benefit_monthly for p in cash)
        if disp(hh1, d1) - disp(hh2, d2) != bound:
            raise AssertionError(f"Z3 손실 {bound} != 평가기 손실 {disp(hh1, d1) - disp(hh2, d2)}")
        findings.append(Finding(hh1, {
            "trigger_program": pid, "delta": delta, "loss": int(bound),
            "disposable_before": disp(hh1, d1), "disposable_after": disp(hh2, d2),
            "lost_programs": [p for p in all_programs if d1[p].eligible and not d2[p].eligible],
            "benefit_changes": {p: {"before": d1[p].benefit_monthly, "after": d2[p].benefit_monthly}
                                for p in all_programs if d1[p].benefit_monthly != d2[p].benefit_monthly},
            "household_after": hh2, "proven_max": proven[pid], "plain_household": plain_found[pid],
        }))
    return QueryResult("cliff", "sat" if findings else "unsat", findings, time.perf_counter() - t0,
                       {"delta": delta, "n": n, "min_loss": min_loss, "budget_sec": budget_sec,
                        "searched_programs": targets,
                        "found_programs": [c[1] for c in sorted(candidates, key=lambda c: -c[0])]})


# ------------------------------------------------------------------ Q3

def overlap(rs: RuleSet, timeout_ms: int = 10_000, realism: bool = True) -> QueryResult:
    """(1) 선언된 배제(exclusions) 쌍을 둘 다 1차 자격으로 만족하는 가구 (2) 사문화 규칙."""
    t0 = time.perf_counter()
    shh = SymHousehold(rs=rs)
    enc = Encoder(rs, shh)
    s = _base_solver(shh, realism, True, timeout_ms)
    findings, dead, unknown = [], [], []
    pairs = {(p.id, ex.program) for p in rs.programs.values() for ex in p.exclusions}
    for a, b in sorted(pairs):
        s.push()
        s.add(enc.eligible0(a), enc.eligible0(b))
        if s.check() == z3.sat:
            hh = shh.decode(s.model(), f"overlap_{a}_{b}")
            findings.append(Finding(hh, {"kind": "overlap", "programs": [a, b]}))
        s.pop()
    for pid in rs.programs:
        s.push()
        s.add(enc.eligible(pid))
        r = s.check()
        if r == z3.unsat:
            dead.append(pid)
        elif r == z3.unknown:
            unknown.append(pid)
        s.pop()
    status = "sat" if findings or dead else ("unknown" if unknown else "unsat")
    return QueryResult("overlap", status, findings, time.perf_counter() - t0,
                       {"exclusion_pairs": sorted(pairs), "dead_rules": dead, "unknown": unknown,
                        "co_eligible_pairs": co_eligible_pairs(rs, enc, s)})


def co_eligible_pairs(rs: RuleSet, enc: Encoder, s: z3.Solver) -> list[list[str]]:
    """참고: 동시에 받을 수 있는 제도 쌍(배제 선언이 없으면 중복이 '허용'된 것)."""
    out = []
    for a, b in combinations(sorted(rs.programs), 2):
        s.push()
        s.add(enc.eligible(a), enc.eligible(b))
        if s.check() == z3.sat:
            out.append([a, b])
        s.pop()
    return out
