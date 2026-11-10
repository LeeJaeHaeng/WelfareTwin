"""페르소나 클러스터 — 문서 06 §2.1.

가구 10만 개에 LLM 을 직접 부르지 않고, 가구를 층화해 약 200개 클러스터로 묶는다.
층화 기준: 가구주 연령(6) × 가구원수(1·2·3·4+) × 소득 5분위 × 장애인 유무 → 비어 있지 않은 셀이 클러스터.
k-means 대신 층화를 쓰는 이유: 결정적이고, 클러스터 의미를 사람이 바로 읽을 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

AGE_GROUPS = [(18, 29, "20대 이하"), (30, 39, "30대"), (40, 49, "40대"), (50, 59, "50대"),
              (60, 69, "60대"), (70, 120, "70세 이상")]
SIZE_GROUPS = {1: "1인", 2: "2인", 3: "3인", 4: "4인 이상"}
INCOME_Q_CUTS = [2550, 4630, 7093, 10918]          # KOSIS 가구소득 P20·P40·P60·P80(만원/년, DT_1HDAAA21)
METRO = ("서울", "경기", "인천")


@dataclass
class Persona:
    persona_id: str
    age_group: str
    size_group: str
    income_quintile: int
    has_disabled: bool
    n_households: int
    weight: float
    profile: dict

    def describe(self) -> str:
        p = self.profile
        parts = [f"가구주 {self.age_group}, {self.size_group} 가구",
                 f"연 소득 약 {p['income_median_manwon']:,.0f}만원(소득 {self.income_quintile}분위)",
                 f"주거: {p['tenure_text']}", f"거주지: {p['region_text']}"]
        if self.has_disabled:
            parts.append("장애인 가구원 있음")
        if p["has_elderly_share"] >= 0.5:
            parts.append("65세 이상 가구원 있음")
        if p["has_child_share"] >= 0.5:
            parts.append("18세 미만 자녀 있음")
        if p["employed_share"] >= 0.5:
            parts.append("가구주 취업 중")
        else:
            parts.append("가구주 미취업")
        return ", ".join(parts)


def assign(hh: pd.DataFrame, persons: pd.DataFrame) -> pd.Series:
    """가구 → persona_id."""
    head = persons[persons["idx"] == 0].set_index("hh_id")
    size = persons.groupby("hh_id").size()
    dis = persons.assign(d=persons["disability"] != "none").groupby("hh_id")["d"].any()
    df = hh.set_index("hh_id")
    age = head.loc[df.index, "age"].to_numpy()
    ag = np.select([age <= hi for _, hi, _ in AGE_GROUPS], range(len(AGE_GROUPS)))
    sg = np.minimum(size.loc[df.index].to_numpy(), 4)
    q = np.searchsorted(INCOME_Q_CUTS, df["income_annual_manwon"].to_numpy(), side="right") + 1
    d = dis.loc[df.index].to_numpy().astype(int)
    return pd.Series([f"P{a}{s}{qq}{dd}" for a, s, qq, dd in zip(ag, sg, q, d)], index=df.index, name="persona_id")


def build(hh: pd.DataFrame, persons: pd.DataFrame) -> tuple[list[Persona], pd.Series]:
    pid = assign(hh, persons)
    df = hh.set_index("hh_id").join(pid)
    head = persons[persons["idx"] == 0].set_index("hh_id")
    per = persons.assign(eld=persons["age"] >= 65, chi=persons["age"] < 18).groupby("hh_id")[["eld", "chi"]].any()
    df = df.join(per).join(head[["employed"]])
    out = []
    for key, g in df.groupby("persona_id"):
        a, s, q, d = int(key[1]), int(key[2]), int(key[3]), bool(int(key[4]))
        w = g["weight"]
        ten = g.groupby("housing")["weight"].sum() / w.sum()
        ten_ko = {"own": "자가", "jeonse": "전세", "monthly_rent": "월세", "other": "무상 등"}
        top_ten = ", ".join(f"{ten_ko[k]} {v:.0%}" for k, v in ten.sort_values(ascending=False).head(2).items())
        metro = g["region_sido"].str.startswith(METRO)
        big = g["region_sido"].str.endswith("광역시") | g["region_sido"].str.startswith("세종")
        reg = {"수도권": float(w[metro].sum()), "광역시": float(w[big & ~metro].sum()),
               "도 지역": float(w[~metro & ~big].sum())}
        region_text = max(reg, key=reg.get) + f" 중심({max(reg.values()) / w.sum():.0%})"
        out.append(Persona(
            persona_id=key, age_group=AGE_GROUPS[a][2], size_group=SIZE_GROUPS[s], income_quintile=q,
            has_disabled=d, n_households=len(g), weight=float(w.sum()),
            profile={"income_median_manwon": float(g["income_annual_manwon"].median()),
                     "tenure_text": top_ten, "region_text": region_text,
                     "has_elderly_share": float((w * g["eld"]).sum() / w.sum()),
                     "has_child_share": float((w * g["chi"]).sum() / w.sum()),
                     "employed_share": float((w * g["employed"]).sum() / w.sum())}))
    return out, pid
