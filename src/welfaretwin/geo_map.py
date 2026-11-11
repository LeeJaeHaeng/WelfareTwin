"""웹 '가상 대한민국' 지도 — 시도 경계(SVG 경로) + 합성 인구를 시군구 분포대로 뿌린 입자 좌표.

경계: 통계청 SGIS 센서스용 행정구역경계(2018) — southkorea/southkorea-maps 의 GeoJSON("KOSTAT: Free to share or remix").
    data/raw/geo/skorea-provinces-2018-geo.json, skorea-municipalities-2018-geo.json (커밋하지 않음, 아래 URL)
    https://raw.githubusercontent.com/southkorea/southkorea-maps/master/kostat/2018/json/<파일명>
투영: 위도 36.2° 기준 등장방형(한반도 남부 규모에선 충분). 울릉도·독도는 동쪽 여백을 줄이려 서쪽으로 옮겨 그린다(점선 상자).
입자: 점 하나 = 합성 인구 가구(배경 POP_PER 가구) 또는 기초생활보장 자격 가구(ELIG_PER 가구). 시군구 경계 안에 무작위로 놓아
     수도권·대도시가 실제처럼 밀집해 보인다. 상태(미신청·수급·생계급여)는 웹에서 시도별 비율로 정한다.

    uv run python -m welfaretwin.geo_map        # → web/data/korea.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from welfaretwin.population.residents import province_of
from welfaretwin.rules.dsl import RULES_DB

ROOT = RULES_DB.parent
GEO = ROOT / "data" / "raw" / "geo"
OUT = ROOT / "web" / "data" / "korea.json"
SOURCE = "통계청 SGIS 센서스용 행정구역경계(2018), southkorea/southkorea-maps — KOSTAT: Free to share or remix"
H = 1000.0                       # viewBox 높이
LAT0, LAT_MAX, LON_MIN = 36.2, 38.70, 124.55
K = H / (LAT_MAX - 33.05)
ISLAND_LON, ISLAND_SHIFT = 130.5, 0.95     # 울릉도·독도: 서쪽으로 옮겨 그림
POP_PER, ELIG_PER = 6000, 800
SIDO_CODE = {"11": "서울", "21": "부산", "22": "대구", "23": "인천", "24": "광주", "25": "대전", "26": "울산", "29": "세종",
             "31": "경기", "32": "강원", "33": "충청북", "34": "충청남", "35": "전북", "36": "전라남", "37": "경상북",
             "38": "경상남", "39": "제주"}
LABEL = {"충청남": "충남", "충청북": "충북", "경상북": "경북", "경상남": "경남", "전라남": "전남"}
ALIAS = {("인천", "미추홀구"): "남구"}        # 2018 경계 이후 이름이 바뀐 곳
# 라벨 위치(경도, 위도) — 수도권처럼 붙어 있는 곳이 겹치지 않게 손으로 정함
LABEL_AT = {"서울": (127.02, 37.60), "인천": (126.30, 37.40), "경기": (127.32, 37.98), "강원": (128.30, 37.72),
            "충청북": (127.78, 36.86), "충청남": (126.82, 36.52), "세종": (127.22, 36.64), "대전": (127.43, 36.30),
            "전북": (127.12, 35.74), "광주": (126.84, 35.16), "전라남": (126.95, 34.83), "경상북": (128.78, 36.42),
            "대구": (128.58, 35.86), "울산": (129.27, 35.56), "경상남": (128.22, 35.30), "부산": (129.06, 35.17),
            "제주": (126.55, 33.38)}


def proj(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon = np.where(lon > ISLAND_LON, lon - ISLAND_SHIFT, lon)
    return (lon - LON_MIN) * math.cos(math.radians(LAT0)) * K, (LAT_MAX - lat) * K


def rings_of(geom: dict) -> list[np.ndarray]:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    out = []
    for poly in polys:
        for ring in poly:
            a = np.asarray(ring, dtype=float)
            x, y = proj(a[:, 0], a[:, 1])
            out.append(np.column_stack([x, y]))
    return out


def area(r: np.ndarray) -> float:
    x, y = r[:, 0], r[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def simplify(r: np.ndarray, tol: float) -> np.ndarray:
    """Douglas–Peucker(반복형)."""
    n = len(r)
    if n < 4:
        return r
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = r[i], r[j]
        seg = b - a
        L = float(np.hypot(*seg))
        pts = r[i + 1:j]
        rel = pts - a
        d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / L if L > 0 else np.hypot(rel[:, 0], rel[:, 1])
        k = int(np.argmax(d))
        if d[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return r[keep]


def path_d(rings: list[np.ndarray]) -> str:
    return " ".join("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in r) + "Z" for r in rings)


def inside(px: np.ndarray, py: np.ndarray, rings: list[np.ndarray]) -> np.ndarray:
    """짝홀 규칙 점-다각형 판정(구멍 포함)."""
    res = np.zeros(len(px), dtype=bool)
    for r in rings:
        x1, y1 = r[:, 0], r[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        for a, b, c, d in zip(x1, y1, x2, y2):
            cond = (b > py) != (d > py)
            with np.errstate(divide="ignore", invalid="ignore"):
                xi = a + (py - b) * (c - a) / (d - b)
            res ^= cond & (px < xi)
    return res


def sample(rings: list[np.ndarray], n: int, rng: np.random.Generator) -> np.ndarray:
    if n <= 0:
        return np.zeros((0, 2))
    allp = np.vstack(rings)
    (x0, y0), (x1, y1) = allp.min(0), allp.max(0)
    got = []
    tries = 0
    while sum(len(g) for g in got) < n and tries < 60:
        m = max(64, n * 4)
        px, py = rng.uniform(x0, x1, m), rng.uniform(y0, y1, m)
        ok = inside(px, py, rings)
        got.append(np.column_stack([px[ok], py[ok]]))
        tries += 1
    pts = np.vstack(got) if got else np.zeros((0, 2))
    return pts[:n]


def _counts(values: pd.Series, per: float, rng: np.random.Generator) -> pd.Series:
    """확률 반올림(결정적) — 작은 시군구도 기대값만큼 점을 받는다."""
    raw = values / per
    base = np.floor(raw)
    return (base + (rng.random(len(raw)) < (raw - base))).astype(int)


def build() -> dict:
    prov = json.loads((GEO / "skorea-provinces-2018-geo.json").read_text(encoding="utf-8"))["features"]
    muni = json.loads((GEO / "skorea-municipalities-2018-geo.json").read_text(encoding="utf-8"))["features"]
    provinces, isl = [], []
    for f in prov:
        key = province_of(f["properties"]["name"])
        rs = rings_of(f["geometry"])
        keep = []
        for r in rs:
            a = area(r)
            is_east = r[:, 0].min() > proj(np.array([ISLAND_LON - ISLAND_SHIFT]), np.array([37.0]))[0][0] and key == "경상북"
            if a < 6.0 and not is_east:          # 아주 작은 섬은 생략(울릉도·독도는 유지)
                continue
            keep.append(simplify(r, 0.35 if a > 50 else 0.15))
            if is_east:
                isl.append(r)
        main = max(keep, key=area)
        cx, cy = (float(v) for v in main.mean(0))
        lx, ly = proj(np.array([LABEL_AT[key][0]]), np.array([LABEL_AT[key][1]]))
        provinces.append({"key": key, "name": LABEL.get(key, key), "d": path_d(keep), "cx": round(cx, 1), "cy": round(cy, 1),
                          "lx": round(float(lx[0]), 1), "ly": round(float(ly[0]), 1)})
    w = max(float(np.vstack([np.vstack(rings_of(f["geometry"])) for f in prov])[:, 0].max()), 1) + 8
    # 시군구: 얇은 경계선 + 입자 배치용 다각형
    mrings, mlines = {}, []
    for f in muni:
        sido = SIDO_CODE[f["properties"]["code"][:2]]
        rs = [r for r in rings_of(f["geometry"]) if area(r) >= 1.0]
        if not rs:
            continue
        mrings.setdefault((sido, f["properties"]["name"]), []).extend(rs)
        mlines += [simplify(r, 0.45) for r in rs if area(r) >= 4.0]
    hh = pd.read_parquet(ROOT / "data" / "processed" / "households.parquet")
    st = pd.read_parquet(ROOT / "artifacts" / "sim" / "static_2026.parquet")
    nb = st[st["program_id"].isin(["NBLS_LIVELIHOOD", "NBLS_MEDICAL", "NBLS_HOUSING", "NBLS_EDUCATION"])] \
        .groupby("hh_id")["eligible"].any()
    hh = hh.set_index("hh_id").join(nb.rename("elig")).reset_index()
    hh["prov"] = hh["region_sido"].map(province_of)
    g = hh.groupby(["prov", "region_sigungu"]).apply(
        lambda d: pd.Series({"w": d["weight"].sum(), "ew": (d["weight"] * d["elig"].fillna(False)).sum()})).reset_index()
    rng = np.random.default_rng(20261111)
    g["n_pop"], g["n_elig"] = _counts(g["w"], POP_PER, rng), _counts(g["ew"], ELIG_PER, rng)
    pidx = {p["key"]: i for i, p in enumerate(provinces)}
    pop, elig, unmatched = [], [], []
    for r in g.itertuples():
        name = ALIAS.get((r.prov, r.region_sigungu), r.region_sigungu)
        rs = mrings.get((r.prov, name))
        if rs is None:      # 구가 나뉜 시(고양시 → 고양시덕양구 …) 또는 2018 이후 시도가 바뀐 곳(군위군)
            rs = [x for (s, n), v in mrings.items() if (s == r.prov and n.startswith(name)) for x in v] or \
                 [x for (s, n), v in mrings.items() if n == name for x in v]
        if not rs:
            unmatched.append(f"{r.prov} {r.region_sigungu}")
            rs = [x for (s, n), v in mrings.items() if s == r.prov for x in v]
        pts = sample(rs, int(r.n_pop) + int(r.n_elig), rng)
        for x, y in pts[: int(r.n_pop)]:
            pop += [round(float(x), 1), round(float(y), 1)]
        for x, y in pts[int(r.n_pop):]:
            elig += [round(float(x), 1), round(float(y), 1), pidx[r.prov]]
    ix0 = float(np.vstack(isl)[:, 0].min()) - 10 if isl else None
    iy0, iy1 = (float(np.vstack(isl)[:, 1].min()) - 10, float(np.vstack(isl)[:, 1].max()) + 10) if isl else (None, None)
    ix1 = float(np.vstack(isl)[:, 0].max()) + 10 if isl else None
    return {"w": round(w, 1), "h": H, "source": SOURCE, "pop_per": POP_PER, "elig_per": ELIG_PER,
            "provinces": provinces, "muni_d": path_d(mlines),
            "island_box": [round(ix0, 1), round(iy0, 1), round(ix1 - ix0, 1), round(iy1 - iy0, 1)] if isl else None,
            "pop": pop, "elig": elig, "unmatched": unmatched}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    m = build()
    OUT.write_text(json.dumps(m, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"시도 {len(m['provinces'])}개, 배경 점 {len(m['pop']) // 2:,}개(가구 {POP_PER:,}당 1), 자격 점 {len(m['elig']) // 3:,}개"
          f"(자격 가구 {ELIG_PER:,}당 1), 이름 못 맞춘 시군구 {m['unmatched']}")
    print(f"→ {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
