"""반례 → 사람이 읽는 설명 — 문서 05 §5.

수치는 항상 평가기(engine)로 다시 계산한 값을 쓴다. (LLM 문장 다듬기는 이후 단계에서 선택적으로)
"""

from __future__ import annotations

import ast
from collections import Counter

from welfaretwin.model import Household
from welfaretwin.rules.dsl import AllCond, AnyCond, ExprCond, NotCond, iter_conditions
from welfaretwin.rules.engine import RuleSet, _Ctx, evaluate, show
from welfaretwin.verify.queries import Finding, QueryResult

HOUSING_KO = {"own": "자가", "jeonse": "전세", "monthly_rent": "월세", "other": "기타(무상 등)"}


def won(v) -> str:
    return f"{round(float(v)):,}원"


def describe_household(hh: Household) -> str:
    ages = Counter(f"{p.age // 10 * 10}대" if p.age >= 10 else "10세 미만" for p in hh.members)
    comp = ", ".join(f"{k} {v}명" for k, v in sorted(ages.items(), key=lambda kv: kv[0]))
    tags = []
    if any(p.disability != "none" for p in hh.members):
        tags.append("장애인 포함")
    if any(p.is_student for p in hh.members):
        tags.append("학생 포함")
    if hh.single_parent:
        tags.append("한부모")
    region = hh.region_sido + (f" {hh.region_sigungu}" if hh.region_sigungu else "")
    earned = sum(p.earned_income for p in hh.members)
    business = sum(p.business_income for p in hh.members)
    other = sum(p.other_income for p in hh.members)
    income = f"월 소득 {won(earned + business + other)}(근로 {won(earned)}, 사업 {won(business)}, 기타 {won(other)})"
    assets = [(n, v) for n, v in (("주거용재산", hh.assets_housing), ("일반재산", hh.assets_general),
                                  ("금융재산", hh.assets_financial), ("자동차", hh.car_value),
                                  ("부채", hh.debts)) if v]
    asset_txt = ", ".join(f"{n} {won(v)}" for n, v in assets) or "재산 없음"
    rent = f" {won(hh.rent_monthly)}" if hh.housing == "monthly_rent" and hh.rent_monthly else ""
    extra = []
    if hh.obligor_over_limit:
        extra.append("부양의무자 고소득·고재산")
    if hh.obligor_medical_capable:
        extra.append("부양능력 있는 부양의무자")
    return (f"{hh.size}인 가구({comp}{', ' + ', '.join(tags) if tags else ''}), {region}, "
            f"{HOUSING_KO[hh.housing]}{rent}, {income}, {asset_txt}"
            + (f", {', '.join(extra)}" if extra else ""))


FEATURE_KO = {
    "size": "가구원 수", "n_elderly": "65세 이상 가구원 수", "n_child": "18세 미만 가구원 수",
    "n_disabled": "장애인 가구원 수", "n_pregnant": "임산부 수", "earned_income": "근로소득",
    "head_age": "가구주 나이",
}
MONEY_HINTS = ("소득", "재산", "기준", "액", "임차료", "income", "asset", "cap")


def _side_label(node: ast.AST, rs: RuleSet, prog) -> str:
    if isinstance(node, ast.Name):
        d = prog.derived.get(node.id) if prog else None
        d = d or rs.lib_derived.get(node.id)
        return (d.label if d and d.label else node.id)
    if isinstance(node, ast.Attribute) and node.value.id == "hh":
        return FEATURE_KO.get(node.attr, node.attr)
    if isinstance(node, ast.Constant):
        return ""
    return ast.unparse(node)


def _fmt(v, label: str) -> str:
    if isinstance(v, (bool, str)):
        return str(v)
    if any(h in label for h in MONEY_HINTS) or abs(float(v)) >= 1000:
        return won(v)
    return f"{v:g}" if isinstance(v, float) else str(v)


def _source(prog, ref_idx) -> str | None:
    if ref_idx is None:
        return None
    ref = prog.source_refs[ref_idx]
    return ref.doc + (f" p.{ref.page}" if ref.page else "") + (f" ({ref.clause})" if ref.clause else "")


def _compare_detail(c: ExprCond, ctx: _Ctx, rs: RuleSet, prog) -> str:
    node = c.expr.tree.body
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return ""
    left, right = ctx._ev(node.left, prog, None), ctx._ev(node.comparators[0], prog, None)
    ll, rl = _side_label(node.left, rs, prog), _side_label(node.comparators[0], rs, prog)
    lv, rv = _fmt(show(left), ll), _fmt(show(right), rl or ll)
    return f"{ll} {lv}" + (f" / {rl} {rv}" if rl else f" (기준 {rv})")


