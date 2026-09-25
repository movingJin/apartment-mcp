"""공공데이터포털(apis.data.go.kr) XML API 공통 클라이언트.

실거래가(etl.rtms)와 건축HUB(etl.bldg)가 같이 쓴다.
- 인증키는 디코딩 키를 params로 넘긴다. 인코딩은 httpx가 한다(SPEC "API 사용 시 주의" 3)
- 요청 간격을 rps로 제한하고, 429/5xx·네트워크 오류·본문이 빈 200 응답은 지수 백오프로 재시도한다
- 모든 페이지를 받고, 받은 항목 수가 totalCount와 다르면 실패로 본다
- 일일 트래픽 한도 초과는 QuotaExceeded로 따로 알린다. 재시도하지 않는다
"""

import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

# 공공데이터포털 공통 오류 코드 22: LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR
QUOTA_EXCEEDED_CODE = "22"


class ApiError(Exception):
    """재시도해도 소용없는 응답: 결과 코드 오류, 인증 실패, 형식 오류, 건수 불일치."""


class QuotaExceeded(ApiError):
    """일일 트래픽 한도 초과. 같은 API의 남은 호출도 모두 실패하므로 수집은 그 API를 멈춘다.

    실제 응답은 아직 못 봤다. 오류 코드 22를 게이트웨이 오류 XML(returnReasonCode)과
    정상 응답 헤더(resultCode) 양쪽에서 찾는다. 형식이 다르면 일반 오류로 잡히고,
    수집 쪽의 연속 실패 차단이 멈춘다.
    """


class RetryableStatus(Exception):
    """HTTP 429/5xx, 또는 본문이 빈 200 응답."""


@dataclass(frozen=True)
class Page:
    total_count: int
    items: list[dict[str, str]]


def parse_page(content: bytes, ok_code: str) -> Page:
    """응답 XML 한 페이지. 항목은 {소문자 태그: 앞뒤 공백을 뗀 값} dict다.

    정상 resultCode는 API마다 다르다(실거래가 '000', 건축HUB '00').
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise ApiError(f"XML이 아닌 응답: {_head(content)}") from None

    if root.tag != "response":
        raise _gateway_error(content, "오류 응답")

    code = (root.findtext("header/resultCode") or "").strip()
    if code != ok_code:
        msg = (root.findtext("header/resultMsg") or "").strip()
        error = QuotaExceeded if code == QUOTA_EXCEEDED_CODE else ApiError
        raise error(f"resultCode {code!r}: {msg}")

    total = (root.findtext("body/totalCount") or "").strip()
    if not total.isdigit():
        raise ApiError(f"totalCount 없음: {_head(content)}")
    items = [
        {child.tag.lower(): (child.text or "").strip() for child in item}
        for item in root.iterfind("body/items/item")
    ]
    return Page(int(total), items)


def _head(content: bytes) -> str:
    return repr(content[:200].decode("utf-8", "replace"))


def _gateway_error(content: bytes, prefix: str) -> ApiError:
    """게이트웨이 오류 XML(<OpenAPI_ServiceResponse><cmmMsgHeader>)이면 사유만 뽑는다.
    인증키 미등록은 HTTP 403에 이 XML이 실려 온다(2026-09-25 확인)."""
    try:
        header = ET.fromstring(content).find(".//cmmMsgHeader")
    except ET.ParseError:
        header = None
    if header is None:
        return ApiError(f"{prefix}: {_head(content)}")
    parts = [(header.findtext(tag) or "").strip() for tag in ("errMsg", "returnAuthMsg", "returnReasonCode")]
    error = QuotaExceeded if parts[2] == QUOTA_EXCEEDED_CODE else ApiError
    return error(f"{prefix}: " + " / ".join(p for p in parts if p))


class DataGoKrClient:
    def __init__(
        self,
        service_key: str,
        *,
        rps: float = 5,
        http: httpx.Client | None = None,
        attempts: int = 6,
        wait: wait_base = wait_exponential(multiplier=1, min=1, max=60),
    ):
        if not service_key:
            raise ValueError("SERVICE_KEY가 비어 있음")
        self._key = service_key
        self._http = http or httpx.Client(timeout=30)
        self._interval = 1 / rps if rps > 0 else 0
        self._last_request = float("-inf")
        self._retrying = Retrying(
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatus)),
            stop=stop_after_attempt(attempts),
            wait=wait,
            reraise=True,
            before_sleep=self._log_retry,
        )

    def _log_retry(self, state: RetryCallState) -> None:
        """재시도 대기가 쌓여 실행이 느려질 때 이유가 보이게 한다."""
        e = state.outcome.exception()
        wait = state.next_action.sleep if state.next_action else 0
        print(self.redact(f"  재시도 {state.attempt_number}회째 실패 ({type(e).__name__}: {e}), {wait:.0f}초 뒤 다시"),
              file=sys.stderr)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self._http.close()

    def redact(self, text: str) -> str:
        """오류 메시지를 로그·DB에 남기기 전에 인증키를 가린다."""
        for secret in {self._key, urllib.parse.quote(self._key, safe=""), urllib.parse.quote_plus(self._key)}:
            text = text.replace(secret, "***")
        return text

    def _fetch_all(self, url: str, params: dict, *, ok_code: str, page_size: int) -> list[dict[str, str]]:
        """마지막 페이지까지 받아 항목을 합친다. 합친 수가 totalCount와 다르면 ApiError."""
        items: list[dict[str, str]] = []
        page_no = 1
        while True:
            page = parse_page(
                self._retrying(self._get, url, {**params, "numOfRows": page_size, "pageNo": page_no}), ok_code
            )
            items.extend(page.items)
            if not page.items or len(items) >= page.total_count:
                break
            page_no += 1
        if len(items) != page.total_count:
            raise ApiError(f"{page_no}페이지까지 받은 항목 {len(items)}건이 totalCount {page.total_count}와 다름")
        return items

    def _get(self, url: str, params: dict) -> bytes:
        delay = self._last_request + self._interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()

        resp = self._http.get(url, params={"serviceKey": self._key, **params})
        if resp.status_code == 200:
            # 건축HUB는 몰아서 호출하면 가끔 본문 없는 200을 준다(2026-09-25 확인). 0건 응답과 다르다
            if not resp.content.strip():
                raise RetryableStatus("HTTP 200 빈 응답")
            return resp.content
        error = _gateway_error(resp.content, f"HTTP {resp.status_code}")
        if not isinstance(error, QuotaExceeded) and (resp.status_code == 429 or resp.status_code >= 500):
            raise RetryableStatus(f"HTTP {resp.status_code}")
        raise error
