"""수집기 테스트 — 네트워크 없이 httpx.MockTransport 로 API 응답을 흉내 낸다."""

import json

import httpx
import pytest

from welfaretwin.data.bokjiro_api import ApiError, BokjiroClient, parse_response
from welfaretwin.data.collect import collect_source, read_manifest, scan_manifest, write_manifest

KEY = "TEST-KEY"


def _list_xml(total: int, ids: list[str]) -> bytes:
    items = "".join(f"<servList><servId>{i}</servId><servNm>{i}명</servNm></servList>" for i in ids)
    return (
        f"<wantedList><resultCode>0</resultCode><totalCount>{total}</totalCount>{items}</wantedList>"
    ).encode()


def _detail_xml(sid: str) -> bytes:
    return (
        f"<wantedDtl><resultCode>0</resultCode><servId>{sid}</servId>"
        f"<servNm>서비스 {sid}</servNm><slctCritCn>기준중위소득 이하</slctCritCn></wantedDtl>"
    ).encode()


GATEWAY_ERR = (
    b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
    b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    b"<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
)

ALL_IDS = [f"WLF{n:08d}" for n in range(1, 6)]  # 5건, 쪽당 2건 → 3쪽


def make_client(handler, **kw) -> BokjiroClient:
    return BokjiroClient(
        KEY, http=httpx.Client(transport=httpx.MockTransport(handler)),
        min_interval=0, sleep=lambda s: None, **kw,
    )


def api_handler(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        q = req.url.params
        assert q["serviceKey"] == KEY
        if req.url.path.endswith("NationalWelfarelistV001"):
            assert q["callTp"] == "L" and q["srchKeyCode"] == "003"
            page, rows = int(q["pageNo"]), int(q["numOfRows"])
            return httpx.Response(200, content=_list_xml(len(ALL_IDS), ALL_IDS[(page - 1) * rows : page * rows]))
        if req.url.path.endswith("NationalWelfaredetailedV001"):
            assert q["callTp"] == "D"
            return httpx.Response(200, content=_detail_xml(q["servId"]))
        return httpx.Response(404)
    return handler


def test_collect_list_then_detail(tmp_path):
    calls: list = []
    client = make_client(api_handler(calls))
    manifest: dict = {}
    s = collect_source(client, "central", tmp_path, manifest, num_rows=2, log=lambda *_: None)

    assert s["list_total_count"] == 5 and s["detail_fetched"] == 5 and not s["detail_failed"]
    assert len(list((tmp_path / "central" / "_list").glob("page_*.xml"))) == 3
    rec = json.loads((tmp_path / "central" / "WLF00000003.json").read_text(encoding="utf-8"))
    assert rec["raw_xml"] == _detail_xml("WLF00000003").decode()  # 원문 그대로
    assert "serviceKey" not in rec["params"] and KEY not in json.dumps(rec)
    assert len(manifest) == 5

    write_manifest(tmp_path / "_manifest.csv", manifest)
    assert read_manifest(tmp_path / "_manifest.csv") == manifest
    assert scan_manifest(tmp_path) == manifest  # 디스크에서 재구성해도 같다


def test_collect_skips_existing(tmp_path):
    calls: list = []
    client = make_client(api_handler(calls))
    collect_source(client, "central", tmp_path, {}, num_rows=2, log=lambda *_: None)
    calls.clear()
    s = collect_source(client, "central", tmp_path, {}, num_rows=2, log=lambda *_: None)
    assert s["detail_fetched"] == 0 and s["detail_skipped_existing"] == 5
    assert not any("detailed" in c.url.path for c in calls)


def test_limit(tmp_path):
    client = make_client(api_handler([]))
    s = collect_source(client, "central", tmp_path, {}, num_rows=2, limit=3, log=lambda *_: None)
    assert s["detail_fetched"] == 3


def test_retry_on_5xx_then_success():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=_detail_xml("WLF1"))

    _, _, raw = make_client(handler).detail("central", "WLF1")
    assert n["i"] == 3 and b"WLF1" in raw


def test_retry_gives_up():
    client = make_client(lambda req: httpx.Response(500), max_retries=2)
    with pytest.raises(ApiError):
        client.detail("central", "WLF1")


def test_unregistered_key_not_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(200, content=GATEWAY_ERR)

    with pytest.raises(ApiError) as e:
        make_client(handler).detail("local", "WLF1")
    assert e.value.code == "30" and not e.value.retryable and n["i"] == 1


def test_unregistered_key_aborts_collection(tmp_path):
    with pytest.raises(ApiError):
        collect_source(make_client(lambda r: httpx.Response(200, content=GATEWAY_ERR)),
                       "local", tmp_path, {}, log=lambda *_: None)


def test_service_result_code_error():
    xml = b"<wantedDtl><resultCode>10</resultCode><resultMessage>bad param</resultMessage></wantedDtl>"
    with pytest.raises(ApiError) as e:
        parse_response(xml)
    assert e.value.code == "10"


def test_unexpected_list_structure_is_loud():
    xml = b"<x><resultCode>0</resultCode><totalCount>3</totalCount><item><id>1</id></item></x>"
    client = make_client(lambda r: httpx.Response(200, content=xml))
    with pytest.raises(ApiError, match="응답 구조"):
        client.list_page("local", 1, 10)


QUOTA_ERR = (
    b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</errMsg>"
    b"<returnAuthMsg>quota</returnAuthMsg><returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
)


def test_quota_429_with_body_code_22_is_not_retried():
    """실측(2026-11-10): 중앙 API 는 한도 초과 시 HTTP 429 + 본문 코드 22 를 준다."""
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429, content=QUOTA_ERR)

    with pytest.raises(ApiError) as e:
        make_client(handler).detail("central", "WLF1")
    assert e.value.code == "22" and n["i"] == 1


def test_bare_429_is_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429) if n["i"] == 1 else httpx.Response(200, content=_detail_xml("WLF1"))

    make_client(handler).detail("central", "WLF1")
    assert n["i"] == 2


def test_consecutive_failures_abort(tmp_path):
    def handler(req):
        if "list" in req.url.path:
            return api_handler([])(req)
        return httpx.Response(500)

    with pytest.raises(ApiError, match="연속"):
        collect_source(make_client(handler, max_retries=0), "central", tmp_path, {},
                       num_rows=2, log=lambda *_: None)
