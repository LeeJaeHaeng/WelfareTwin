"""지자체 사업 규칙 추출 파이프라인 — 문서 04 §4, 문서 01 M4.

    uv run python -m welfaretwin.rules.pipeline run WLF00005860 WLF00005203 ...
    uv run python -m welfaretwin.rules.pipeline approve LOCAL_WLF00005860   # 사람 1클릭 승인

원문 → 초안 A·B(LLM) → 인용 검사 → Z3 동치 검사 → 테스트 자동 생성 → rules_db/review/
- review/<ID>.yaml          검수 대상 규칙(A 초안)
- review/<ID>.report.json   검증 리포트(인용·교차검증·테스트·LLM 사용량)
- review/<ID>.tests.yaml    자동 생성 테스트
- draft/<ID>__B.yaml        독립 재추출 초안 B
승인하면 approved/<ID>.yaml + tests/rules/<ID>.yaml 로 옮긴다.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import yaml

from welfaretwin.rules.crosscheck import compare
from welfaretwin.rules.dsl import RULES_DB, Program
from welfaretwin.rules.engine import RuleSet, load_ruleset
from welfaretwin.rules.extract import Draft, extract, load_service, to_program
from welfaretwin.rules.testgen import generate

ROOT = RULES_DB.parent
# claude-opus-5 단가(USD / 1M 토큰): 입력 5, 출력 25, 캐시 쓰기 1.25배, 캐시 읽기 0.1배
PRICE = {"in": 5.0, "out": 25.0, "cw": 6.25, "cr": 0.5}


def cost(calls: list[dict]) -> float:
    return sum(0 if c["cached"] else (c["input_tokens"] * PRICE["in"] + c["output_tokens"] * PRICE["out"]
                                      + c["cache_creation_input_tokens"] * PRICE["cw"]
                                      + c["cache_read_input_tokens"] * PRICE["cr"]) / 1e6 for c in calls)


def _dump(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")


def process(serv_id: str, base: RuleSet) -> dict:
    svc = load_service(serv_id)
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa, fb = ex.submit(extract, svc, "A", base), ex.submit(extract, svc, "B", base)
        da, db = fa.result(), fb.result()
    report = {"serv_id": serv_id, "name": svc.name, "region": f"{svc.sido} {svc.sigungu}".strip(),
              "A": _draft_info(da), "B": _draft_info(db),
              "llm_cost_usd": round(cost(da.llm + db.llm), 4)}
    pid = f"LOCAL_{serv_id}"
    if da.ok:
        prog_a = to_program(da.raw, svc, "A", da.llm[-1]["model"])
        prog_a["status"] = "review"
        prog_a["tests"] = f"tests/rules/{pid}.yaml"
        _dump(prog_a, RULES_DB / "review" / f"{pid}.yaml")
    if db.ok:
        _dump(to_program(db.raw, svc, "B", db.llm[-1]["model"]), RULES_DB / "draft" / f"{pid}__B.yaml")
    if da.ok and db.ok:
        report["crosscheck"] = compare(da.program, db.program, base)
    if da.ok:
        rs = RuleSet({**base.programs, pid: da.program}, base.libraries, base.params)
        try:
            cases = generate(da.program, rs)
            _dump({"program": pid, "cases": cases}, RULES_DB / "review" / f"{pid}.tests.yaml")
            report["tests"] = {"generated": len(cases)}
        except AssertionError as e:
            report["tests"] = {"generated": 0, "error": str(e)}
    reasons = []
    if not da.ok:
        reasons.append("A 초안 DSL 검증 실패")
    if not db.ok:
        reasons.append("B 초안 DSL 검증 실패")
    if da.quote_issues:
        reasons.append(f"A 인용 불일치 {len(da.quote_issues)}건")
    cc = report.get("crosscheck", {})
    if cc and not cc["agree"]:
        reasons.append("A·B 판정 불일치" if cc["eligibility"]["equivalent"] is False else "A·B 급여/판정 미확정")
    if report.get("tests", {}).get("generated", 0) == 0:
        reasons.append("테스트 생성 실패")
    if da.ok and any(c.uncertain for c in _expr_conds(da.program)):
        reasons.append("불확실(uncertain) 조건 포함")
    report["verdict"] = "auto_pass" if not reasons else "needs_review"
    report["review_reasons"] = reasons
    (RULES_DB / "review").mkdir(parents=True, exist_ok=True)
    (RULES_DB / "review" / f"{pid}.report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return report


def _expr_conds(p: Program):
    from welfaretwin.rules.dsl import iter_conditions
    return list(iter_conditions(p.eligibility))


def _draft_info(d: Draft) -> dict:
    return {"ok": d.ok, "errors": d.errors, "quote_issues": d.quote_issues,
            "n_conditions": len(d.raw.get("conditions", [])), "n_not_modeled": len(d.raw.get("not_modeled", [])),
            "llm": d.llm}


def approve(pid: str) -> None:
    src = RULES_DB / "review" / f"{pid}.yaml"
    prog = yaml.safe_load(src.read_text(encoding="utf-8"))
    prog["status"] = "approved"
    _dump(prog, RULES_DB / "approved" / f"{pid}.yaml")
    tests = RULES_DB / "review" / f"{pid}.tests.yaml"
    if tests.exists():
        (ROOT / "tests" / "rules" / f"{pid}.yaml").write_text(tests.read_text(encoding="utf-8"), encoding="utf-8")
    load_ruleset()   # 승인 후 전체 규칙 세트가 여전히 유효한지 확인
    print(f"승인: {pid} → rules_db/approved/, tests/rules/")


def write_table(reports: list[dict]) -> Path:
    lines = [f"# M4 지자체 사업 규칙 추출 결과 ({date.today().isoformat()})", "",
             "| 서비스 | 지역 | 조건 | 미모델링 | 인용(A) | A·B 자격 동치 | 테스트 | 판정 | 비용($) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        cc = r.get("crosscheck", {}).get("eligibility", {})
        eq = {True: "증명", False: "불일치", None: "미확정"}.get(cc.get("equivalent"), "-")
        lines.append(f"| {r['name']} ({r['serv_id']}) | {r['region']} | {r['A']['n_conditions']} | "
                     f"{r['A']['n_not_modeled']} | {'OK' if not r['A']['quote_issues'] else len(r['A']['quote_issues'])} | "
                     f"{eq} | {r.get('tests', {}).get('generated', 0)} | {r['verdict']} | {r['llm_cost_usd']} |")
    total = sum(r["llm_cost_usd"] for r in reports)
    agree = sum(1 for r in reports if r.get("crosscheck", {}).get("agree"))
    lines += ["", f"- 처리 {len(reports)}건, A·B 완전 일치(자격·급여) {agree}건, 자동 통과 "
                  f"{sum(r['verdict'] == 'auto_pass' for r in reports)}건, LLM 비용 합계 ${total:.2f}",
              "- '불일치' 항목은 review/<ID>.report.json 에 두 초안이 갈리는 구체적 가구(반례)가 있다."]
    path = ROOT / "reports" / f"m4_extract_{date.today().strftime('%Y%m%d')}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="지자체 사업 규칙 추출 파이프라인")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("serv_ids", nargs="+")
    r.add_argument("--workers", type=int, default=3)
    a = sub.add_parser("approve")
    a.add_argument("program_ids", nargs="+")
    args = ap.parse_args(argv)
    if args.cmd == "approve":
        for pid in args.program_ids:
            approve(pid)
        return 0
    base = load_ruleset()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        reports = list(ex.map(lambda s: process(s, base), args.serv_ids))
    for rep in reports:
        cc = rep.get("crosscheck", {}).get("eligibility", {})
        print(f"{rep['serv_id']} {rep['name']}: {rep['verdict']} {rep['review_reasons']} "
              f"동치={cc.get('equivalent')} 테스트={rep.get('tests', {}).get('generated')} ${rep['llm_cost_usd']}")
    print(f"→ {write_table(reports)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
