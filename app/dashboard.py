"""복지트윈 대시보드 — 문서 08 §2, 문서 10.

    uv run streamlit run app/dashboard.py

화면 1 복지 법전 → 코드 / 2 사각지대 / 3 급여 절벽 / 4 정책 시뮬레이터 + 레드팀 / 5 백테스트
- 모드: '사전 계산본'(artifacts/demo, 네트워크 불필요) / '실시간 실행'(Z3·평가기·LLM 캐시)
- URL ?page=1..5 로 화면 지정(리허설 자동 점검용)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from welfaretwin.model import Household
from welfaretwin.rules.engine import evaluate, load_ruleset

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "artifacts" / "demo"

# 제도별 고정 범주색(데이터시각화 기준 팔레트 1~7번 슬롯, 순서 고정) + 상태색(위험)
COLORS = {"NBLS_LIVELIHOOD": "#2a78d6", "NBLS_HOUSING": "#eb6834", "NBLS_EDUCATION": "#1baf7a",
          "BASIC_PENSION": "#eda100", "EITC": "#e87ba4", "ENERGY_VOUCHER": "#008300", "LOCAL": "#4a3aa7"}
INCOME_GRAY = "#c3c2b7"
CRITICAL = "#d03b3b"
NAMES = {"NBLS_LIVELIHOOD": "생계급여", "NBLS_HOUSING": "주거급여", "NBLS_EDUCATION": "교육급여",
         "BASIC_PENSION": "기초연금", "EITC": "근로장려금", "ENERGY_VOUCHER": "에너지바우처", "LOCAL": "지자체 사업"}
PAGES = ["1. 복지 법전 → 코드", "2. 사각지대 탐지", "3. 급여 절벽", "4. 정책 시뮬레이터 + 레드팀", "5. 백테스트"]

st.set_page_config(page_title="복지트윈 WelfareTwin", layout="wide")


@st.cache_data
def load_json(name: str):
    path = DEMO / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@st.cache_resource
def ruleset():
    return load_ruleset()


def won(v) -> str:
    return f"{round(float(v)):,}원"


def assumptions_box(lines: list[str], title: str = "단순화 가정") -> None:
    if st.session_state.get("show_assumptions"):
        with st.expander(title, expanded=False):
            for line in lines:
                st.markdown(f"- {line}")


# ------------------------------------------------------------------ 사이드바
qp = st.query_params.get("page")
default_page = int(qp) - 1 if qp and qp.isdigit() and 1 <= int(qp) <= 5 else 0
with st.sidebar:
    st.title("복지트윈")
    st.caption("사회보장제도 Rules-as-Code · 형식검증 · 합성 인구 시뮬레이션")
    page = st.radio("화면", PAGES, index=default_page)
    mode = st.radio("모드", ["사전 계산본(오프라인)", "실시간 실행"], index=0,
                    help="사전 계산본은 artifacts/demo 결과를 즉시 보여줍니다. 실시간은 Z3·평가기를 직접 실행합니다.")
    st.session_state["show_assumptions"] = st.toggle("단순화 가정 보기", value=False)
    live = mode.startswith("실시간")
    st.divider()
    st.caption("규칙 7종 + 지자체 12건 · 합성 가구 10만 · 판정은 코드, LLM 은 추출·행동 추정만")


# ------------------------------------------------------------------ 화면 1
def page_rules():
    st.header("복지 법전 → 코드")
    st.caption("지자체 사업 원문을 LLM 이 두 번(프롬프트 A·B) 규칙으로 옮기고, Z3 가 두 초안의 판정이 같은지 증명합니다.")
    rules = load_json("m4_rules.json") or []
    if not rules:
        st.warning("m4_rules.json 이 없습니다 — python -m welfaretwin.demo_bundle")
        return
    agree = sum(1 for r in rules if (r.get("crosscheck") or {}).get("agree"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("추출 사업", f"{len(rules)}건")
    c2.metric("A·B 판정 동치 증명", f"{agree}건")
    c3.metric("인용 불일치(환각)", f"{sum(len(r['A']['quote_issues']) for r in rules)}건")
    c4.metric("LLM 비용", f"${sum(r['llm_cost_usd'] for r in rules):.2f}")
    labels = [f"{r['name']} ({r['text']['region'] if r['text'] else ''})" for r in rules]
    default = next((k for k, r in enumerate(rules) if r["serv_id"] == "WLF00005691"), 0)   # 구미 청년월세(발표용)
    i = st.selectbox("사업 선택", range(len(rules)), index=default, format_func=lambda k: labels[k])
    r = rules[i]
    left, right = st.columns(2)
    with left:
        st.subheader("원문")
        if r["text"]:
            st.markdown("**지원대상**")
            st.text(r["text"]["target"])
            st.markdown("**선정기준**")
            st.text(r["text"]["criteria"])
            st.markdown("**지원내용**")
            st.text(r["text"]["benefit"])
        else:
            st.info("원문이 번들에 없습니다.")
    with right:
        st.subheader("추출된 규칙(승인본)")
        rule = r["rule_approved"]
        cc = r.get("crosscheck") or {}
        el = cc.get("eligibility", {})
        badge = {True: "✅ A·B 판정 동치(Z3 증명)", False: "⚠️ A·B 판정 불일치 — 반례 확인 후 사람 검수",
                 None: "⏳ 미확정"}.get(el.get("equivalent"), "-")
        st.markdown(f"{badge} · 인용 검사 {'✅' if not r['A']['quote_issues'] else '⚠️'} · 자동 테스트 {r['n_tests']}건")
        if rule:
            rows = [{"조건": c.get("label", ""), "식": c["expr"], "근거 인용": c.get("quote", ""),
                     "불확실": "예" if c.get("uncertain") else ""} for c in rule["eligibility"]["all"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            period = "monthly" if "monthly" in rule["benefit"] else "annual"
            st.markdown(f"**급여({'월' if period == 'monthly' else '연'})** `{rule['benefit'][period]}` — "
                        f"{rule['benefit'].get('quote', '')}")
            if rule.get("not_modeled"):
                with st.expander(f"규칙으로 표현하지 못한 조건 {len(rule['not_modeled'])}개(담당자 확인 필요)"):
                    for n in rule["not_modeled"]:
                        st.markdown(f"- {n['quote']} — _{n['reason']}_")
            edits = (rule.get("provenance") or {}).get("human_edits")
            if edits:
                st.info("사람 검수 수정: " + " / ".join(edits))
        if el.get("equivalent") is False:
            st.markdown("**두 초안이 갈리는 가구(Z3 반례)**")
            st.write(el["household_text"])
            st.write(f"A: {'자격' if el['A_eligible'] else '탈락'} / B: {'자격' if el['B_eligible'] else '탈락'}")
        if r.get("rule_B"):
            with st.expander("초안 B 조건"):
                for c in r["rule_B"]["eligibility"]["all"]:
                    st.markdown(f"- {c.get('label', '')}: `{c['expr']}`")
    assumptions_box(rule.get("assumptions", []) if rule else [])


# ------------------------------------------------------------------ 화면 2
def page_blindspot():
    st.header("사각지대 탐지")
    st.caption("빈곤선(소득인정액 ≤ 기준중위소득 40%) 이하인데 생계급여·기초연금·근로장려금에서 모두 탈락하는 가구를 "
               "샘플링이 아니라 Z3 로 찾습니다.")
    scale = load_json("blindspot_scale.json")
    if scale:
        c1, c2, c3 = st.columns(3)
        c1.metric("합성 인구 중 빈곤 가구", f"{scale['poor_households']:,.0f}가구")
        c2.metric("그중 소득보장 3종 모두 탈락", f"{scale['blindspot_households']:,.0f}가구")
        c3.metric("비율", f"{scale['blindspot_households'] / scale['poor_households']:.1%}")
    if st.button("사각지대 찾기", type="primary"):
        t0 = time.perf_counter()
        if live:
            from welfaretwin.verify import queries as q
            from welfaretwin.verify.explain import explain
            res = q.blindspot(ruleset(), n=6)
            items = explain(res, ruleset())
        else:
            items = (load_json("verify_blindspot.json") or {}).get("items", [])
        st.session_state["blind_items"] = items
        st.session_state["blind_sec"] = time.perf_counter() - t0
    items = st.session_state.get("blind_items")
    if items:
        st.caption(f"{len(items)}개 유형 · {st.session_state['blind_sec']:.1f}초")
        cols = st.columns(2)
        for k, it in enumerate(items):
            with cols[k % 2].container(border=True):
                st.markdown(f"**{it['title']}**")
                st.write(it["text"].split("\n")[0].split("] ", 1)[-1])
                f = it["facts"]
                st.markdown(f"소득인정액 **{won(f['income_recognized'])}** (기준중위소득의 {f['median_pct']}%)")
                for prog in it["programs"]:
                    for rr in prog["reasons"]:
                        st.markdown(f"- **{prog['name']}** 탈락: {rr['label']}"
                                    + (f" — {rr['detail']}" if rr["detail"] else "")
                                    + (f"  \n  <span style='font-size:0.8em'>근거: {rr['source']}</span>"
                                       if rr["source"] else ""), unsafe_allow_html=True)
    assumptions_box(ruleset().libraries["NBLS_INCOME"].assumptions, "소득인정액 단순화 가정")


# ------------------------------------------------------------------ 화면 3
PRESETS = {
    "알바 시작한 장애 청년 1인가구(대구, 월세)": dict(
        region_sido="대구광역시", housing="monthly_rent", rent_monthly=300_000, assets_housing=5_000_000,
        members=[{"age": 24, "disability": "mild"}]),
    "노인 부부(경북 의성, 자가)": dict(
        region_sido="경상북도", region_sigungu="의성군", housing="own", assets_housing=80_000_000,
        members=[{"age": 72}, {"age": 70, "sex": "F"}]),
    "한부모 + 중학생 자녀(서울, 월세)": dict(
        region_sido="서울특별시", housing="monthly_rent", rent_monthly=450_000, assets_housing=10_000_000,
        single_parent=True, members=[{"age": 40, "sex": "F"}, {"age": 14, "is_student": True}]),
    "청년 근로자 1인(경북 영천, 월세)": dict(
        region_sido="경상북도", region_sigungu="영천시", housing="monthly_rent", rent_monthly=350_000,
        assets_housing=5_000_000, members=[{"age": 30}]),
}


def _group(pid: str) -> str:
    return pid if pid in COLORS else "LOCAL"


@st.cache_data(show_spinner=False)
def sweep(preset: str, max_income: int, step: int) -> pd.DataFrame:
    rs = ruleset()
    base = PRESETS[preset]
    rows = []
    for inc in range(0, max_income + 1, step):
        members = [dict(m) for m in base["members"]]
        members[0]["earned_income"] = inc
        members[0]["employed"] = inc > 0
        hh = Household(**{**base, "members": members})
        row = {"earned": inc, "own_income": sum(m.earned_income + m.business_income + m.other_income for m in hh.members)}
        for d in evaluate(hh, rs):
            g = _group(d.program_id)
            row[g] = row.get(g, 0) + d.benefit_monthly
        rows.append(row)
    df = pd.DataFrame(rows).fillna(0)
    groups = [g for g in COLORS if g in df.columns]
    df["disposable"] = df["own_income"] + df[groups].sum(axis=1)
    return df


def page_cliff():
    st.header("급여 절벽")
    st.caption("가구주 근로소득을 1만원씩 올리며 모든 제도를 평가합니다. 소득이 늘었는데 가처분소득이 줄면 절벽입니다.")
    preset = st.selectbox("가구 프리셋", list(PRESETS))
    df = sweep(preset, 3_500_000, 10_000)
    x = st.slider("가구주 월 근로소득(원)", 0, 3_500_000, 900_000, 10_000)
    groups = [g for g in COLORS if g in df.columns and df[g].sum() > 0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["earned"], y=df["own_income"], name="본인 소득(근로·기타)", stackgroup="one",
                             line=dict(width=0.5, color=INCOME_GRAY), fillcolor=INCOME_GRAY,
                             hovertemplate="%{y:,.0f}원"))
    for g in groups:
        fig.add_trace(go.Scatter(x=df["earned"], y=df[g], name=NAMES[g], stackgroup="one",
                                 line=dict(width=0.5, color=COLORS[g]), fillcolor=COLORS[g],
                                 hovertemplate="%{y:,.0f}원"))
    fig.add_trace(go.Scatter(x=df["earned"], y=df["disposable"], name="가처분소득", mode="lines",
                             line=dict(width=2, color="#0b0b0b"), hovertemplate="%{y:,.0f}원"))
    drops = df[df["disposable"].diff(-1) > 0]
    for _, r in drops.iterrows():
        loss = r["disposable"] - df.loc[df["earned"] == r["earned"] + 10_000, "disposable"].iloc[0]
        if loss >= 5_000:
            fig.add_trace(go.Scatter(x=[r["earned"]], y=[r["disposable"]], mode="markers+text", showlegend=False,
                                     marker=dict(size=10, color=CRITICAL, symbol="triangle-down",
                                                 line=dict(width=2, color="#fcfcfb")),
                                     text=[f"⚠ 절벽 −{loss:,.0f}원"], textposition="top center",
                                     textfont=dict(color="#52514e"),
                                     hovertemplate=f"절벽: 근로소득 +1만원 → 가처분 −{loss:,.0f}원<extra></extra>"))
    fig.add_vline(x=x, line_width=1, line_dash="dot", line_color="#52514e")
    fig.update_layout(height=470, hovermode="x unified", template="plotly_white",
                      xaxis_title="가구주 월 근로소득(원)", yaxis_title="원/월",
                      legend=dict(orientation="h", y=-0.2), margin=dict(t=20))
    fig.update_xaxes(tickformat=",")
    fig.update_yaxes(tickformat=",", gridcolor="#ecebe7")
    st.plotly_chart(fig, use_container_width=True)
    cur = df[df["earned"] == x].iloc[0]
    cols = st.columns(2 + len(groups))
    cols[0].metric("가처분소득", won(cur["disposable"]))
    cols[1].metric("본인 소득", won(cur["own_income"]))
    for c, g in zip(cols[2:], groups):
        c.metric(NAMES[g], won(cur[g]))
    with st.expander("표로 보기"):
        st.dataframe(df.rename(columns={**NAMES, "earned": "근로소득", "own_income": "본인 소득", "disposable": "가처분"}),
                     hide_index=True)
    st.subheader("Z3 가 찾은 가장 큰 절벽(전 제도·전 가구 유형)")
    cl = load_json("verify_cliff.json") or {}
    for it in cl.get("items", [])[:3]:
        with st.container(border=True):
            st.text(it["text"].split("\n    ·")[0])


# ------------------------------------------------------------------ 화면 4
def page_simulator():
    st.header("정책 시뮬레이터 + 레드팀")
    tab1, tab2 = st.tabs(["정책 시나리오 영향평가", "사업안 레드팀(30초)"])
    with tab1:
        st.caption("LLM 페르소나 239개의 신청 확률을 2025년 실제 수급 가구 수로 보정(공통 절편)한 뒤 시나리오를 비교합니다.")
        sc = load_json("scenario_demo.json")
        if live:
            ratio = st.slider("생계급여 선정기준(기준중위소득 비율)", 0.30, 0.40, 0.35, 0.01)
            beh = st.radio("신청 방식", ["S0 현행 신청주의", "S1 자동 안내 + 간편 신청"], horizontal=True)
            if st.button("영향평가 실행(표본 2만 가구)", type="primary"):
                from welfaretwin.sim.scenario import PolicyChange, compare
                t0 = time.perf_counter()
                with st.spinner("평가 중…"):
                    sc = compare([(PolicyChange("현행"), "S0"),
                                  (PolicyChange(f"생계 {ratio:.0%}", {"selection_ratio_livelihood": ratio}), beh[:2])],
                                 sample=20_000)
                st.caption(f"{time.perf_counter() - t0:.0f}초")
                st.session_state["sc_live"] = sc
            sc = st.session_state.get("sc_live", sc)
        if not sc:
            st.warning("scenario_demo.json 이 없습니다.")
        else:
            cal = sc["calibration"]
            st.markdown(f"보정 목표: {cal['target']['year']}년 실제 수급 **{cal['target']['households']:,.0f}가구** "
                        f"({cal['target']['source']}) · 공통 절편 b = {cal['intercept_b']:.2f}")
            names = [s["name"] for s in sc["scenarios"]]
            k = st.selectbox("시나리오", range(len(names)), index=len(names) - 1, format_func=lambda i: names[i])
            m, d = sc["scenarios"][k]["metrics"], sc["scenarios"][k]["delta_vs_base"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("기대 수급 가구", f"{m['expected_recipient_households']:,.0f}",
                      f"{d['expected_recipient_households']:+,.0f}")
            c2.metric("미신청 사각지대", f"{m['non_take_up_households']:,.0f}", f"{d['non_take_up_households']:+,.0f}",
                      delta_color="inverse")
            c3.metric("월 재정(생계·주거·교육)", f"{m['monthly_budget_won'] / 1e8:,.0f}억원",
                      f"{d['monthly_budget_won'] / 1e8:+,.0f}억원", delta_color="off")
            c4.metric("생계급여 자격 가구", f"{m['livelihood_eligible_households']:,.0f}",
                      f"{d['livelihood_eligible_households']:+,.0f}")
            fig = go.Figure(go.Bar(x=names, y=[s["metrics"]["expected_recipient_households"] for s in sc["scenarios"]],
                                   marker=dict(color=COLORS["NBLS_LIVELIHOOD"], cornerradius=4),
                                   text=[f"{s['metrics']['expected_recipient_households'] / 1e4:,.0f}만" for s in sc["scenarios"]],
                                   textposition="outside", hovertemplate="%{x}<br>%{y:,.0f}가구<extra></extra>"))
            fig.update_layout(height=320, template="plotly_white", yaxis_title="기대 수급 가구", margin=dict(t=20),
                              title=None)
            fig.update_yaxes(tickformat=",", gridcolor="#ecebe7")
            st.plotly_chart(fig, use_container_width=True)
            ch = sc["scenarios"][k].get("changes_vs_base")
            if ch:
                a, b = st.columns(2)
                a.markdown(f"**새로 포함** {ch['newly_included']['households']:,.0f}가구")
                a.dataframe(pd.DataFrame(ch["newly_included"]["top_types"]), hide_index=True)
                b.markdown(f"**새로 탈락** {ch['newly_excluded']['households']:,.0f}가구")
                b.dataframe(pd.DataFrame(ch["newly_excluded"]["top_types"]), hide_index=True)
            assumptions_box(["LLM 은 신청 확률의 방향·상대 크기만, 수준은 실제 수급 통계로 보정",
                             "자격 판정은 기초생활보장 4종 기준, 재정은 생계·주거·교육 현금급여 합",
                             "합성 인구 휴리스틱(가구원 구성·자산 배분 등)은 population_meta.json 참조"])
    with tab2:
        from welfaretwin.redteam.report import PRESETS as RT_PRESETS
        text = st.text_area("사업안(자연어)", RT_PRESETS["heating"], height=80)
        if st.button("레드팀 점검 실행", type="primary"):
            t0 = time.perf_counter()
            if live or text.strip() != RT_PRESETS["heating"]:
                from welfaretwin.redteam.agents import run
                from welfaretwin.redteam.report import render
                with st.spinner("규칙 추출 → 영향·배제·절벽·허점 점검 중…"):
                    md = render(run(text, sample=20_000))
            else:
                md = (DEMO / "redteam_heating.md").read_text(encoding="utf-8")
            st.session_state["rt_md"] = md
            st.session_state["rt_sec"] = time.perf_counter() - t0
        if st.session_state.get("rt_md"):
            st.caption(f"화면 처리 {st.session_state['rt_sec']:.1f}초 (사전 계산본은 리포트 안의 실제 처리 시간 참조)")
            with st.container(border=True):
                st.markdown(st.session_state["rt_md"].replace("\n", "  \n"))


# ------------------------------------------------------------------ 화면 5
def page_backtest():
    st.header("백테스트")
    bt = load_json("backtest_B2_2024_2025.json")
    if not bt:
        st.warning("백테스트 결과가 없습니다.")
        return
    st.caption(bt["description"] + " — 오차를 숨기지 않고 그대로 공개합니다.")
    series = pd.read_csv(DEMO / "nbls_series.csv") if (DEMO / "nbls_series.csv").exists() else None
    fig = go.Figure()
    if series is not None:
        s = series[series["year"] >= 2015]
        fig.add_trace(go.Scatter(x=s["year"], y=s["합계"], name="실제 수급 가구(e-나라지표)", mode="lines+markers",
                                 line=dict(width=2, color=COLORS["NBLS_LIVELIHOOD"]), marker=dict(size=8),
                                 hovertemplate="%{x}년 %{y:,.0f}가구<extra></extra>"))
    by, ty = str(bt["base_year"]), str(bt["target_year"])
    pred = [bt["actual"][by]["households"], bt["actual"][by]["households"] + bt["households"]["predicted_increase"]]
    fig.add_trace(go.Scatter(x=[int(by), int(ty)], y=pred, name="시뮬레이션 예측(기준값 + 소득·자산 증가)", mode="lines+markers",
                             line=dict(width=2, dash="dash", color=COLORS["NBLS_HOUSING"]), marker=dict(size=9),
                             hovertemplate="%{x}년 예측 %{y:,.0f}가구<extra></extra>"))
    fig.update_layout(height=380, template="plotly_white", yaxis_title="수급 가구", margin=dict(t=20),
                      legend=dict(orientation="h", y=-0.2))
    fig.update_yaxes(tickformat=",", gridcolor="#ecebe7")
    fixed = load_json("backtest_B2_2024_2025_fixedpop.json")
    if not fixed:
        st.plotly_chart(fig, use_container_width=True, key="bt1")
    if fixed:
        fy = str(fixed["target_year"])
        fig.add_trace(go.Scatter(x=[int(by), int(fy)],
                                 y=[fixed["actual"][by]["households"],
                                    fixed["actual"][by]["households"] + fixed["households"]["predicted_increase"]],
                                 name="비교: 인구 소득·자산 고정", mode="lines+markers",
                                 line=dict(width=2, dash="dot", color="#8b8a83"), marker=dict(size=7),
                                 hovertemplate="%{x}년 (고정) %{y:,.0f}가구<extra></extra>"))
        st.plotly_chart(fig, use_container_width=True, key="bt2")
    h, p = bt["households"], bt["persons"]
    c1, c2, c3 = st.columns(3)
    c1.metric("예측 증가(가구)", f"{h['predicted_increase']:+,.0f}")
    c2.metric("실제 증가(가구)", f"{h['actual_increase']:+,.0f}")
    ok = h["error_rate"] <= 0.10
    c3.metric("오차율(가구)", f"{h['error_rate']:.0%}")
    c3.markdown("✅ 목표 ±10% 달성" if ok else "⚠️ **목표 ±10% 미달**")
    st.caption(f"인원 기준: 예측 {p['predicted_increase']:+,.0f} / 실제 {p['actual_increase']:+,.0f} → 오차율 {p['error_rate']:.0%}"
               + (f" · 비교(인구 고정) 가구 오차율 {fixed['households']['error_rate']:.0%}" if fixed else ""))
    if not ok:
        st.error("⚠ 목표 미달 — 원인: 합성 인구 소득을 고정해 소득 증가에 따른 탈락이 빠짐(과대 추정). "
                 "2단계: 연도별 소득 분포(가계금융복지조사) 갱신, 가명정보 결합으로 정밀화.")
    st.markdown("**한계(발표에서 명시)**")
    for line in bt["limitations"]:
        st.markdown(f"- {line}")
    with st.expander("연도별 기준값 출처"):
        for y, src in bt["sources"].items():
            for s in src:
                st.markdown(f"- {y} `{s['param']}`: {s['doc']} ({s.get('url') or ''})")


{PAGES[0]: page_rules, PAGES[1]: page_blindspot, PAGES[2]: page_cliff,
 PAGES[3]: page_simulator, PAGES[4]: page_backtest}[page]()