def failed_reasons(hh: Household, rs: RuleSet, pid: str) -> list[dict]:
    """해당 제도에서 불충족한 조건(좌·우변 값, 출처). '다음 중 하나' 묶음은 한 줄로."""
    prog = rs.programs[pid]
    ctx = _Ctx(hh, rs)
    out: list[dict] = []

    def ok(c) -> bool:
        if isinstance(c, ExprCond):
            return bool(ctx.eval(c.expr, prog))
        if isinstance(c, AllCond):
            return all(ok(x) for x in c.all)
        if isinstance(c, AnyCond):
            return any(ok(x) for x in c.any)
        return not ok(c.not_)

    def walk(c) -> None:
        if ok(c):
            return
        if isinstance(c, AllCond):
            for x in c.all:
                walk(x)
        elif isinstance(c, AnyCond):
            labels = [x.label or x.expr.src for x in iter_conditions(c)]
            refs = [x.ref for x in iter_conditions(c) if x.ref is not None]
            out.append({"label": "다음 중 어느 것에도 해당하지 않음: " + ", ".join(labels), "detail": "",
                        "source": _source(prog, refs[0] if refs else None)})
        elif isinstance(c, NotCond):
            out.append({"label": "제외 조건 해당", "detail": "", "source": None})
        else:
            out.append({"label": c.label or c.expr.src, "detail": _compare_detail(c, ctx, rs, prog),
                        "source": _source(prog, c.ref)})

    walk(prog.eligibility)
    return out


def explain_blindspot(f: Finding, rs: RuleSet, programs: list[str], idx: int) -> dict:
    hh = f.household
    lines = [f"[사각지대 유형 #{idx}] {describe_household(hh)}",
             f"  → 소득인정액 {won(f.facts['income_recognized'])} (기준중위소득의 {f.facts['median_pct']}%)"]
    progs = []
    for pid in programs:
        reasons = failed_reasons(hh, rs, pid)
        progs.append({"program": pid, "name": rs.programs[pid].name, "reasons": reasons})
        for r in reasons:
            lines.append(f"  - {rs.programs[pid].name}: {r['label']} 불충족"
                         + (f" — {r['detail']}" if r["detail"] else "")
                         + (f" [근거: {r['source']}]" if r["source"] else ""))
    return {"title": f"사각지대 유형 #{idx}", "household": hh.model_dump(), "facts": f.facts,
            "programs": progs, "text": "\n".join(lines)}


def explain_cliff(f: Finding, rs: RuleSet, idx: int) -> dict:
    hh, after = f.household, f.facts["household_after"]
    head_before, head_after = hh.members[0].earned_income, after.members[0].earned_income
    lines = [f"[급여 절벽 #{idx}] {describe_household(hh)}",
             f"  가구주 근로소득 {won(head_before)} → {won(head_after)} (+{won(f.facts['delta'])}) 이면 "
             f"가처분소득 {won(f.facts['disposable_before'])} → {won(f.facts['disposable_after'])} "
             f"(−{won(f.facts['loss'])})"]
    for pid, ch in f.facts["benefit_changes"].items():
        lines.append(f"  - {rs.programs[pid].name}: {won(ch['before'])} → {won(ch['after'])}")
    lost = []
    for pid in f.facts["lost_programs"]:
        reasons = failed_reasons(after, rs, pid)
        lost.append({"program": pid, "name": rs.programs[pid].name, "reasons": reasons})
        for r in reasons:
            lines.append(f"    · {rs.programs[pid].name} 탈락 사유: {r['label']}"
                         + (f" — {r['detail']}" if r["detail"] else "")
                         + (f" [근거: {r['source']}]" if r["source"] else ""))
    facts = {k: v for k, v in f.facts.items() if k != "household_after"}
    return {"title": f"급여 절벽 #{idx}", "household": hh.model_dump(), "household_after": after.model_dump(),
            "facts": facts, "lost": lost, "text": "\n".join(lines)}


def explain(result: QueryResult, rs: RuleSet) -> list[dict]:
    if result.query == "blindspot":
        return [explain_blindspot(f, rs, result.options["programs"], i + 1) for i, f in enumerate(result.findings)]
    if result.query == "cliff":
        return [explain_cliff(f, rs, i + 1) for i, f in enumerate(result.findings)]
    if result.query == "overlap":
        out = [{"title": f"중복 수급 가능: {' + '.join(f.facts['programs'])}", "household": f.household.model_dump(),
                "text": f"[중복] {' / '.join(f.facts['programs'])} 를 동시에 받을 수 있는 가구: "
                        f"{describe_household(f.household)}"} for f in result.findings]
        for pid in result.options["dead_rules"]:
            out.append({"title": f"사문화 규칙: {pid}", "text": f"[사문화] {rs.programs[pid].name}: 어떤 가구도 자격을 충족할 수 없음"})
        return out
    raise ValueError(result.query)


def program_summary(hh: Household, rs: RuleSet) -> list[str]:
    return [f"{d.program_name}: {'자격' if d.eligible else '탈락'} {won(d.benefit_monthly)}"
            for d in evaluate(hh, rs)]
