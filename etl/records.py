"""실거래가 API 응답 항목을 DB 행으로 정규화한다.

항목은 rtms.parse_page가 만든 dict다. 태그는 소문자로, 값은 앞뒤 공백을 뗀 문자열로 온다.
(매매 roadNm과 전월세 roadnm처럼 API마다 태그 대소문자가 달라서 소문자로 통일한다.)
빈 값은 ''이고, 숫자에는 쉼표가 섞인다('1,234'). SPEC "API 사용 시 주의" 6 참조.

형식이 예상과 다르면 추측하지 않고 ValueError를 낸다. 그 (지역, 월)은 적재하지 않고 실패로 남는다.
"""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

AREA_QUANTUM = Decimal("0.0001")  # trade_*.area_excl NUMERIC(8,4)


def to_text(value: str) -> str | None:
    return value.strip() or None


def to_int(value: str) -> int | None:
    """'  1,234' → 1234, 빈 값 → None."""
    s = value.replace(",", "").strip()
    if not s:
        return None
    if not re.fullmatch(r"-?\d+", s):
        raise ValueError(f"정수가 아님: {value!r}")
    return int(s)


def to_required_int(value: str, field: str) -> int:
    n = to_int(value)
    if n is None:
        raise ValueError(f"{field} 값이 비어 있음")
    return n


def to_area(value: str) -> Decimal:
    """전용면적 m2. API는 소수 0~4자리('99', '84.9573')를 준다. DB와 같은 4자리로 맞춘다."""
    try:
        area = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError(f"전용면적이 숫자가 아님: {value!r}") from None
    if not area.is_finite() or area <= 0 or area.as_tuple().exponent < -4:
        raise ValueError(f"전용면적 형식 오류: {value!r}")
    return area.quantize(AREA_QUANTUM)


def to_yymmdd(value: str) -> date | None:
    """'25.07.22' → 2025-07-22 (cdealDay, rgstDate). 빈 값 → None."""
    s = value.strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", s)
    if not m:
        raise ValueError(f"YY.MM.DD 형식이 아님: {value!r}")
    return date(2000 + int(m[1]), int(m[2]), int(m[3]))


def to_code(value: str, digits: int, field: str) -> str | None:
    """zero-padding된 숫자 코드('0009'). 빈 값 → None."""
    s = value.strip()
    if not s:
        return None
    if not re.fullmatch(rf"\d{{{digits}}}", s):
        raise ValueError(f"{field}가 {digits}자리 숫자가 아님: {value!r}")
    return s


def split_jibun(jibun: str | None) -> tuple[str | None, str | None]:
    """전월세 지번 문자열 '345-4' → ('0345', '0004'). '산' 지번 등 다른 형식은 (None, None)."""
    m = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", jibun or "")
    if not m:
        return None, None
    return m[1].zfill(4), (m[2] or "0").zfill(4)


def normalize_name(name: str) -> str:
    """표시용 단지명: 전각→반각, '(주)'·'아파트'·공백·괄호 제거. 단지 식별에는 쓰지 않는다."""
    s = unicodedata.normalize("NFKC", name)  # 전각 문자와 '㈜'도 여기서 풀린다
    s = s.replace("(주)", "").replace("아파트", "")
    return re.sub(r"[\s()\[\]{}]", "", s)


def _deal_date(item: dict[str, str], deal_ymd: str) -> date:
    d = date(
        to_required_int(item["dealyear"], "dealYear"),
        to_required_int(item["dealmonth"], "dealMonth"),
        to_required_int(item["dealday"], "dealDay"),
    )
    if d.strftime("%Y%m") != deal_ymd:
        raise ValueError(f"계약일 {d}가 요청한 월 {deal_ymd} 밖")
    return d


def _check_region(item: dict[str, str], lawd_cd5: str) -> None:
    if item["sggcd"] != lawd_cd5:
        raise ValueError(f"sggCd {item['sggcd']!r}가 요청한 시군구 {lawd_cd5}와 다름")


