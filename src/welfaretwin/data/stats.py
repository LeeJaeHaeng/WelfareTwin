"""검증용 통계 수집: e-나라지표 국민기초생활보장 수급 현황(지표 2760).

    uv run python -m welfaretwin.data.stats

e-나라지표 페이지는 통계표를 iframe(showStblGams3.do)으로 불러온다. 그 HTML 을 그대로
data/raw/stats/ 에 저장하고, 표를 파싱해 data/processed/stats/*.csv 로 만든다.
- 276001 기초생활보장 수급 현황: 수급자 수(천명), 수급률(%) — 출처: 보건복지부(내부행정자료)
- 276002 가구유형별 수급가구 수(가구) — 출처: 국민기초생활보장 수급자현황
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

PAGE_URL = "https://www.index.go.kr/unity/potal/main/EachDtlPageDetail.do?idx_cd=2760"
TABLE_URL = "https://www.index.go.kr/unity/potal/eNara/sub/showStblGams3.do"

TABLES = {
    "276001": {
        "name": "nbls_recipients",
        "title": "기초생활보장 수급 현황",
        "unit": {"수급자 수": "천명", "수급률": "%"},
        "source": "보건복지부 (내부행정자료), e-나라지표 지표 2760",
    },
    "276002": {
        "name": "nbls_households_by_type",
        "title": "가구유형별 수급가구 수",
        "unit": "가구",
        "source": "국민기초생활보장 수급자현황, e-나라지표 지표 2760",
    },
}


def _cell(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()


def parse_table(page: str) -> tuple[list[int], dict[str, list[float | None]]]:
    """통계표 HTML → (연도 목록, {행 이름: 연도별 값}). 빈 칸·'-' 는 None."""
    page = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    rows = []
    for tr in re.findall(r"<tr.*?</tr>", page, flags=re.S):
        cells = [_cell(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S)]
        if any(cells):
            rows.append(cells)
    header = next(r for r in rows if sum(c.isdigit() and len(c) == 4 for c in r[1:]) >= 2)
    years = [int(c) for c in header[1:]]
    data: dict[str, list[float | None]] = {}
    for r in rows[rows.index(header) + 1 :]:
        label, vals = r[0].replace(" ", ""), r[1:]
        if not label or len(vals) != len(years):
            continue
        data[label] = [float(v.replace(",", "")) if re.fullmatch(r"[\d,.]+", v) else None for v in vals]
    if not data:
        raise ValueError("통계표에서 데이터 행을 찾지 못했습니다. 페이지 구조를 확인하세요.")
    return years, data


def fetch(stts_cd: str, period: str, http: httpx.Client) -> str:
    res = http.get(TABLE_URL, params={"stts_cd": stts_cd, "idx_cd": "2760", "freq": "Y", "period": period})
    res.raise_for_status()
    return res.text


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raw_dir, out_dir = Path("data/raw/stats"), Path("data/processed/stats")
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    period = "2010:2025"
    with httpx.Client(timeout=30.0, headers={"User-Agent": "Mozilla/5.0"}) as http:
        for code, meta in TABLES.items():
            page = fetch(code, period, http)
            (raw_dir / f"enara_2760_{code}.html").write_text(page, encoding="utf-8")
            years, data = parse_table(page)
            csv_path = out_dir / f"{meta['name']}.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["year", *data])
                for i, y in enumerate(years):
                    w.writerow([y, *(data[k][i] for k in data)])
            meta_out = {**meta, "stts_cd": code, "page_url": PAGE_URL, "period": period,
                        "fetched_at": fetched_at}
            csv_path.with_suffix(".meta.json").write_text(
                json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{meta['title']}: {len(years)}개 연도 × {len(data)}개 행 → {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
