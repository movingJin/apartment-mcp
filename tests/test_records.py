import random
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from etl.records import (
    RentRow,
    SaleRow,
    normalize_name,
    parse_rent,
    parse_sale,
    split_jibun,
    src_hashes,
    to_area,
    to_int,
    to_yymmdd,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  1,234", 1234), ("88,000", 88000), ("0", 0), ("-1", -1), ("", None), (" ", None)],
)
def test_to_int(value, expected):
    assert to_int(value) == expected


@pytest.mark.parametrize("value", ["12a", "1.5", "--1"])
def test_to_int_rejects(value):
    with pytest.raises(ValueError):
        to_int(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("99", "99.0000"), ("84.9573", "84.9573"), ("37.969", "37.9690"), (" 60 ", "60.0000")],
)
def test_to_area_keeps_four_decimals(value, expected):
    assert to_area(value) == Decimal(expected)
    assert str(to_area(value)) == expected


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", "84.95731", "NaN"])
def test_to_area_rejects(value):
    with pytest.raises(ValueError):
        to_area(value)


def test_to_yymmdd():
    assert to_yymmdd("25.07.22") == date(2025, 7, 22)
    assert to_yymmdd("") is None


@pytest.mark.parametrize("value", ["2025-07-22", "25.7.22", "25.13.01"])
def test_to_yymmdd_rejects(value):
    with pytest.raises(ValueError):
        to_yymmdd(value)


@pytest.mark.parametrize(
    ("jibun", "expected"),
    [
        ("345-4", ("0345", "0004")),
        ("883", ("0883", "0000")),
        ("산12-3", (None, None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_jibun(jibun, expected):
    assert split_jibun(jibun) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DMC파크뷰자이2단지(임대)", "DMC파크뷰자이2단지임대"),
        ("홍은 벽산 아파트", "홍은벽산"),
        ("ＤＭＣ한양", "DMC한양"),  # 전각
        ("㈜한양", "한양"),
        ("(주)한양아파트", "한양"),
    ],
)
def test_normalize_name(name, expected):
    assert normalize_name(name) == expected


# --- 실제 응답(서대문구 2025-06) ---


def test_parse_sale_fixture(sale_items):
    rows = [parse_sale(item, "11410", "202506") for item in sale_items]
    assert len(rows) == 578
    canceled = [r for r in rows if r.is_canceled]
    assert len(canceled) == 77
    assert all(r.canceled_date and r.registered_date is None for r in canceled)
    assert Counter(r.dealing_type for r in rows) == {"중개거래": 564, "직거래": 14}
    assert all(r.lawd_cd and r.lawd_cd.startswith("11410") and len(r.lawd_cd) == 10 for r in rows)

    # 해제된 원 신고와 같은 조건의 재신고 (PROGRESS 참고 수치)
    pair = [r for r in rows if r.apt_seq == "11410-104" and r.deal_date == date(2025, 6, 21) and r.floor == 3]
    base = dict(
        lawd_cd5="11410", lawd_cd="1141012000", bonbun="0376", bubun="0000", jibun="376",
        apt_name="남가좌동현대", apt_seq="11410-104", area_excl=Decimal("84.78"),
        deal_date=date(2025, 6, 21), price_manwon=88000, floor=3, built_year=1999, dealing_type="중개거래",
    )
    assert sorted(pair, key=lambda r: r.is_canceled) == [
        SaleRow(**base, is_canceled=False, canceled_date=None, registered_date=date(2025, 11, 28)),
        SaleRow(**base, is_canceled=True, canceled_date=date(2025, 10, 30), registered_date=None),
    ]


def test_parse_rent_fixture(rent_items):
    rows = [parse_rent(item, "11410", "202506") for item in rent_items]
    assert len(rows) == 639
    assert rows[0] == RentRow(
        lawd_cd5="11410", umd_nm="홍제동", jibun="474", apt_name="홍제역해링턴플레이스", apt_seq="11410-4894",
        built_year=2024, area_excl=Decimal("84.95"), deal_date=date(2025, 6, 28),
        deposit_manwon=60000, monthly_manwon=0, floor=13, contract_type=None, contract_term=None,
        renewal_right=None, pre_deposit_manwon=None, pre_monthly_manwon=None,
    )
    assert Counter(r.contract_type for r in rows) == {"신규": 361, "갱신": 232, None: 46}
    assert Counter(r.renewal_right for r in rows) == {None: 498, "사용": 141}
    assert sum(r.pre_deposit_manwon is not None for r in rows) == 639 - 408
    assert all(r.monthly_manwon >= 0 for r in rows)


def test_parse_rejects_other_region(sale_items):
    with pytest.raises(ValueError, match="sggCd"):
        parse_sale(sale_items[0], "11440", "202506")


def test_parse_rejects_deal_outside_month(sale_items):
    with pytest.raises(ValueError, match="요청한 월"):
        parse_sale(sale_items[0], "11410", "202507")


def test_parse_rejects_unknown_cancel_flag(sale_items):
    with pytest.raises(ValueError, match="cdealType"):
        parse_sale({**sale_items[0], "cdealtype": "X"}, "11410", "202506")


def test_parse_rejects_missing_price(sale_items, rent_items):
    with pytest.raises(ValueError, match="dealAmount"):
        parse_sale({**sale_items[0], "dealamount": ""}, "11410", "202506")
    with pytest.raises(ValueError, match="deposit"):
        parse_rent({**rent_items[0], "deposit": ""}, "11410", "202506")


# --- src_hash ---


def test_src_hashes_unique_within_slice(sale_items, rent_items):
    # 저장하는 값이 모두 같은 행: 매매 1쌍(독립문삼호, 둘 다 해제), 전월세 30그룹 61행
    sale = [parse_sale(item, "11410", "202506") for item in sale_items]
    rent = [parse_rent(item, "11410", "202506") for item in rent_items]
    assert sum(n - 1 for n in Counter(sale).values()) == 1
    assert sum(n - 1 for n in Counter(rent).values()) == 61 - 30
    assert len(set(src_hashes(sale))) == 578
    assert len(set(src_hashes(rent))) == 639


def test_src_hashes_ignore_response_order(rent_items):
    rows = [parse_rent(item, "11410", "202506") for item in rent_items]
    shuffled = rows[:]
    random.Random(0).shuffle(shuffled)
    assert sorted(src_hashes(rows)) == sorted(src_hashes(shuffled))


def test_src_hashes_change_only_for_changed_row(sale_items):
    rows = [parse_sale(item, "11410", "202506") for item in sale_items]
    counts = Counter(rows)
    target = next(i for i, r in enumerate(rows) if not r.is_canceled and counts[r] == 1)
    changed = rows[:]
    changed[target] = replace(rows[target], is_canceled=True, canceled_date=date(2025, 10, 1), registered_date=None)
    before, after = src_hashes(rows), src_hashes(changed)
    assert [i for i in range(len(rows)) if before[i] != after[i]] == [target]
