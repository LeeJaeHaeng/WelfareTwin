"""레드팀 리포트 — 문서 07 §4 형식.

    uv run python -m welfaretwin.redteam.report --preset heating
    uv run python -m welfaretwin.redteam.report --text "대구 ○○구 65세 이상 단독가구에 월 5만원 ..."
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from welfaretwin.redteam.agents import RedTeamResult, run
from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
PRESETS = {
    "heating": "대구광역시 남구 65세 이상 단독가구에 월 5만원 난방비 지원, 기준중위소득 60% 이하",
}
NAMES = {"ENERGY_VOUCHER": "에너지바우처", "BASIC_PENSION": "기초연금", "NBLS_LIVELIHOOD": "생계급여",
         "NBLS_HOUSING": "주거급여", "EITC": "근로장려금"}


def render(r: RedTeamResult) -> str:
    p, im, ad = r.program, r.impact, r.admin
    budget = im["monthly_budget_won_full_take_up"]
    overlap = ", ".join(f"{NAMES[k]} {v:.0%}" for k, v in sorted(im["overlap_rates"].items(), key=lambda kv: -kv[1])
                        if v > 0) or "없음"
    lines = [f"[정책 사전 점검 리포트] {p['name']}", "",
             f"사업안: {r.proposal}", "",
             "추출된 규칙(검토 필요):"]
    for c in p["eligibility"]["all"]:
        lines.append(f"  - {c['label']}: `{c['expr']}`" + ("  (불확실)" if c.get("uncertain") else ""))
    period = "monthly" if "monthly" in p["benefit"] else "annual"
    lines.append(f"  - 급여({'월' if period == 'monthly' else '연'}): `{p['benefit'][period]}`")
    region = p.get("region") or {}
    lines += ["", f"1. 영향: 대상 {im['eligible_households']:,.0f}가구(시도 단위 판정: {region.get('sido', '-')}) / "
                  f"월 재정 {budget / 1e6:,.1f}백만원(전원 신청 가정) / 기존 제도와 중복: {overlap}"]
    lines.append("2. 배제 위험(조건 하나 때문에 탈락하는 가구 유형):")
    for e in r.exclusion[:4]:
        why = "; ".join(f"{x['label']}{' — ' + x['detail'] if x['detail'] else ''}" for x in e["reasons"])
        lines.append(f"   - {e['household_text']}\n     → {why}")
    if not r.exclusion:
        lines.append("   - 발견되지 않음")
    lines.append("3. 허점 위험(방어 목적 점검 — 작은 변경으로 새로 자격을 얻는 경로와 보완안):")
    for f in r.loopholes:
        lines.append(f"   - {f['kind']}: 약 {f['households']:,.0f}가구에서 가능 → 보완: {f['remedy']}")
    if not r.loopholes:
        lines.append("   - 표본에서 발견되지 않음")
    lines.append("4. 급여 절벽:")
    for c in r.cliff:
        lines.append("   " + c["text"].replace("\n", "\n   "))
    if not r.cliff:
        lines.append("   - 발견되지 않음(점감 구간이 있거나 소득 조건이 없음)")
    lines.append(f"5. 행정 부담: 확인 단계 {ad['n_checks']}개({', '.join(ad['checks'])}), "
                 f"수작업 확인 조건 {len(ad['manual_checks'])}개, 중복 확인 대상 제도 "
                 f"{len(ad['overlap_review'])}개({', '.join(NAMES.get(k, k) for k in ad['overlap_review']) or '-'})")
    if r.extraction.get("not_modeled"):
        lines.append("   - 규칙으로 표현하지 못한 조건(담당자 확인 필요): "
                     + "; ".join(n["quote"] for n in r.extraction["not_modeled"]))
    lines += ["", f"근거: 반례 가구는 Z3·평가기로 재확인한 값, 영향은 합성 가구 표본 {im['sample_households']:,}개 기준. "
                  f"처리 시간 {r.timings['total']}초({', '.join(f'{k} {v}초' for k, v in r.timings.items() if k != 'total')})."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="정책 레드팀 리포트")
    ap.add_argument("--text")
    ap.add_argument("--preset", choices=list(PRESETS))
    ap.add_argument("--sample", type=int, default=20_000)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "demo")
    args = ap.parse_args(argv)
    text = args.text or PRESETS[args.preset or "heating"]
    r = run(text, sample=args.sample)
    md = render(r)
    print(md)
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"redteam_{args.preset}" if args.preset else f"redteam_{r.program['id']}"
    (args.out / f"{stem}.md").write_text(md + "\n", encoding="utf-8")
    (args.out / f"{stem}.json").write_text(json.dumps(dataclasses.asdict(r), ensure_ascii=False, indent=1, default=str),
                                           encoding="utf-8")
    print(f"→ {args.out / (stem + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
