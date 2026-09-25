from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from etl.rtms import ApiError, QuotaExceeded, RetryableStatus, RtmsClient, parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "api"

KEY = "abc+def/ghi=="  # 디코딩 키에는 +, /, = 가 들어간다


def page_xml(total: int, items: list[dict[str, str]], code: str = "000") -> bytes:
    body = "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in item.items()) + "</item>" for item in items)
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?><response>'
        f"<header><resultCode>{code}</resultCode><resultMsg>MSG</resultMsg></header>"
        f"<body><items>{body}</items><numOfRows>1000</numOfRows><pageNo>1</pageNo>"
        f"<totalCount>{total}</totalCount></body></response>"
    ).encode()


def make_client(handler) -> RtmsClient:
    return RtmsClient(KEY, rps=0, http=httpx.Client(transport=httpx.MockTransport(handler)), wait=wait_none())


def test_parse_page_fixture():
    page = parse_page((FIXTURES / "sale_11410_202506.xml").read_bytes())
    assert page.total_count == 578
    assert len(page.items) == 578
    first = page.items[0]
    assert first["aptnm"] == "송도그린빌A동"  # 태그는 소문자로
    assert first["aptdong"] == ""  # 공백 한 칸 → 빈 문자열
    assert first["dealamount"] == "38,000"


def test_parse_page_lowercases_tags_across_apis():
    page = parse_page((FIXTURES / "rent_11410_202506.xml").read_bytes())
    assert "roadnm" in page.items[0]  # 전월세 roadnm, 매매 roadNm


def test_fetch_all_pages():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page_no = int(request.url.params["pageNo"])
        items = {1: [{"a": "1"}, {"a": "2"}], 2: [{"a": "3"}]}[page_no]
        return httpx.Response(200, content=page_xml(3, items))

    with make_client(handler) as client:
        items = client.fetch("sale", "11410", "202506")

    assert [i["a"] for i in items] == ["1", "2", "3"]
    assert [r.url.params["pageNo"] for r in requests] == ["1", "2"]
    params = requests[0].url.params
    assert (params["LAWD_CD"], params["DEAL_YMD"], params["numOfRows"]) == ("11410", "202506", "1000")
    assert params["serviceKey"] == KEY  # 디코딩 키를 한 번만 인코딩해 보낸다
    assert "abc%2Bdef%2Fghi%3D%3D" in str(requests[0].url)
    assert requests[0].url.path == "/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev"


def test_fetch_zero_rows():
    with make_client(lambda r: httpx.Response(200, content=page_xml(0, []))) as client:
        assert client.fetch("rent", "41110", "202506") == []


def test_fetch_fails_when_pages_run_short_of_total():
    def handler(request):
        page_no = int(request.url.params["pageNo"])
        return httpx.Response(200, content=page_xml(5, [{"a": "1"}, {"a": "2"}] if page_no == 1 else []))

    with make_client(handler) as client, pytest.raises(ApiError, match="totalCount 5"):
        client.fetch("sale", "11410", "202506")


def test_retries_on_5xx_429_and_network_error():
    responses = iter([500, 429, "connect", 200])

    def handler(request):
        status = next(responses)
        if status == "connect":
            raise httpx.ConnectError("boom")
        return httpx.Response(status, content=page_xml(1, [{"a": "1"}]) if status == 200 else b"")

    with make_client(handler) as client:
        assert client.fetch("sale", "11410", "202506") == [{"a": "1"}]


def test_empty_200_is_retried_not_zero_rows():
    # 건축HUB는 몰아서 호출하면 본문 없는 200을 준다. 0건으로 받아들이면 조회 실패가 "없음"이 된다
    responses = iter([b"", b"  \n", page_xml(1, [{"a": "1"}])])

    with make_client(lambda r: httpx.Response(200, content=next(responses))) as client:
        assert client.fetch("sale", "11410", "202506") == [{"a": "1"}]


def test_gives_up_after_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    client = RtmsClient(KEY, rps=0, http=httpx.Client(transport=httpx.MockTransport(handler)), attempts=3, wait=wait_none())
    with client, pytest.raises(RetryableStatus):
        client.fetch("sale", "11410", "202506")
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("status", "content", "match"),
    [
        (
            200,
            b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
            b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
            b"<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>",
            "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
        ),
        (
            403,
            '<?xml version="1.0" encoding="UTF-8"?>\n<OpenAPI_ServiceResponse>\n<cmmMsgHeader>\n'
            "  <errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>\n  <returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg>\n"
            "  <returnReasonCode>30</returnReasonCode>\n</cmmMsgHeader>\n</OpenAPI_ServiceResponse>".encode(),
            "^HTTP 403: SERVICE_KEY_IS_NOT_REGISTERED_ERROR / 등록되지 않은 서비스키 / 30$",
        ),
        (200, page_xml(0, [], code="03"), "resultCode '03'"),
        (200, b"Unauthorized", "XML"),
        (401, b"Unauthorized", "HTTP 401"),
    ],
)
def test_error_responses_are_not_retried(status, content, match):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=content)

    with make_client(handler) as client, pytest.raises(ApiError, match=match):
        client.fetch("sale", "11410", "202506")
    assert len(calls) == 1


QUOTA_GATEWAY_XML = (
    b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>"
    b"<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>"
    b"<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
)


@pytest.mark.parametrize(
    ("status", "content"),
    [
        (200, QUOTA_GATEWAY_XML),
        (429, QUOTA_GATEWAY_XML),  # 429여도 한도 초과면 재시도하지 않는다
        (403, QUOTA_GATEWAY_XML),
        (200, page_xml(0, [], code="22")),
    ],
)
def test_quota_exceeded_is_distinguished_and_not_retried(status, content):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=content)

    with make_client(handler) as client, pytest.raises(QuotaExceeded):
        client.fetch("sale", "11410", "202506")
    assert len(calls) == 1


def test_plain_429_is_retried_not_quota():
    responses = iter([429, 200])

    def handler(request):
        status = next(responses)
        return httpx.Response(status, content=page_xml(0, []) if status == 200 else b"Too Many Requests")

    with make_client(handler) as client:
        assert client.fetch("sale", "11410", "202506") == []


def test_redact_hides_key_in_any_encoding():
    with make_client(lambda r: httpx.Response(200)) as client:
        text = f"raw {KEY} quoted abc%2Bdef%2Fghi%3D%3D"
        assert client.redact(text) == "raw *** quoted ***"


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        RtmsClient("")
