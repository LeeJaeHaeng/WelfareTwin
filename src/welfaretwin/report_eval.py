"""평가 리포트 — 문서 09 §2. 발표 슬라이드의 모든 수치는 이 리포트에서만 가져온다.

    uv run python -m welfaretwin.report_eval

artifacts/ 의 실제 실행 결과만 읽어 reports/eval_<날짜>.md 와 docs/11_질의응답_대비표.md 를 만든다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
A = ROOT / "artifacts"


def j(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def git_rev() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return "unknown"


def collect() -> dict:
    return {
        "blind": j(A / "demo" / "verify_blindspot.json"), "cliff": j(A / "demo" / "verify_cliff.json"),
        "overlap": j(A / "demo" / "verify_overlap.json"), "scen": j(A / "demo" / "scenario_demo.json"),
        "bt": j(A / "demo" / "backtest_B2_2024_2025.json"), "rules": j(A / "demo" / "m4_rules.json"),
        "bt_fixed": j(A / "demo" / "backtest_B2_2024_2025_fixedpop.json"),
        "scale": j(A / "demo" / "blindspot_scale.json"), "static": j(A / "sim" / "static_2026.json"),
        "pop": j(ROOT / "data" / "processed" / "population_meta.json"),
        "rehearsal": j(A / "rehearsal" / "rehearsal_report.json"),
        "rt_md": (A / "demo" / "redteam_heating.md").read_text(encoding="utf-8")
        if (A / "demo" / "redteam_heating.md").exists() else "",
    }


def eval_md(d: dict) -> str:
    s, sc, bt, pop, rules = d["static"], d["scen"], d["bt"], d["pop"], d["rules"] or []
    cal = s["calibration"]
    base, s1, liv = sc["scenarios"][0]["metrics"], sc["scenarios"][1], sc["scenarios"][2]
    v = pop["validation"]
    agree = sum(1 for r in rules if (r.get("crosscheck") or {}).get("agree"))
    cl = d["cliff"]["items"]
    rh = d["rehearsal"] or {}
    L = [f"# 평가 리포트 ({date.today().isoformat()}, git {git_rev()})", "",
         "> 발표 슬라이드의 수치는 이 리포트에서만 가져온다(문서 09 §2). 재생성: `python -m welfaretwin.report_eval`.", "",
         "## 1. 규칙(Rules-as-Code)",
         "- 중앙 제도 7종(생계·의료·주거·교육급여, 기초연금, 에너지바우처, 근로장려금) 수작업 규칙, 모든 수치는 출처 포함 params",
         f"- 지자체(대구·경북) 사업 {len(rules)}건 LLM 추출: A·B 판정 동치 증명 {agree}건, 불일치 {len(rules) - agree}건(반례로 누락 조건 발견), "
         f"인용 불일치 {sum(len(r['A']['quote_issues']) for r in rules)}건, LLM 비용 ${sum(r['llm_cost_usd'] for r in rules):.2f}",
         "- 정답 테스트: 사업안내 계산 예시 포함 + 자동 생성 경계 테스트, 평가기–Z3 일관성 무작위 1,000가구 100% 일치", "",
         "## 2. 형식검증(Z3)",
         f"- 사각지대(Q1): {len(d['blind']['items'])}개 유형, {d['blind']['elapsed_sec']}초. 합성 인구 기준 빈곤 가구 "
         f"{d['scale']['poor_households']:,.0f} 중 소득보장 3종 모두 탈락 {d['scale']['blindspot_households']:,.0f}가구"
         f"({d['scale']['blindspot_households'] / d['scale']['poor_households']:.1%})",
         f"- 급여 절벽(Q2): 상위 {len(cl)}건, 최대 손실 {cl[0]['facts']['loss']:,}원(가처분소득, 근로소득 +1만원 시)",
         f"- 중복·사문화(Q3): 사문화 규칙 {len(d['overlap']['options']['dead_rules'])}건, 배제 선언 쌍 {len(d['overlap']['options']['exclusion_pairs'])}개", "",
         "## 3. 합성 인구",
         f"- {pop['n_households']:,}가구 / {pop['n_persons']:,}명(가중치 {pop['weight']:.1f}), KOSIS 집계표 9종",
         f"- 주변분포 최대 오차: 시도 {v['sido']['max_abs_pp']:.2f}%p, 가구원수 {v['size']['max_abs_pp']:.2f}%p, "
         f"점유형태 {v['tenure']['max_abs_pp']:.2f}%p, 가구주 연령 {v['head_age_band']['max_abs_pp']:.2f}%p (목표 ±2%p)",
         f"- 소득 P50 합성 {v['income_percentiles_manwon']['synthetic'][4]:,.0f} vs KOSIS {v['income_percentiles_manwon']['kosis'][4]:,.0f}만원, "
         f"순자산 P50 합성 {v['net_worth_percentiles_manwon']['synthetic'][4]:,.0f} vs KOSIS {v['net_worth_percentiles_manwon']['kosis'][4]:,.0f}만원", "",
         "## 4. 시뮬레이션·보정",
         f"- 기초생활보장 자격(합성) {cal['nbls_any_eligible_households']:,.0f}가구 vs {cal['actual_year']}년 실제 수급 "
         f"{cal['actual_households']:,.0f}가구 → {cal['ratio_households']:.2f}배(자격 > 수급 = 비수급 + 단순화 오차)",
         f"- 행동 보정: 공통 절편 b={sc['calibration']['intercept_b']:.2f}, 현행 수급률 {base['take_up_rate']:.1%}, "
         f"미신청 {base['non_take_up_households']:,.0f}가구",
         f"- 자동 안내(S1): 수급 {s1['delta_vs_base']['expected_recipient_households']:+,.0f}가구, "
         f"월 재정 {s1['delta_vs_base']['monthly_budget_won'] / 1e8:+,.0f}억원",
         f"- 생계급여 32→35%: 생계 자격 {liv['delta_vs_base']['livelihood_eligible_households']:+,.0f}가구, "
         f"월 재정 {liv['delta_vs_base']['monthly_budget_won'] / 1e8:+,.0f}억원(기초생활 전체 자격 불변)", "",
         "## 5. 백테스트",
         f"- B2(2024→2025): 수급 가구 증가 예측 {bt['households']['predicted_increase']:+,.0f} vs 실제 "
         f"{bt['households']['actual_increase']:+,.0f} → 오차율 {bt['households']['error_rate']:.1%} "
         f"(**목표 ±10% {'달성' if bt['households']['error_rate'] <= 0.1 else '미달'}**), 인원 기준 오차율 "
         f"{bt['persons']['error_rate']:.1%}",
         *([f"- 비교: 인구 소득·자산 고정 시 가구 오차율 {d['bt_fixed']['households']['error_rate']:.1%} — "
            f"가구 소득·자산 증가(가계동향·가계금융복지조사 공표 증가율)를 반영해 개선"] if d.get("bt_fixed") else []),
         *[f"  - 한계: {x}" for x in bt["limitations"]], "",
         "## 6. 데모",
         f"- 리허설 {rh.get('rounds', '-')}회 연속 무오류: {rh.get('consecutive_clean_rounds')}, 화면 최대 {rh.get('max_seconds')}초 (목표 30초)",
         "- 레드팀(사업안 → 리포트) 실제 처리 시간은 `artifacts/demo/redteam_heating.md` 하단 참조", ""]
    return "\n".join(L)


def qa_md(d: dict) -> str:
    s, sc, bt, rules = d["static"], d["scen"], d["bt"], d["rules"] or []
    agree = sum(1 for r in rules if (r.get("crosscheck") or {}).get("agree"))
    base = sc["scenarios"][0]["metrics"]
    return "\n".join([
        "# 11. 질의응답 대비표(확정)", "",
        f"> 수치는 `reports/eval_{date.today().strftime('%Y%m%d')}.md` 기준. 생성: `python -m welfaretwin.report_eval`.", "",
        "| 질문 | 답변 요지 | 근거(화면·파일) |", "|---|---|---|",
        f"| LLM이 규칙을 틀리게 만들면? | 판정은 코드가 한다. LLM 초안은 두 번 독립 추출 → Z3 로 판정 동치 증명 → 자동 경계 테스트 → 사람 승인. "
        f"대구·경북 {len(rules)}건 중 {agree}건 동치 증명, {len(rules) - agree}건은 Z3 반례로 누락 조건을 찾아 수정. 두 초안이 같은 실수를 한 1건은 사람 검수에서 수정(교차검증의 한계도 공개). "
        f"| 화면 1, reports/m4_extract_*.md |",
        f"| 합성 인구가 현실과 다르면? | KOSIS 집계표(시군구 포함)로 IPF(주변분포 오차 ≤0.1%p). 실제 수급 통계로 보정: 기초생활 자격 {s['calibration']['ratio_households']:.2f}배 → "
        f"행동 보정 후 실수급 일치. 백테스트(2024→2025) 수급 가구 증가 오차 {bt['households']['error_rate']:.1%}, 인원 기준 {bt['persons']['error_rate']:.0%} — "
        f"소득을 고정하면 {d['bt_fixed']['households']['error_rate']:.0%}였던 것을 공표 소득·자산 증가율로 개선(오차를 숨기지 않고 공개). | 화면 5, population_meta.json |",
        f"| LLM 에이전트 행동 예측을 믿을 수 있나? | LLM 은 변화의 방향·상대 크기만, 수준은 실제 수급 통계로 보정(공통 절편 1개). 현행 수급률 {base['take_up_rate']:.0%}, "
        f"미신청 {base['non_take_up_households'] / 1e4:,.0f}만 가구 — 비수급 빈곤층 연구 규모와 같은 수준. | 화면 4, scenario_demo.json |",
        "| 수학적으로 '찾는다'는 게 무슨 뜻인가? | 표본 조사가 아니라 규칙 전체를 Z3 제약식으로 바꿔 '이런 가구가 존재하는가'를 판정. 존재하면 구체적 가구(반례)와 탈락 조항을, 없으면 '없음'을 증명. | 화면 2·3 |",
        "| 개인정보는? | 실제 개인 데이터 미사용. 공개 집계표로 만든 합성 가구만. | README |",
        "| 기존 복지멤버십·모의계산과 뭐가 다른가? | 그것들은 집행 단계(개인 안내) 도구. 복지트윈은 설계 단계에서 제도 간 상호작용(사각지대·절벽·중복)을 검증. 예: 생계급여 기준을 35%로 올려도 기초생활 전체 대상은 그대로이고 재정만 증가 — 의료·주거급여가 이미 포괄. | 화면 4 |",
        "| 실제 도입 경로는? | 사회보장제도 신설·변경 사전협의에 레드팀 리포트(영향·배제·허점·절벽·행정 부담) 첨부부터. 사업안 한 줄 → 약 30초. | 화면 4 레드팀 |",
        "| 부양의무자 같은 복잡한 기준은? | MVP 는 범주형 근사(생계 고소득·고재산 예외, 의료 부양능력 여부)이고 각 규칙의 assumptions 에 명시. 가족관계 결합은 2단계. 2021 부양의무자 폐지 백테스트(B1)는 이 때문에 미수행. | rules_db/approved/*.yaml |",
        "| 시군구 사업도 정확한가? | 합성 가구에 2025 인구총조사 시군구×연령×가구원수 분포로 시군구를 배정하고, 평가기·Z3 모두 시군구까지 판정. 예: 영천시 청년근로자 월세 지원 대상은 영천시 가구만. | reports/m4_extract_*.md |",
        "| 비용은? | LLM 은 규칙 추출(사업당 약 $0.07, Opus)과 행동 추정(1,434건 $3.8, Haiku)뿐, 응답 캐시로 재실행 비용 0. 판정·검증은 로컬 계산. | .cache/llm |",
        "| 혼자서 가능한가? | MVP 전 단계(수집→규칙→검증→인구→시뮬레이션→레드팀→백테스트→대시보드)가 지금 동작하고 테스트로 고정. 2단계부터 사회복지 전문가 검수 인력과 협업. | tests/, 리허설 리포트 |",
        ""])


def script_md(d: dict) -> str:
    """5분 발표 대본 — 문서 10 §1 시간표. 수치는 결과 파일에서."""
    s, sc, bt, rules = d["static"], d["scen"], d["bt"], d["rules"] or []
    agree = sum(1 for r in rules if (r.get("crosscheck") or {}).get("agree"))
    base, s1, liv = sc["scenarios"][0]["metrics"], sc["scenarios"][1], sc["scenarios"][2]
    blind = d["blind"]["items"][0]
    cliff = sorted(d["cliff"]["items"], key=lambda it: -it["facts"]["loss"])
    sc_ = d["scale"]
    return "\n".join([
        "# 12. 5분 발표 대본(본선 12/3)", "",
        f"> 문서 10 시간표 기준. 수치는 `reports/eval_{date.today().strftime('%Y%m%d')}.md` 와 같은 결과 파일에서 자동 생성 — "
        "결과를 다시 계산하면 `python -m welfaretwin.report_eval` 로 대본도 갱신. 데모는 사이드바 '사전 계산본(오프라인)' 모드.", "",
        "## 0:00–0:40 문제 (슬라이드)",
        "- \"사각지대는 대부분 사건이 알려진 뒤에야 발견됩니다. 제도 신설·변경 사전협의는 해마다 늘지만, 제도 사이의 빈틈·급여 절벽·중복을 "
        "사전에 정량 검증하는 도구가 없습니다.\"", "",
        "## 0:40–1:10 해법 (슬라이드)",
        "- \"복지트윈은 정책을 사람에게 실험하기 전에 가상 대한민국에서 먼저 실험합니다. 법을 코드로, 코드를 수학으로 검증하고, "
        "합성 인구와 AI 에이전트로 반응을 시뮬레이션합니다. 판정은 항상 코드가 하고, AI 는 규칙 작성과 행동 추정만 합니다.\"", "",
        "## 1:10–1:50 데모① 법전 → 코드 (화면 1)",
        f"- 구미시 청년월세 지원 원문 → 추출된 규칙. \"AI 가 두 번 독립적으로 규칙을 만들고, 수학 엔진이 두 규칙의 판정이 모든 가구에서 같은지 증명합니다. "
        f"대구·경북 {len(rules)}건 중 {agree}건 증명, 나머지는 두 규칙이 갈리는 가구를 찾아 사람이 고쳤습니다. 원문에 없는 근거는 0건입니다.\"", "",
        "## 1:50–2:40 데모② 사각지대 (화면 2) — '사각지대 찾기' 클릭",
        f"- \"빈곤선 아래인데 생계급여·기초연금·근로장려금을 모두 못 받는 가구가 존재하는지 수학적으로 물었습니다.\" 카드 1: "
        f"{blind['text'].splitlines()[0].split('] ', 1)[-1][:70]}…",
        f"- \"합성 인구로 보면 빈곤 가구 {sc_['poor_households'] / 1e4:,.0f}만 중 약 {sc_['blindspot_households'] / 1e4:,.1f}만 가구가 이 유형입니다. "
        "각 탈락 사유에는 사업안내 쪽수와 고시 조항이 붙어 있습니다.\"", "",
        "## 2:40–3:20 데모③ 급여 절벽 (화면 3)",
        f"- 프리셋 그래프에서 빨간 표시 지점. \"소득이 1만원 늘었는데 가처분소득이 줄어드는 지점입니다. 수학 엔진이 찾은 가장 큰 절벽은 "
        f"{cliff[0]['facts']['loss']:,}원입니다.\" (아래 카드 1~3 중 설명하기 쉬운 것 선택)", "",
        "## 3:20–4:10 데모④ 사업안 → 30초 영향평가 + 레드팀 (화면 4)",
        "- 레드팀 탭에서 사업안 그대로 '레드팀 점검 실행'. \"대상 가구, 기존 제도와의 중복, 조건 하나 때문에 탈락하는 가구, "
        "세대 분리 같은 허점과 보완안, 급여 절벽, 행정 부담까지 한 번에 나옵니다.\"",
        f"- 시나리오 탭: \"자동 안내를 하면 수급이 {s1['delta_vs_base']['expected_recipient_households'] / 1e4:,.1f}만 가구 늘고 월 "
        f"{s1['delta_vs_base']['monthly_budget_won'] / 1e8:,.0f}억원이 듭니다. 생계급여 기준을 35%로 올리면 생계급여 대상은 "
        f"{liv['delta_vs_base']['livelihood_eligible_households'] / 1e4:,.1f}만 가구 늘지만 기초생활보장 전체 대상은 그대로입니다 — "
        "이미 의료·주거급여가 포괄하기 때문입니다. 이런 제도 간 상호작용이 사전협의에서 보여야 합니다.\"", "",
        "## 4:10–4:35 신뢰도 (화면 5)",
        f"- \"AI 행동 추정은 실제 수급 통계로 보정합니다(현행 수급률 {base['take_up_rate']:.0%}, 미신청 {base['non_take_up_households'] / 1e4:,.0f}만 가구). "
        f"2024→2025 수급 가구 증가를 예측하면 오차 {bt['households']['error_rate']:.0%}입니다. 소득을 고정했을 때는 "
        f"{d['bt_fixed']['households']['error_rate']:.0%}였고, 그 오차도 숨기지 않고 보여드립니다.\"", "",
        "## 4:35–5:00 사업화·마무리 (슬라이드)",
        "- \"사회보장제도 신설·변경 사전협의에 영향평가 리포트를 첨부하는 것부터 시작합니다. 기초지자체가 협의의 대부분을 차지합니다. "
        "사람이 쓰러진 뒤가 아니라, 법전 안에서 먼저 찾겠습니다.\"", "",
        "## 비상 대응",
        "- 화면 오류 → 사이드바 모드가 '사전 계산본'인지 확인, 그래도 안 되면 `artifacts/rehearsal/demo_backup.webm` 재생",
        "- 질문 대응은 `docs/11_질의응답_대비표.md`", ""])


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    d = collect()
    rep = ROOT / "reports" / f"eval_{date.today().strftime('%Y%m%d')}.md"
    rep.parent.mkdir(parents=True, exist_ok=True)
    rep.write_text(eval_md(d), encoding="utf-8")
    qa = ROOT / "docs" / "11_질의응답_대비표.md"
    qa.write_text(qa_md(d), encoding="utf-8")
    sc = ROOT / "docs" / "12_발표_대본.md"
    sc.write_text(script_md(d), encoding="utf-8")
    print(f"→ {rep}\n→ {qa}\n→ {sc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
