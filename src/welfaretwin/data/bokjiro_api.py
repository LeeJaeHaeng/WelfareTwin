"""한국사회보장정보원 복지서비스 Open API 클라이언트 (중앙부처 / 지자체).

엔드포인트 출처
- 기본 URL·요청 파라미터: 공공데이터포털 상세 페이지(확인 2026-11-09)
  - 중앙부처복지서비스 https://www.data.go.kr/data/15090532/openapi.do
  - 지자체복지서비스   https://www.data.go.kr/data/15108347/openapi.do
- 오퍼레이션 경로(…listV001, …detailedV001 등): 포털 페이지에 경로가 노출되지 않아
  공개 구현(github.com/aha645/welfare welfare_extract/sources.py)에서 가져왔고,
  2026-11-10 실호출로 확인했다(중앙 totalCount 461, 지자체 4828; 목록 wantedList/servList,
  상세 wantedDtl). 지자체 목록은 numOfRows=1000 까지 응답함을 확인.

응답은 XML이다. 원문은 가공하지 않고 그대로 저장한다(collect.py).
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal

import httpx

Source = Literal["central", "local"]

ENDPOINTS_VERIFIED = True

_BASE = "https://apis.data.go.kr/B554287"


@dataclass(frozen=True)
class Endpoint:
    source: Source
    list_url: str
    detail_url: str
    list_params: dict[str, str] = field(default_factory=dict)
    detail_params: dict[str, str] = field(default_factory=dict)
    max_rows: int = 100  # numOfRows 상한


ENDPOINTS: dict[Source, Endpoint] = {
    "central": Endpoint(
        source="central",
        list_url=f"{_BASE}/NationalWelfareInformationsV001/NationalWelfarelistV001",
        detail_url=f"{_BASE}/NationalWelfareInformationsV001/NationalWelfaredetailedV001",
        # callTp·srchKeyCode 는 목록 조회 필수 파라미터(포털 명세). 003 = 제목+내용
        list_params={"callTp": "L", "srchKeyCode": "003"},
        detail_params={"callTp": "D"},
        max_rows=500,  # 포털 명세: numOfRows 최대 500
    ),
    "local": Endpoint(
        source="local",
        list_url=f"{_BASE}/LocalGovernmentWelfareInformations/LcgvWelfarelist",
        detail_url=f"{_BASE}/LocalGovernmentWelfareInformations/LcgvWelfaredetailed",
        max_rows=500,  # 명세에 상한 없음. 1000 까지 실측 확인, 여유 두고 500
    ),
}

# 공공데이터포털 게이트웨이 오류(cmmMsgHeader/returnReasonCode) 중 재시도할 코드.
# 01 APPLICATION_ERROR, 04 HTTP_ERROR, 05 SERVICETIME_OUT. 나머지(22 일일 한도 초과,
# 30 미등록 키 등)는 재시도해도 소용없으므로 즉시 중단한다.
_RETRYABLE_GATEWAY_CODES = {"1", "01", "04", "05"}
_OK_RESULT_CODES = {"0", "00"}


class ApiError(RuntimeError):
    def __init__(self, code: str | None, message: str, *, retryable: bool) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.retryable = retryable


def parse_response(content: bytes) -> ET.Element:
    """XML 응답을 파싱하고, 게이트웨이/서비스 오류면 ApiError 를 던진다."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ApiError(None, f"XML 파싱 실패: {content[:200]!r}", retryable=True) from e

    header = root.find(".//cmmMsgHeader")
    if header is not None:
        code = (header.findtext("returnReasonCode") or "").strip()
        msg = (header.findtext("returnAuthMsg") or header.findtext("errMsg") or "").strip()
        raise ApiError(code, msg, retryable=code in _RETRYABLE_GATEWAY_CODES)

    code = root.findtext("resultCode")
    if code is not None and code.strip() not in _OK_RESULT_CODES:
        raise ApiError(code.strip(), root.findtext("resultMessage") or "", retryable=False)
    return root


@dataclass
class ListPage:
    total_count: int
    serv_ids: list[str]
    raw: bytes


class BokjiroClient:
    def __init__(
        self,
        service_key: str,
        *,
        http: httpx.Client | None = None,
        min_interval: float = 0.2,
        max_retries: int = 4,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not service_key:
            raise ValueError("DATA_GO_KR_API_KEY 가 비어 있습니다 (.env 확인)")
        self._key = service_key
        self._http = http or httpx.Client(timeout=30.0)
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff = backoff
        self._sleep = sleep
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def get(self, url: str, params: dict[str, str]) -> tuple[bytes, ET.Element]:
        """속도 제한 + 지수 백오프 재시도. (원문 bytes, 파싱된 root) 반환."""
        attempt = 0
        while True:
            self._throttle()
            try:
                res = self._http.get(url, params={"serviceKey": self._key, **params})
                if res.status_code == 429 or res.status_code >= 500:
                    # 게이트웨이는 일일 한도 초과(22)도 429 + XML 본문으로 준다.
                    # 본문에 오류 코드가 있으면 그 판단(재시도 여부)을 따른다.
                    try:
                        parse_response(res.content)
                    except ApiError as body_err:
                        if body_err.code:
                            raise
                    raise ApiError(str(res.status_code), "HTTP 오류", retryable=True)
                res.raise_for_status()
                return res.content, parse_response(res.content)
            except (httpx.TransportError, ApiError) as e:
                retryable = isinstance(e, httpx.TransportError) or e.retryable
                if not retryable or attempt >= self._max_retries:
                    raise
                self._sleep(self._backoff * 2**attempt)
                attempt += 1

    def list_page(
        self, source: Source, page_no: int, num_rows: int, extra: dict[str, str] | None = None
    ) -> ListPage:
        ep = ENDPOINTS[source]
        params = {
            **ep.list_params,
            "pageNo": str(page_no),
            "numOfRows": str(min(num_rows, ep.max_rows)),
            **(extra or {}),
        }
        raw, root = self.get(ep.list_url, params)
        total = int((root.findtext(".//totalCount") or "0").strip() or 0)
        ids = [
            sid.strip()
            for item in root.iter("servList")
            if (sid := item.findtext("servId")) and sid.strip()
        ]
        if total > 0 and not ids and page_no == 1:
            raise ApiError(
                None,
                "totalCount>0 인데 servList/servId 가 없습니다. 응답 구조를 활용가이드와 대조하세요.",
                retryable=False,
            )
        return ListPage(total, ids, raw)

    def iter_list_pages(
        self, source: Source, num_rows: int | None = None, extra: dict[str, str] | None = None
    ) -> Iterator[ListPage]:
        rows = min(num_rows or ENDPOINTS[source].max_rows, ENDPOINTS[source].max_rows)
        page_no, seen = 1, 0
        while True:
            page = self.list_page(source, page_no, rows, extra)
            yield page
            seen += len(page.serv_ids)
            if not page.serv_ids or seen >= page.total_count:
                return
            page_no += 1

    def detail(self, source: Source, serv_id: str) -> tuple[str, dict[str, str], bytes]:
        """(요청 URL, 요청 파라미터(키 제외), 응답 원문) 반환."""
        ep = ENDPOINTS[source]
        params = {**ep.detail_params, "servId": serv_id}
        raw, _ = self.get(ep.detail_url, params)
        return ep.detail_url, params, raw
