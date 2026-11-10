"""KOSIS(국가통계포털) OpenAPI 클라이언트.

응답 원문은 data/raw/kosis/<tblId>__<요청해시>.json 에 저장하고, 같은 요청은 캐시에서 읽는다.
API 키는 환경변수 KOSIS_API_KEY (.env).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

DATA_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
META_URL = "https://kosis.kr/openapi/statisticsData.do"
RAW_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "kosis"


class KosisError(RuntimeError):
    pass


def _key() -> str:
    load_dotenv()
    key = os.environ.get("KOSIS_API_KEY", "").strip()
    if not key:
        raise KosisError("KOSIS_API_KEY 가 없습니다(.env)")
    return key


def _get(url: str, params: dict, http: httpx.Client | None = None) -> list | dict:
    own = http is None
    http = http or httpx.Client(timeout=60.0)
    try:
        res = http.get(url, params={"apiKey": _key(), "format": "json", "jsonVD": "Y", **params})
        res.raise_for_status()
        data = res.json()
    finally:
        if own:
            http.close()
    if isinstance(data, dict) and "err" in data:
        raise KosisError(f"[{data.get('err')}] {data.get('errMsg')} — {params}")
    return data


def fetch(org_id: str, tbl_id: str, *, itm_id: str = "ALL", objs: dict[str, str] | None = None,
          prd_se: str = "Y", start: str | None = None, end: str | None = None, latest: int | None = 1,
          refresh: bool = False, http: httpx.Client | None = None) -> list[dict]:
    """통계표 자료 조회. objs 예: {"objL1": "ALL", "objL2": "ALL"}. latest=n 이면 최근 n개 시점."""
    params = {"method": "getList", "orgId": org_id, "tblId": tbl_id, "itmId": itm_id, "prdSe": prd_se,
              **(objs or {"objL1": "ALL"})}
    if start and end:
        params.update(startPrdDe=start, endPrdDe=end)
    elif latest:
        params["newEstPrdCnt"] = str(latest)
    tag = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
    path = RAW_DIR / f"{tbl_id}__{tag}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))["rows"]
    rows = _get(DATA_URL, params, http)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"request": params, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return rows


def meta(org_id: str, tbl_id: str, kind: str = "ITM") -> list | dict:
    return _get(META_URL, {"method": "getMeta", "type": kind, "orgId": org_id, "tblId": tbl_id})


def to_float(v: str | None) -> float | None:
    if v is None:
        return None
    v = str(v).replace(",", "").strip()
    try:
        return float(v)
    except ValueError:
        return None   # '-', 'X'(비밀보호) 등