# 필드 순서는 DB 컬럼 순서이자 src_hash 입력 순서다. 필드를 추가·삭제하면 모든 해시가 바뀌어
# 다음 수집 때 해당 구간 행이 전부 지워지고 다시 들어간다(값은 같고 id만 바뀐다).
@dataclass(frozen=True)
class SaleRow:
    lawd_cd5: str
    lawd_cd: str | None  # sggCd + umdCd
    bonbun: str | None
    bubun: str | None
    jibun: str | None
    apt_name: str | None
    apt_seq: str | None
    area_excl: Decimal
    deal_date: date
    price_manwon: int
    floor: int | None
    built_year: int | None
    dealing_type: str | None
    is_canceled: bool
    canceled_date: date | None
    registered_date: date | None


@dataclass(frozen=True)
class RentRow:
    lawd_cd5: str
    umd_nm: str | None
    jibun: str | None
    apt_name: str | None
    apt_seq: str | None
    built_year: int | None
    area_excl: Decimal
    deal_date: date
    deposit_manwon: int
    monthly_manwon: int
    floor: int | None
    contract_type: str | None
    contract_term: str | None
    renewal_right: str | None
    pre_deposit_manwon: int | None
    pre_monthly_manwon: int | None


def parse_sale(item: dict[str, str], lawd_cd5: str, deal_ymd: str) -> SaleRow:
    _check_region(item, lawd_cd5)
    umd_cd = to_code(item["umdcd"], 5, "umdCd")
    cdeal_type = item["cdealtype"]
    if cdeal_type not in ("", "O"):
        raise ValueError(f"cdealType 값 오류: {cdeal_type!r}")
    return SaleRow(
        lawd_cd5=lawd_cd5,
        lawd_cd=lawd_cd5 + umd_cd if umd_cd else None,
        bonbun=to_code(item["bonbun"], 4, "bonbun"),
        bubun=to_code(item["bubun"], 4, "bubun"),
        jibun=to_text(item["jibun"]),
        apt_name=to_text(item["aptnm"]),
        apt_seq=to_text(item["aptseq"]),
        area_excl=to_area(item["excluusear"]),
        deal_date=_deal_date(item, deal_ymd),
        price_manwon=to_required_int(item["dealamount"], "dealAmount"),
        floor=to_int(item["floor"]),
        built_year=to_int(item["buildyear"]),
        dealing_type=to_text(item["dealinggbn"]),
        is_canceled=cdeal_type == "O",
        canceled_date=to_yymmdd(item["cdealday"]),
        registered_date=to_yymmdd(item["rgstdate"]),
    )


def parse_rent(item: dict[str, str], lawd_cd5: str, deal_ymd: str) -> RentRow:
    _check_region(item, lawd_cd5)
    return RentRow(
        lawd_cd5=lawd_cd5,
        umd_nm=to_text(item["umdnm"]),
        jibun=to_text(item["jibun"]),
        apt_name=to_text(item["aptnm"]),
        apt_seq=to_text(item["aptseq"]),
        built_year=to_int(item["buildyear"]),
        area_excl=to_area(item["excluusear"]),
        deal_date=_deal_date(item, deal_ymd),
        deposit_manwon=to_required_int(item["deposit"], "deposit"),
        monthly_manwon=to_required_int(item["monthlyrent"], "monthlyRent"),
        floor=to_int(item["floor"]),
        contract_type=to_text(item["contracttype"]),
        contract_term=to_text(item["contractterm"]),
        renewal_right=to_text(item["userrright"]),
        pre_deposit_manwon=to_int(item["predeposit"]),
        pre_monthly_manwon=to_int(item["premonthlyrent"]),
    )


def src_hashes(rows: Sequence[SaleRow | RentRow]) -> list[str]:
    """행마다 src_hash를 만든다: sha256(저장하는 모든 값 + 같은 값인 행 사이의 순번).

    rows는 한 (종류, 지역, 월) 응답 전체여야 한다. 값이 모두 같은 행은 DB에서 서로 구분할 수 없으므로
    어느 행이 몇 번째 순번을 받든 결과가 같다. 그래서 응답 순서가 바뀌어도 해시 집합은 그대로다.
    """
    seen: Counter[str] = Counter()
    hashes = []
    for row in rows:
        payload = json.dumps(astuple(row), ensure_ascii=False, default=str)
        hashes.append(hashlib.sha256(f"{payload}#{seen[payload]}".encode()).hexdigest())
        seen[payload] += 1
    return hashes
