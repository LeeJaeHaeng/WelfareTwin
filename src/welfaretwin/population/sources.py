"""합성 인구 입력 집계표(KOSIS) — 표 ID·항목 코드는 KOSIS OpenAPI 로 확인한 값(2026-11-10).

각 함수는 numpy 배열과 출처 메타를 돌려준다. 응답 원문은 data/raw/kosis/ 에 캐시된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from welfaretwin.data.kosis import fetch, to_float

# 인구총조사 시도 코드(2025 명칭). 장애인(117)·주민등록 표는 코드가 달라 이름으로 맞춘다.
SIDO_CODES = ["11", "21", "22", "23", "24", "25", "26", "29", "31", "32", "33", "34", "35", "36", "37", "38", "39"]
SIDO_NAMES = ["서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시", "울산광역시",
              "세종특별자치시", "경기도", "강원특별자치도", "충청북도", "충청남도", "전북특별자치도", "전라남도",
              "경상북도", "경상남도", "제주특별자치도"]
# 가구주 연령 5세 구간(DT_1JC1511 objL2). 15세 미만 가구주는 15~19세에 합친다.
AGE_CODES = ["020", "025", "030", "035", "040", "045", "050", "055", "060", "065", "070", "075", "080", "085", "086"]
AGE_BANDS = [(15, 19), (20, 24), (25, 29), (30, 34), (35, 39), (40, 44), (45, 49), (50, 54), (55, 59),
             (60, 64), (65, 69), (70, 74), (75, 79), (80, 84), (85, 95)]
SIZES = [1, 2, 3, 4, 5, 6]                       # 6 = 6인 이상
TENURE = ["own", "jeonse", "monthly_rent", "other"]
# 가계금융복지조사 가구주 연령 그룹 — AGE_BANDS 인덱스 → 그룹. 60~64세는 '60세 이상'(025),
# 65세 이상은 '65세 이상'(026) 분포를 쓴다(025 에는 아직 일하는 60~64세가 섞여 고령 가구 소득이 과대).
AGE_GROUP_OF_BAND = np.array([0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 5, 5, 5, 5, 5])
N_AGE_GROUPS = 6
SIZE_GROUP = np.array([0, 1, 2, 3, 4, 4])          # 가구원수 1..5인 이상
INCOME_BANDS = [(0, 1000), (1000, 3000), (3000, 5000), (5000, 7000), (7000, 10000), (10000, 30000)]  # 만원/년


def sido_key(name: str) -> str:
    alias = {"충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
             "전라남도": "전남", "경상북도": "경북", "경상남도": "경남"}
    return alias.get(name, name[:2])


SIDO_INDEX = {sido_key(n): i for i, n in enumerate(SIDO_NAMES)}


@dataclass
class Table:
    data: np.ndarray
    source: dict = field(default_factory=dict)


def _src(org, tbl, title, year, rows) -> dict:
    return {"orgId": org, "tblId": tbl, "title": title, "year": year,
            "prd_de": sorted({r.get("PRD_DE") for r in rows})}


def households_sido_age_size() -> Table:
    """2025 인구총조사: 시도(17) × 가구주 연령(15) × 가구원수(6) 일반가구 수."""
    rows = fetch("101", "DT_1JC1511", itm_id="T210 T220 T230 T240 T250 T260 T270",
                 objs={"objL1": " ".join(SIDO_CODES), "objL2": "015 " + " ".join(AGE_CODES)})
    itm = {"T210": 0, "T220": 1, "T230": 2, "T240": 3, "T250": 4, "T260": 5, "T270": 5}
    age_idx = {c: i for i, c in enumerate(AGE_CODES)} | {"015": 0}
    a = np.zeros((len(SIDO_CODES), len(AGE_BANDS), len(SIZES)))
    for r in rows:
        v = to_float(r["DT"])
        if v is None or r["C1"] not in SIDO_CODES:
            continue
        a[SIDO_CODES.index(r["C1"]), age_idx[r["C2"]], itm[r["ITM_ID"]]] += v
    return Table(a, _src("101", "DT_1JC1511", "가구주의 연령 및 가구원수별 가구(일반가구)", 2025, rows))


def tenure_sido_age() -> Table:
    """2020 인구총조사(20% 표본): 시도 × 가구주 연령 × 점유형태."""
    rows = fetch("101", "DT_1PE2010", itm_id="T20 T30 T40 T50 T60 T70",
                 objs={"objL1": " ".join(SIDO_CODES), "objL2": "015 " + " ".join(AGE_CODES)}, prd_se="F")
    itm = {"T20": 0, "T30": 1, "T40": 2, "T50": 2, "T60": 2, "T70": 3}
    age_idx = {c: i for i, c in enumerate(AGE_CODES)} | {"015": 0}
    a = np.zeros((len(SIDO_CODES), len(AGE_BANDS), len(TENURE)))
    for r in rows:
        v = to_float(r["DT"])
        if v is None or r["C1"] not in SIDO_CODES or r["C2"] not in age_idx:
            continue
        a[SIDO_CODES.index(r["C1"]), age_idx[r["C2"]], itm[r["ITM_ID"]]] += v
    return Table(a, _src("101", "DT_1PE2010", "가구주의 연령별/점유형태별 가구(일반가구)", 2020, rows))


def tenure_sido_size() -> Table:
    """2020 인구총조사(20% 표본): 시도 × 점유형태 × 가구원수."""
    rows = fetch("101", "DT_1PE2003", itm_id="T10",
                 objs={"objL1": " ".join(SIDO_CODES), "objL2": "1 2 3 4 5 6", "objL3": "1 2 3 4 5 6 7"},
                 prd_se="F")
    ten = {"1": 0, "2": 1, "3": 2, "4": 2, "5": 2, "6": 3}
    a = np.zeros((len(SIDO_CODES), len(TENURE), len(SIZES)))
    for r in rows:
        v = to_float(r["DT"])
        if v is None or r["C1"] not in SIDO_CODES:
            continue
        a[SIDO_CODES.index(r["C1"]), ten[r["C2"]], min(int(r["C3"]), 6) - 1] += v
    return Table(a, _src("101", "DT_1PE2003", "점유형태별/가구원수별 가구(일반가구)", 2020, rows))


def income_band_shares() -> tuple[Table, Table]:
    """가계금융복지조사(2025, 2024년 소득): 소득 구간 비중(%) — 가구주 연령 그룹별, 가구원수별."""
    rows = fetch("101", "DT_1HDAAB03", itm_id="T10",
                 objs={"objL1": "021 022 023 024 025 026 071 072 073 074 075", "objL2": "10 20 30 40 50 60"})
    band = {"10": 0, "20": 1, "30": 2, "40": 3, "50": 4, "60": 5}
    by_age, by_size = np.zeros((6, 6)), np.zeros((5, 6))
    for r in rows:
        v = to_float(r["DT"])
        if v is None:
            continue
        c = int(r["C1"])
        target = by_age[c - 21] if 21 <= c <= 26 else by_size[c - 71]
        target[band[r["C2"]]] = v
    src = _src("101", "DT_1HDAAB03", "가구특성별 소득구간별 가구분포", 2025, rows)
    return Table(by_age, src), Table(by_size, src)


def income_source_means() -> Table:
    """가구주 연령 그룹(6) × 소득원천(경상, 근로, 사업, 재산, 공적이전, 사적이전) 평균(만원/년)."""
    rows = fetch("101", "DT_1HDAAB01", itm_id="T00",
                 objs={"objL1": "021 022 023 024 025 026", "objL2": "00 10 20 30 40 50"})
    a = np.zeros((6, 6))
    for r in rows:
        v = to_float(r["DT"])
        if v is not None:
            a[int(r["C1"]) - 21, int(r["C2"]) // 10] = v
    return Table(a, _src("101", "DT_1HDAAB01", "가구특성별 소득원천별 가구소득", 2025, rows))


def assets_by_income_quintile() -> Table:
    """소득 5분위 × (자산, 금융자산, 실물자산, 부채, 경상소득) 평균(만원) + 부채 보유 비율(%)."""
    rows = fetch("101", "DT_1HDAAA10", itm_id="T01 T04",
                 objs={"objL1": "A0100", "objL2": "B1901 B1902 B1903 B1904 B1905",
                       "objL3": "C05 C0501 C0502 C06 C04"})
    cols = {"C05": 0, "C0501": 1, "C0502": 2, "C06": 3, "C04": 4}
    a = np.full((5, 6), np.nan)
    for r in rows:
        v = to_float(r["DT"])
        if v is None:
            continue
        q = int(r["C2"][-1]) - 1
        if r["ITM_ID"] == "T01":
            a[q, cols[r["C3"]]] = v
        elif r["ITM_ID"] == "T04" and r["C3"] == "C06":
            a[q, 5] = v
    return Table(a, _src("101", "DT_1HDAAA10", "소득5분위별 자산, 부채, 소득 현황", 2025, rows))


def income_net_worth_percentiles() -> Table:
    """가구소득·순자산 P10..P90 경계값(만원). 행 0 = 순자산, 행 1 = 가구소득(전년도)."""
    rows = fetch("101", "DT_1HDAAA21", itm_id="T10 T20",
                 objs={"objL1": "301 302 303 304 305 306 307 308 309"})
    a = np.zeros((2, 9))
    for r in rows:
        v = to_float(r["DT"])
        if v is not None:
            a[0 if r["ITM_ID"] == "T10" else 1, int(r["C1"]) - 301] = v
    return Table(a, _src("101", "DT_1HDAAA21", "순자산, 가구소득의 분위별 평균, 점유율 및 경계값", 2025, rows))


def disability_prevalence() -> Table:
    """시도별 등록장애인 비율 — [심한장애, 심하지 않은 장애] / 총인구."""
    rows = fetch("117", "DT_11761_N008", itm_id="T001",
                 objs={"objL1": "CHUT0", "objL2": "ALL", "objL3": "01 02", "objL4": "T001"})
    pop = fetch("101", "DT_1IN1502", itm_id="T100", objs={"objL1": " ".join(SIDO_CODES)})
    disabled = np.zeros((len(SIDO_CODES), 2))
    for r in rows:
        v = to_float(r["DT"])
        k = SIDO_INDEX.get(sido_key(r["C2_NM"]))
        if v is not None and k is not None:
            disabled[k, 0 if r["C3"] == "01" else 1] += v
    population = np.zeros(len(SIDO_CODES))
    for r in pop:
        v = to_float(r["DT"])
        if v is not None and r["C1"] in SIDO_CODES:
            population[SIDO_CODES.index(r["C1"])] = v
    src = {"disabled": _src("117", "DT_11761_N008", "시도별 장애정도별 등록장애인수", 2025, rows),
           "population": _src("101", "DT_1IN1502", "인구, 가구 및 주택", 2025, pop)}
    return Table(disabled / population[:, None], src)


def is_sigungu_code(code: str) -> bool:
    """5자리 시군구 코드(동부·읍부·면부 003~005 와 일반구(끝자리 1~9) 제외)."""
    return len(code) == 5 and code[2:] not in ("003", "004", "005") and code.endswith("0")


def households_sigungu_age_size() -> Table:
    """2025 인구총조사: 시도별 시군구 × 가구주 연령(15) × 가구원수(6). data = {시도 인덱스: (시군구 이름 목록, 배열)}."""
    rows = fetch("101", "DT_1JC1511", itm_id="T210 T220 T230 T240 T250 T260 T270",
                 objs={"objL1": "ALL", "objL2": "015 " + " ".join(AGE_CODES)})
    itm = {"T210": 0, "T220": 1, "T230": 2, "T240": 3, "T250": 4, "T260": 5, "T270": 5}
    age_idx = {c: i for i, c in enumerate(AGE_CODES)} | {"015": 0}
    by_sido: dict[int, dict[str, np.ndarray]] = {}
    for r in rows:
        code, v = r["C1"], to_float(r["DT"])
        if v is None or not is_sigungu_code(code) or code[:2] not in SIDO_CODES or r["C2"] not in age_idx:
            continue
        si = SIDO_CODES.index(code[:2])
        a = by_sido.setdefault(si, {}).setdefault(r["C1_NM"], np.zeros((len(AGE_BANDS), len(SIZES))))
        a[age_idx[r["C2"]], itm[r["ITM_ID"]]] += v
    data = {si: (list(d), np.stack(list(d.values()))) for si, d in by_sido.items()}
    return Table(data, _src("101", "DT_1JC1511", "가구주의 연령 및 가구원수별 가구(일반가구) - 시군구", 2025, rows))
