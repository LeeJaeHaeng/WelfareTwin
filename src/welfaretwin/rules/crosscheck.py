"""LLM 초안 A·B 교차검증 — 문서 04 §4.

문자열 비교가 아니라 '판정이 같은가'를 Z3 로 묻는다.
- 자격: elig_A ≠ elig_B 인 가구가 있는가? 없으면(unsat) 두 초안은 수학적으로 동치.
  있으면 그 가구가 차이를 보여주는 반례(평가기로 재확인, 조건별로 어디서 갈렸는지 표시).
- 급여: 둘 다 자격인데 급여액이 다른 가구가 있는가?
"""

from __future__ import annotations

import z3

from welfaretwin.rules.dsl import Program
from welfaretwin.rules.engine import RuleSet, evaluate
from welfaretwin.verify.encoder import Encoder, SymHousehold
from welfaretwin.verify.explain import describe_household, failed_reasons
from welfaretwin.verify.queries import _base_solver


def _pair_ruleset(a: Program, b: Program, base: RuleSet) -> tuple[RuleSet, str, str]:
    ia, ib = f"{a.id}__A", f"{b.id}__B"
    pa = a.model_copy(update={"id": ia})
    pb = b.model_copy(update={"id": ib})
    return RuleSet({**base.programs, ia: pa, ib: pb}, base.libraries, base.params), ia, ib


def compare(a: Program, b: Program, base: RuleSet, timeout_ms: int = 15_000) -> dict:
    rs, ia, ib = _pair_ruleset(a, b, base)
    shh = SymHousehold(rs=rs)
    enc = Encoder(rs, shh)
    s = _base_solver(shh, True, True, timeout_ms)   # 현실성 + 읽기 쉬운 금액 단위
    out: dict = {}

    s.push()
    s.add(enc.eligible(ia) != enc.eligible(ib))
    r = s.check()
    if r == z3.unsat:
        out["eligibility"] = {"equivalent": True, "proof": "Z3: 자격 판정이 다른 가구가 존재하지 않음(unsat)"}
    elif r == z3.sat:
        hh = shh.decode(s.model(), "diff")
        d = {x.program_id: x for x in evaluate(hh, rs, [ia, ib])}
        out["eligibility"] = {
            "equivalent": False,
            "household": hh.model_dump(),
            "household_text": describe_household(hh),
            "A_eligible": d[ia].eligible, "B_eligible": d[ib].eligible,
            "A_failed": failed_reasons(hh, rs, ia), "B_failed": failed_reasons(hh, rs, ib),
        }
    else:
        out["eligibility"] = {"equivalent": None, "proof": "시간 초과(unknown)"}
    s.pop()

    s.push()
    s.add(enc.eligible(ia), enc.eligible(ib), enc.benefit(ia) != enc.benefit(ib))
    r = s.check()
    if r == z3.unsat:
        out["benefit"] = {"equivalent": True}
    elif r == z3.sat:
        hh = shh.decode(s.model(), "benefit_diff")
        d = {x.program_id: x for x in evaluate(hh, rs, [ia, ib])}
        out["benefit"] = {"equivalent": False, "household_text": describe_household(hh),
                          "A": d[ia].benefit_monthly, "B": d[ib].benefit_monthly}
    else:
        out["benefit"] = {"equivalent": None}
    s.pop()

    na = {n.quote for n in a.not_modeled}
    nb = {n.quote for n in b.not_modeled}
    out["not_modeled"] = {"A_only": sorted(na - nb), "B_only": sorted(nb - na), "both": sorted(na & nb)}
    out["agree"] = bool(out["eligibility"]["equivalent"] and out["benefit"]["equivalent"])
    return out
