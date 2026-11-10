"""합성 인구 생성 — 문서 06 §1.

    uv run python -m welfaretwin.population.synth [--n 100000] [--seed 20261110]

1. IPF: 시도(17) × 가구주 연령(5세 15구간) × 가구원수(6) × 점유형태(4) × 소득구간(6) 셀 가중치를
   - 2025 인구총조사 시도×연령×가구원수(그대로)
   - 2020 인구총조사 시도×연령×점유형태, 시도×점유형태×가구원수(비율을 2025 가구 수에 적용)
   - 2025 가계금융복지조사 가구주연령그룹×소득구간, 가구원수그룹×소득구간(비율을 2025 가구 수에 적용)
   에 동시에 맞춘다.
2. 셀별 정수 표본 → 가구 생성. 가구원 구성·소득 배분·자산은 아래 ASSUMPTIONS 의 규칙(휴리스틱)으로.
3. data/processed/{households,persons}.parquet + population_meta.json (출처·IPF 오차·가정·검증)

개인정보 없음: 집계표만 사용한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from welfaretwin.population import sources as S
from welfaretwin.population.ipf import Marginal, integerize, ipf
from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
OUT = ROOT / "data" / "processed"

# 집계표가 없는 부분을 채우는 규칙. 결과 해석 시 반드시 함께 제시한다(meta 에 기록).
ASSUMPTIONS = {
    "income_within_band": ("kosis_cdf", "소득구간 안의 값은 KOSIS 전국 소득 P10~P90 경계값을 잇는 누적분포로 뽑음"),
    "income_tail_alpha": (2.0, "소득 P90 초과 꼬리는 파레토(α=2) — 통계 미연계 가정"),
    "networth_floor_manwon": (-5000, "순자산 최하단(P0) = −5천만원 가정, P10~P90 은 KOSIS 경계값"),
    "networth_tail_alpha": (2.0, "순자산 P90 초과 꼬리는 파레토(α=2.0) — KOSIS 순자산 10분위 평균(217,122만원)/P90(110,020만원)"
                                 "≈ α/(α−1) 에서 역산(DT_1HDAAD03, DT_1HDAAA21)"),
    "networth_income_corr": (0.6, "소득 순위와 순자산 순위의 상관(가우시안 코퓰라 ρ) — 0.4~0.6 중 소득 5분위별 평균 순자산(DT_1HDAAA10)과 ±11% 이내로 맞는 값 선택"),
    "spouse_prob": ({"<30": 0.45, "30-59": 0.8, "60-74": 0.8, "75+": 0.55},
                    "2인 이상 가구의 배우자 존재 확률(가구주 연령별) — 통계 미연계 휴리스틱"),
    "child_age_gap": ((25, 38), "자녀 나이 = 가구주 나이 − U(25, 38)"),
    "three_generation_prob": (0.1, "가구주 35~55세·4인 이상 가구 중 부모 1명 동거 확률"),
    "college_prob_19_24": (0.6, "19~24세 재학(대학) 확률"),
    "older_head_work_prob": (0.35, "19~64세 가구원이 없는 가구(고령 가구)에서 가구주가 일하는 확률 — 일하지 않으면 근로·사업소득 몫을 "
                                    "기타소득(연금·이전소득)으로 배분. 통계 미연계 가정(고령자 고용률로 대체 예정)"),
    "public_transfer_share": (0.5, "공적이전소득 중 '공적연금 등 소득평가액 반영분'으로 보는 비율(나머지는 기초연금 등 "
                                   "시뮬레이션 대상 급여로 보고 제외)"),
    "debt_sigma": (1.0, "부채 보유 가구의 부채액 로그정규 σ"),
    "housing_split": ({"own": (0.8, "실물자산의 80%를 거주 주택(주거용재산), 20%를 일반재산"),
                       "jeonse": (0.7, "금융자산의 70%를 전세보증금(주거용재산)으로, 실물자산은 전부 일반재산"),
                       "monthly_rent": (0.4, "금융자산의 40%(최대 5천만원)를 월세보증금, 실물자산은 전부 일반재산"),
                       "other": (0.0, "무상거주 — 실물자산은 전부 일반재산")},
                      "점유형태별 자산 배분"),
    "monthly_rent_base": ({1: 600_000, 2: 450_000, 3: 350_000, 4: 300_000},
                          "월세액 = 급지별 기준값 × 로그정규(σ=0.3) — 통계 미연계 휴리스틱"),
    "car_value": (0, "소득환산율 100% 자동차 없음(대부분 일반재산 환산 차량으로 보고 미반영)"),
    "obligor": (False, "부양의무자 기준(생계 예외·의료급여 부양능력) 해당 없음으로 가정 → 의료급여 대상 과대"),
    "pregnant": (False, "임산부 미생성"),
    "disability_flat": (True, "장애 유병률은 시도별 등록장애인 비율을 연령 구분 없이 적용"),
}


class PiecewiseCDF:
    """P0, P10..P90 경계값을 선형으로 잇고 P90 위는 파레토 꼬리인 누적분포."""

    def __init__(self, knots: list[float], alpha: float) -> None:
        self.x = np.asarray(knots, dtype=float)
        self.p = np.linspace(0.0, 0.9, len(knots))
        self.alpha = alpha

    def cdf(self, x: float) -> float:
        if x <= self.x[-1]:
            return float(np.interp(x, self.x, self.p))
        return float(1 - 0.1 * (self.x[-1] / x) ** self.alpha)

    def inv(self, u: float) -> float:
        if u <= 0.9:
            return float(np.interp(u, self.p, self.x))
        return float(self.x[-1] * (0.1 / (1 - u)) ** (1 / self.alpha))


def _age_group(age: int) -> str:
    return "<30" if age < 30 else "30-59" if age < 60 else "60-74" if age < 75 else "75+"


def build_weights() -> tuple[np.ndarray, dict, dict]:
    cube = S.households_sido_age_size()
    ten_age = S.tenure_sido_age()
    ten_size = S.tenure_sido_size()
    inc_age, inc_size = S.income_band_shares()
    c = cube.data                                             # (sido, age, size)
    n_ten, n_inc = len(S.TENURE), len(S.INCOME_BANDS)

    # 2020 점유형태 비율 × 2025 가구 수
    sa = c.sum(axis=2)                                        # (sido, age)
    p_ten_age = ten_age.data / np.maximum(ten_age.data.sum(axis=2, keepdims=True), 1)
    ss = c.sum(axis=1)                                        # (sido, size)
    p_ten_size = ten_size.data / np.maximum(ten_size.data.sum(axis=1, keepdims=True), 1)  # (sido, ten, size)
    # 가계금융복지조사 소득 비율 × 2025 가구 수(그룹별)
    age_g = np.array([c[:, S.AGE_GROUP_OF_BAND == g].sum() for g in range(S.N_AGE_GROUPS)])
    size_g = np.array([c[:, :, S.SIZE_GROUP == g].sum() for g in range(5)])
    t_inc_age = inc_age.data / inc_age.data.sum(axis=1, keepdims=True) * age_g[:, None]
    t_inc_size = inc_size.data / inc_size.data.sum(axis=1, keepdims=True) * size_g[:, None]

    seed = np.ones(c.shape + (n_ten, n_inc))
    seed[c == 0] = 0
    margs = [
        Marginal((0, 1, 2), c, "시도×연령×가구원수(2025)"),
        Marginal((0, 1, 3), sa[:, :, None] * p_ten_age, "시도×연령×점유형태(2020 비율)"),
        Marginal((0, 3, 2), ss[:, None, :] * p_ten_size, "시도×점유형태×가구원수(2020 비율)"),
        Marginal((1, 4), t_inc_age, "연령그룹×소득구간(2025 가계금융)", groups={1: S.AGE_GROUP_OF_BAND}),
        Marginal((2, 4), t_inc_size, "가구원수그룹×소득구간(2025 가계금융)", groups={2: S.SIZE_GROUP}),
    ]
    res = ipf(seed, margs, tol=1e-4, max_iter=500)
    sources = {"cube": cube.source, "tenure_age": ten_age.source, "tenure_size": ten_size.source,
               "income": inc_age.source}
    info = {"iterations": res.iterations, "converged": res.converged, "max_rel_error": res.max_rel_error,
            "errors": res.errors, "total_households": float(c.sum())}
    return res.weights, sources, info


def _person(age, sex, disability, is_student, earned=0, business=0, other=0) -> dict:
    return {"age": int(age), "sex": sex, "disability": disability, "is_student": bool(is_student),
            "employed": earned > 0, "pregnant": False, "earned_income": int(earned),
            "business_income": int(business), "other_income": int(other)}


def generate(n: int = 100_000, seed: int = 20261110) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    rng = np.random.default_rng(seed)
    w, sources, ipf_info = build_weights()
    counts = integerize(w, n, rng)
    src_means = S.income_source_means()            # (연령그룹5, [경상, 근로, 사업, 재산, 공적이전, 사적이전])
    assets_q = S.assets_by_income_quintile()       # (5, [자산, 금융, 실물, 부채, 경상소득, 부채보유%])
    pct = S.income_net_worth_percentiles()
    dis = S.disability_prevalence()
    q_cut = pct.data[1][[1, 3, 5, 7]]              # 소득 P20, P40, P60, P80 (만원)
    weight = ipf_info["total_households"] / n
    inc_cdf = PiecewiseCDF([0.0, *pct.data[1]], ASSUMPTIONS["income_tail_alpha"][0])
    nw_cdf = PiecewiseCDF([ASSUMPTIONS["networth_floor_manwon"][0], *pct.data[0]],
                          ASSUMPTIONS["networth_tail_alpha"][0])
    rho = ASSUMPTIONS["networth_income_corr"][0]
    normal = NormalDist()

    hh_rows, p_rows = [], []
    k = 0
    for idx in zip(*np.nonzero(counts)):
        si, ai, zi, ti, ii = idx
        for _ in range(counts[idx]):
            k += 1
            hh_id = f"H{k:06d}"
            sido = S.SIDO_NAMES[si]
            size = S.SIZES[zi]
            tenure = S.TENURE[ti]
            lo, hi = S.AGE_BANDS[ai]
            head_age = int(rng.integers(max(lo, 18), hi + 1))
            # --- 가구원
            sev, mild = dis.data[si]
            def disab():  # noqa: E306
                u = rng.random()
                return "severe" if u < sev else "mild" if u < sev + mild else "none"
            members = []
            has_spouse = size >= 2 and rng.random() < ASSUMPTIONS["spouse_prob"][0][_age_group(head_age)]
            head_sex = ("M" if rng.random() < 0.7 else "F") if has_spouse else ("M" if rng.random() < 0.5 else "F")
            members.append([head_age, head_sex, "head"])
            if has_spouse:
                members.append([int(np.clip(head_age + rng.normal(-2, 3), 18, 99)),
                                "F" if head_sex == "M" else "M", "spouse"])
            if size - len(members) >= 2 and 35 <= head_age <= 55 \
                    and rng.random() < ASSUMPTIONS["three_generation_prob"][0]:
                members.append([int(min(99, head_age + rng.integers(25, 33))), "F", "parent"])
            g0, g1 = ASSUMPTIONS["child_age_gap"][0]
            while len(members) < size:
                members.append([int(max(0, head_age - rng.integers(g0, g1 + 1))),
                                "M" if rng.random() < 0.5 else "F", "child"])
            single_parent = not has_spouse and any(m[0] < 18 and m[2] == "child" for m in members)
            # --- 소득(만원/년 → 원/월)
            blo, bhi = S.INCOME_BANDS[ii]
            u_lo = inc_cdf.cdf(blo)
            u_hi = 0.9995 if ii == len(S.INCOME_BANDS) - 1 else inc_cdf.cdf(bhi)
            u_inc = float(rng.uniform(u_lo, u_hi))
            inc = max(50.0, inc_cdf.inv(u_inc))
            g = S.AGE_GROUP_OF_BAND[ai]
            tot, work, biz, prop, pub, priv = src_means.data[g]
            share_work, share_biz = work / tot, biz / tot
            share_other = (prop + priv + pub * ASSUMPTIONS["public_transfer_share"][0]) / tot
            monthly = inc * 10_000 / 12
            earned_total = monthly * share_work
            earners = [i for i, m in enumerate(members) if 19 <= m[0] <= 64]   # 근로능력 연령(사업안내 p.96: 18~64세)
            if not earners:   # 고령 가구: 가구주가 일할 때만 근로소득, 아니면 기타소득으로
                if rng.random() < ASSUMPTIONS["older_head_work_prob"][0]:
                    earners = [0]
                else:   # 일하지 않는 고령 가구주: 근로·사업소득 몫을 기타소득(연금·이전소득)으로
                    share_other += share_work + share_biz
                    share_biz = 0.0
                    earned_total = 0.0
                    earners = [0]
            wts = np.array([1.0 if members[i][2] == "head" else 0.5 if members[i][2] == "spouse" else 0.6
                            for i in earners])
            alloc = dict(zip(earners, earned_total * wts / wts.sum()))
            persons = []
            for i, (age, sex, role) in enumerate(members):
                student = (7 <= age <= 18) or (19 <= age <= 24 and rng.random() < ASSUMPTIONS["college_prob_19_24"][0])
                persons.append(_person(age, sex, disab(), student,
                                       earned=round(alloc.get(i, 0), -3),
                                       business=round(monthly * share_biz, -3) if i == 0 else 0,
                                       other=round(monthly * share_other, -3) if i == 0 else 0))
            # --- 자산(만원 → 원)
            q = int(np.searchsorted(q_cut, inc, side="right"))
            a_tot, a_fin, a_real, a_debt, _, debt_pct = assets_q.data[q]
            z = rho * normal.inv_cdf(min(max(u_inc, 1e-6), 1 - 1e-6)) + np.sqrt(1 - rho ** 2) * rng.normal()
            net_worth = nw_cdf.inv(normal.cdf(z))
            has_debt = rng.random() < debt_pct / 100
            dsig = ASSUMPTIONS["debt_sigma"][0]
            debts = (a_debt / (debt_pct / 100)) * np.exp(rng.normal(-dsig ** 2 / 2, dsig)) if has_debt else 0.0
            total_assets = max(0.0, net_worth + debts)
            debts = min(debts, total_assets - net_worth) if total_assets > 0 else max(0.0, -net_worth)
            real = total_assets * a_real / a_tot
            fin = total_assets - real
            rent = 0.0
            if tenure == "own":
                housing, general, financial = real * 0.8, real * 0.2, fin
            elif tenure == "jeonse":
                housing, general, financial = fin * 0.7, real, fin * 0.3
            elif tenure == "monthly_rent":
                housing = min(fin * 0.4, 5_000)
                general, financial = real, fin - housing
                zone = 1 if sido.startswith("서울") else 2 if sido.startswith(("경기", "인천")) \
                    else 3 if sido.endswith("광역시") or sido.startswith("세종") else 4
                rent = ASSUMPTIONS["monthly_rent_base"][0][zone] * float(np.exp(rng.normal(-0.045, 0.3)))
            else:
                housing, general, financial = 0.0, real, fin
            won = lambda x: int(round(max(0.0, x) * 10_000, -4))  # noqa: E731
            hh_rows.append({
                "hh_id": hh_id, "region_sido": sido, "region_sigungu": None, "housing": tenure,
                "rent_monthly": int(round(rent, -3)), "assets_housing": won(housing),
                "assets_general": won(general), "assets_financial": won(financial),
                "car_value": 0, "debts": won(debts), "single_parent": single_parent,
                "obligor_over_limit": False, "obligor_medical_capable": False, "weight": weight,
                "head_age_band": f"{lo}-{hi}", "income_band": ii, "income_annual_manwon": round(inc, 1),
            })
            for i, p in enumerate(persons):
                p_rows.append({"hh_id": hh_id, "idx": i, **p})
    hh, persons = pd.DataFrame(hh_rows), pd.DataFrame(p_rows)
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_households": len(hh), "n_persons": len(persons), "seed": seed, "weight": weight,
        "sources": sources | {"income_sources": src_means.source, "assets": assets_q.source,
                              "percentiles": pct.source, "disability": dis.source},
        "ipf": ipf_info,
        "assumptions": {k: v[1] for k, v in ASSUMPTIONS.items()},
        "validation": validate(hh, persons, w, pct.data),
    }
    return hh, persons, meta


def validate(hh: pd.DataFrame, persons: pd.DataFrame, w: np.ndarray, pct: np.ndarray) -> dict:
    """주변분포 비교(문서 06 §1.2: ±2%p) + 소득·순자산 분위 경계값 비교."""
    def share_diff(sim: pd.Series, target: np.ndarray, labels) -> dict:
        s = sim.reindex(labels).fillna(0) / sim.sum()
        t = target / target.sum()
        return {"max_abs_pp": float(np.max(np.abs(s.values - t)) * 100),
                "by": {str(lab): [round(float(a) * 100, 2), round(float(b) * 100, 2)]
                       for lab, a, b in zip(labels, s.values, t)}}
    sizes = persons.groupby("hh_id").size().clip(upper=6)
    out = {
        "sido": share_diff(hh["region_sido"].value_counts(), w.sum(axis=(1, 2, 3, 4)), S.SIDO_NAMES),
        "size": share_diff(sizes.value_counts(), w.sum(axis=(0, 1, 3, 4)), S.SIZES),
        "tenure": share_diff(hh["housing"].value_counts(), w.sum(axis=(0, 1, 2, 4)), S.TENURE),
        "head_age_band": share_diff(hh["head_age_band"].value_counts(), w.sum(axis=(0, 2, 3, 4)),
                                    [f"{lo}-{hi}" for lo, hi in S.AGE_BANDS]),
    }
    net = (hh["assets_housing"] + hh["assets_general"] + hh["assets_financial"] - hh["debts"]) / 10_000
    qs = np.arange(10, 100, 10)
    out["income_percentiles_manwon"] = {"synthetic": np.percentile(hh["income_annual_manwon"], qs).round().tolist(),
                                        "kosis": pct[1].tolist()}
    out["net_worth_percentiles_manwon"] = {"synthetic": np.percentile(net, qs).round().tolist(),
                                           "kosis": pct[0].tolist()}
    out["persons_per_household"] = float(len(persons) / len(hh))
    out["disabled_share_pct"] = float((persons["disability"] != "none").mean() * 100)
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="합성 인구 생성(IPF)")
    ap.add_argument("--n", type=int, default=100_000)
    ap.add_argument("--seed", type=int, default=20261110)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    hh, persons, meta = generate(args.n, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    hh.to_parquet(args.out / "households.parquet", index=False)
    persons.to_parquet(args.out / "persons.parquet", index=False)
    (args.out / "population_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    v = meta["validation"]
    print(f"{meta['n_households']:,}가구 / {meta['n_persons']:,}명 (가중치 {meta['weight']:.1f}), "
          f"IPF {meta['ipf']['iterations']}회 수렴={meta['ipf']['converged']} 최대오차 {meta['ipf']['max_rel_error']:.2e}")
    for key in ("sido", "size", "tenure", "head_age_band"):
        print(f"  {key}: 최대 차이 {v[key]['max_abs_pp']:.2f}%p")
    print("  소득 P10..P90(만원) 합성", v["income_percentiles_manwon"]["synthetic"])
    print("                    KOSIS", v["income_percentiles_manwon"]["kosis"])
    print("  순자산 P10..P90 합성", v["net_worth_percentiles_manwon"]["synthetic"])
    print("                KOSIS", v["net_worth_percentiles_manwon"]["kosis"])
    print(f"  가구당 인원 {v['persons_per_household']:.2f}, 장애인 비율 {v['disabled_share_pct']:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
