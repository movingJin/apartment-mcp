"""국토부 실거래가 API(apis.data.go.kr/1613000) 호출.

(종류, 시군구 5자리, 계약년월) 하나를 마지막 페이지까지 받아 항목 목록으로 돌려준다.
간격 제한·재시도·한도 초과 구분은 etl.datagokr가 한다.
받은 항목 수가 totalCount와 다르면 실패로 본다. 적재 단계가 응답에 없는 행을 지우므로
일부만 받은 응답으로 적재하면 멀쩡한 행이 지워진다.
"""

from etl.datagokr import ApiError, DataGoKrClient, Page, QuotaExceeded, RetryableStatus
from etl.datagokr import parse_page as _parse_page

__all__ = ["ApiError", "Page", "QuotaExceeded", "RetryableStatus", "RtmsClient", "parse_page"]

ENDPOINTS = {
    "sale": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "rent": "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
}
PAGE_SIZE = 1000
RESULT_OK = "000"


def parse_page(content: bytes) -> Page:
    return _parse_page(content, RESULT_OK)


class RtmsClient(DataGoKrClient):
    def fetch(self, kind: str, lawd_cd5: str, deal_ymd: str) -> list[dict[str, str]]:
        return self._fetch_all(
            ENDPOINTS[kind], {"LAWD_CD": lawd_cd5, "DEAL_YMD": deal_ymd}, ok_code=RESULT_OK, page_size=PAGE_SIZE
        )
