"""규칙 테스트 자동 생성 — 문서 04 §5.

Z3 로 (1) 모든 조건을 만족하는 가구 (2) 조건 하나만 어기고 나머지는 만족하는 가구를 만들고,
평가기로 판정을 다시 계산해 기대값으로 고정한다(Z3 와 평가기가 다르면 오류).
결과는 tests/rules/<ID>.yaml 형식.
"""

from __future__ import annotations

import z3

from welfaretwin.model import Household
from welfaretwin.rules.dsl import AllCond, ExprCond, Program
from welfaretwin.rules.engine import RuleSet, evaluate
from welfaretwin.verify.encoder import REGION_PROFILES, Encoder, SymHousehold
from welfaretwin.verify.queries import _base_solver


def _to_case(name: str, hh: Household, program: Program, rs: RuleSet, source: str) -> dict:
    if program.region and program.region.sigungu and not program.region.sido.endswith("광역시"):
        hh = hh.model_copy(update={"region_sigungu": program.region.sigungu})
    d = {x.program_id: x for x in evaluate(hh, rs, [program.id])}[program.id]
    body = hh.model_dump(exclude={"hh_id"}, exclude_defaults=True)
    body["members"] = [m for m in body.get("members", [])] or [{"age": hh.members[0].age}]
    return {"name": name, "source": source, "household": body,
            "expect": {"eligible": d.eligible, "benefit_monthly": d.benefit_monthly}}


def generate(program: Program, rs: RuleSet, timeout_ms: int = 10_000, plain: bool = False) -> list[dict]:
    """plain=True 이면 '평범한 가구'(장애·부채·자동차·사업소득 없음 등)를 우선하는 소프트 제약(레드팀 배제 분석용)."""
    shh = SymHousehold()
    enc = Encoder(rs, shh)
    s = _base_solver(shh, True, True, timeout_ms, optimize=plain)
    if plain:
        for c, w in shh.plain():
            s.add_soft(c, w)
    if program.region:   # 해당 시도 프로필만(시도·시/군 구분이 같은 대표 지역)
        idx = [i for i, (_, sido, sgg) in enumerate(REGION_PROFILES) if sido == program.region.sido]
        sgg = program.region.sigungu or ""
        kind = [i for i in idx if (REGION_PROFILES[i][2] or "").endswith(sgg[-1:])] if sgg else []
        s.add(z3.Or([shh.region == i for i in (kind or idx)]))
    conds = program.eligibility.all if isinstance(program.eligibility, AllCond) else [program.eligibility]
    zc = [enc.cond(c, program) for c in conds]
    cases = []

    s.push()
    s.add(enc.eligible(program.id))
    if s.check() == z3.sat:
        hh = shh.decode(s.model(), "pos")
        case = _to_case("자동 — 모든 조건 충족", hh, program, rs, "testgen: Z3 양성 예")
        if not case["expect"]["eligible"]:
            raise AssertionError(f"{program.id}: Z3 양성 예를 평가기가 탈락시킴")
        cases.append(case)
    s.pop()

    for i, c in enumerate(conds):
        s.push()
        s.add([zc[j] for j in range(len(conds)) if j != i])
        s.add(z3.Not(zc[i]))
        if s.check() == z3.sat:
            hh = shh.decode(s.model(), f"neg{i}")
            label = c.label if isinstance(c, ExprCond) and c.label else f"조건 {i + 1}"
            case = _to_case(f"자동 — '{label}'만 불충족", hh, program, rs, "testgen: Z3 음성 예(조건 하나만 위반)")
            if case["expect"]["eligible"]:
                raise AssertionError(f"{program.id}: Z3 음성 예를 평가기가 통과시킴 — {label}")
            cases.append(case)
        s.pop()
    return cases
