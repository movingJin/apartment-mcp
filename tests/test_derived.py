"""Phase 2 파생 지표 뷰(v_*). 가상 시군구에 가상 단지·거래를 넣고 롤백 트랜잭션 안에서 확인한다.

mv_*는 v_*의 스냅숏이라 v_*를 단지·시군구로 좁혀 조회한다. 기준일은 apartment.as_of로 2026-09-25에 고정한다
(최근 6개월 = 2026-03-01 ~ 2026-09-25, partial = 2026-07 이후).
"""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest

from etl.db import connect
from etl.refresh import MATERIALIZED_VIEWS

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")

REGION = "99999"  # 실제 시군구와 겹치지 않는 가상 코드


@pytest.fixture
def conn():
    with connect(autocommit=True) as c:
        with c.transaction(force_rollback=True):
            c.execute("SET LOCAL apartment.as_of = '2026-09-25'")
            yield c


def add_danji(conn, apt_seq: str, region: str = REGION) -> int:
    return conn.execute(
        "INSERT INTO danji (apt_seq, lawd_cd5, jibun, name, name_norm) VALUES (%s, %s, '1', %s, %s) RETURNING danji_id",
        (apt_seq, region, apt_seq, apt_seq),
    ).fetchone()[0]


def add_sale(conn, danji_id, deal_date, price, area=84.0, dealing="중개거래", canceled=False, floor=5, region=REGION):
    conn.execute(
        """
        INSERT INTO trade_sale (danji_id, lawd_cd5, area_excl, deal_date, price_manwon, floor, dealing_type,
                                is_canceled, src_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (danji_id, region, area, deal_date, price, floor, dealing, canceled, uuid.uuid4().hex),
    )


def add_rent(conn, danji_id, deal_date, deposit, monthly=0, area=84.0, contract="신규", region=REGION):
    conn.execute(
        """
        INSERT INTO trade_rent (danji_id, lawd_cd5, area_excl, deal_date, deposit_manwon, monthly_manwon,
                                contract_type, src_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (danji_id, region, area, deal_date, deposit, monthly, contract, uuid.uuid4().hex),
    )


def one(conn, query, params=()):
    cur = conn.execute(query, params)
    names = [c.name for c in cur.description]
    rows = [dict(zip(names, r)) for r in cur.fetchall()]
    assert len(rows) == 1, rows
    return rows[0]


def test_every_mv_is_refreshed_by_etl_refresh(conn):
    names = {n for (n,) in conn.execute("SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'")}
    assert names == set(MATERIALIZED_VIEWS)


def test_as_of_date_defaults_to_korean_today():
    with connect() as c:  # apartment.as_of 설정이 없는 세션
        assert c.execute("SELECT as_of_date() = (now() AT TIME ZONE 'Asia/Seoul')::date").fetchone()[0]


def test_canceled_trade_is_excluded(conn):
    d = add_danji(conn, "T-cancel")
    for price in (50000, 52000, 54000):
        add_sale(conn, d, date(2026, 5, 10), price)
    add_sale(conn, d, date(2026, 5, 20), 90000, canceled=True)  # 신고가 취소

    m = one(conn, "SELECT * FROM v_danji_price_monthly WHERE danji_id = %s", (d,))
    assert (m["sample_size"], m["median_price_manwon"], m["max_price_manwon"]) == (3, 52000, 54000)
    latest = one(conn, "SELECT * FROM v_danji_latest WHERE danji_id = %s", (d,))
    assert (latest["sample_size"], latest["max_price_manwon"]) == (3, 54000)
    assert conn.execute(
        "SELECT is_basis FROM v_sale_basis WHERE danji_id = %s AND is_canceled", (d,)
    ).fetchone() == (False,)


def test_area_groups_are_not_mixed(conn):
    d = add_danji(conn, "T-mixed")  # 전용 35m2와 130m2가 함께 있는 단지
    for price in (20000, 21000, 22000):
        add_sale(conn, d, date(2026, 4, 5), price, area=35.0)
    for price in (90000, 95000, 100000):
        add_sale(conn, d, date(2026, 4, 6), price, area=130.0)

    rows = conn.execute(
        "SELECT area_group, sample_size, median_price_manwon FROM v_danji_price_monthly WHERE danji_id = %s ORDER BY 1",
        (d,),
    ).fetchall()
    assert rows == [("XL", 3, 95000), ("XS", 3, 21000)]


def test_two_trades_are_low_confidence(conn):
    d = add_danji(conn, "T-thin")
    add_sale(conn, d, date(2026, 5, 1), 50000)
    add_sale(conn, d, date(2026, 6, 1), 51000)
    latest = one(conn, "SELECT * FROM v_danji_latest WHERE danji_id = %s", (d,))
    assert (latest["sample_size"], latest["low_confidence"]) == (2, True)
    assert latest["jeonse_sample_size"] == 0 and latest["jeonse_ratio"] is None


def test_recent_two_months_are_partial(conn):
    d = add_danji(conn, "T-partial")
    add_sale(conn, d, date(2026, 6, 30), 50000)
    add_sale(conn, d, date(2026, 7, 1), 50000)
    rows = conn.execute(
        "SELECT month, data_completeness FROM v_danji_price_monthly WHERE danji_id = %s ORDER BY 1", (d,)
    ).fetchall()
    assert rows == [(date(2026, 6, 1), "complete"), (date(2026, 7, 1), "partial")]
    assert one(conn, "SELECT data_completeness FROM v_danji_latest WHERE danji_id = %s", (d,)) == {
        "data_completeness": "partial"
    }
    # 기준일이 10월로 넘어가면 7월은 complete, 8월부터 partial
    conn.execute("SET LOCAL apartment.as_of = '2026-10-01'")
    assert conn.execute(
        "SELECT data_completeness_of('2026-07-01'), data_completeness_of('2026-08-01')"
    ).fetchone() == ("complete", "partial")


def test_direct_deals_and_outliers_are_not_market_price(conn):
    d = add_danji(conn, "T-outlier")
    for price in (100000, 101000, 99000, 100500, 99500):  # 100m2, m2당 약 1,000
        add_sale(conn, d, date(2025, 3, 1), price, area=100.0)
    add_sale(conn, d, date(2025, 3, 2), 40000, area=100.0)  # 중위의 40%: 이상치
    add_sale(conn, d, date(2025, 3, 3), 70000, area=100.0, dealing="직거래")
    add_sale(conn, d, date(2025, 3, 4), 100000, area=100.0, dealing=None)  # 거래유형 빈 값은 포함

    flags = conn.execute(
        """
        SELECT price_manwon, dealing_type, is_direct, is_outlier, is_basis
        FROM v_sale_basis WHERE danji_id = %s AND price_manwon IN (40000, 70000) OR danji_id = %s AND dealing_type IS NULL
        ORDER BY price_manwon, dealing_type NULLS FIRST
        """,
        (d, d),
    ).fetchall()
    assert flags == [
        (40000, "중개거래", False, True, False),
        (70000, "직거래", True, False, False),
        (100000, None, False, False, True),
    ]
    m = one(conn, "SELECT * FROM v_danji_price_monthly WHERE danji_id = %s", (d,))
    assert (m["sample_size"], m["min_price_manwon"]) == (6, 99000)


def test_outlier_needs_five_trades_in_cell(conn):
    d = add_danji(conn, "T-few")
    for price in (100000, 101000, 99000):
        add_sale(conn, d, date(2025, 3, 1), price, area=100.0)
    add_sale(conn, d, date(2025, 3, 2), 40000, area=100.0)  # 4건뿐이라 판정하지 않는다
    assert conn.execute(
        "SELECT bool_or(is_outlier), count(*) FILTER (WHERE is_basis) FROM v_sale_basis WHERE danji_id = %s", (d,)
    ).fetchone() == (False, 4)


def test_danji_without_market_sale_is_excluded(conn):
    d = add_danji(conn, "T-rental")  # 사업자 일괄 직거래 매각만 있고 전월세가 많은 단지
    add_sale(conn, d, date(2026, 5, 1), 30000, dealing="직거래")
    add_sale(conn, d, date(2026, 5, 2), 50000, canceled=True)
    for deposit in (20000, 21000, 22000):
        add_rent(conn, d, date(2026, 5, 3), deposit)

    st = one(conn, "SELECT scope, scope_reason, market_sale_count FROM v_danji_status WHERE danji_id = %s", (d,))
    assert st == {"scope": "excluded", "scope_reason": "no_market_sale", "market_sale_count": 0}
    assert conn.execute(
        "SELECT bool_or(is_basis) FROM v_rent_basis WHERE danji_id = %s", (d,)
    ).fetchone() == (False,)
    assert conn.execute("SELECT count(*) FROM v_danji_latest WHERE danji_id = %s", (d,)).fetchone() == (0,)


def test_jeonse_uses_new_contracts_and_ratio_per_m2(conn):
    d = add_danji(conn, "T-jeonse")
    for price in (84000, 84000, 84000):  # 84m2, m2당 1,000
        add_sale(conn, d, date(2026, 5, 1), price, area=84.0)
    for deposit, contract in ((45000, "신규"), (46875, None), (48750, "신규")):  # 같은 M 그룹 75m2, m2당 600~650
        add_rent(conn, d, date(2026, 5, 2), deposit, area=75.0, contract=contract)
    add_rent(conn, d, date(2026, 5, 3), 30000, area=75.0, contract="갱신")
    add_rent(conn, d, date(2026, 5, 4), 10000, monthly=100, area=75.0)  # 월세

    latest = one(conn, "SELECT * FROM v_danji_latest WHERE danji_id = %s", (d,))
    assert (latest["jeonse_sample_size"], latest["median_jeonse_manwon"]) == (3, 46875)
    # 총액끼리 나누면 55.8%지만 면적이 달라 m2당으로 나눈다: 625 / 1,000
    assert latest["jeonse_ratio"] == Decimal("62.5")


def test_sale_conversion_drops_rents_before_first_market_sale(conn):
    d = add_danji(conn, "T-convert")
    add_rent(conn, d, date(2026, 3, 10), 10000)  # 전환 전 규제 임대료
    add_sale(conn, d, date(2026, 4, 15), 60000, dealing="직거래")  # 직거래는 전환일 계산에 안 쓴다
    add_sale(conn, d, date(2026, 5, 1), 80000)
    add_rent(conn, d, date(2026, 5, 20), 40000)
    conn.execute(
        "INSERT INTO danji_flag (apt_seq, flag, note, reviewed_on) VALUES ('T-convert', 'sale_conversion', 'test', '2026-09-25')"
    )
    st = one(conn, "SELECT scope, rent_basis_from FROM v_danji_status WHERE danji_id = %s", (d,))
    assert st == {"scope": "full", "rent_basis_from": date(2026, 5, 1)}
    rows = conn.execute(
        "SELECT deposit_manwon, is_before_conversion, is_jeonse_basis FROM v_rent_basis WHERE danji_id = %s ORDER BY 1",
        (d,),
    ).fetchall()
    assert rows == [(10000, True, False), (40000, False, True)]

    conn.execute("UPDATE danji_flag SET converted_on = '2026-03-01' WHERE apt_seq = 'T-convert'")
    assert one(conn, "SELECT jeonse_sample_size FROM v_danji_latest WHERE danji_id = %s", (d,)) == {
        "jeonse_sample_size": 2
    }


def test_region_comparisons_skip_land_lease(conn):
    """분위·회복률·지역 통계. 시군구 단위 뷰는 전체를 계산해 느리다(약 1분)."""
    region = "99998"
    ids = {}
    # 이름: (최근 m2당가, 2021Q3 고점 m2당가, 2023Q1 m2당가)
    plan = {"A": (1000, 1200, 900), "B": (1500, 1500, 1200), "C": (2000, 2000, 1500), "L": (500, 600, 450)}
    for name, (now, peak, trough) in plan.items():
        ids[name] = d = add_danji(conn, f"T-region-{name}", region=region)
        for day in (1, 2, 3):
            add_sale(conn, d, date(2026, 5, day), now * 100, area=100.0, region=region)
            add_sale(conn, d, date(2021, 8, day), peak * 100, area=100.0, region=region)
            add_sale(conn, d, date(2023, 2, day), trough * 100, area=100.0, region=region)
        add_sale(conn, d, date(2022, 1, 5), peak * 100, area=100.0, region=region)  # 3건 미만 분기는 쓰지 않는다
    conn.execute(
        "INSERT INTO danji_flag (apt_seq, flag, note, reviewed_on) VALUES ('T-region-L', 'land_lease', 'test', '2026-09-25')"
    )
    by_id = {v: k for k, v in ids.items()}

    pct = {
        by_id[r[0]]: r[1:]
        for r in conn.execute(
            "SELECT danji_id, price_percentile, group_danji_count FROM v_danji_percentile WHERE lawd_cd5 = %s", (region,)
        )
    }
    assert pct == {"A": (0, 3), "B": (50, 3), "C": (100, 3)}  # 토지임대부 L은 비교에서 빠진다

    cur = conn.execute("SELECT * FROM v_danji_recovery WHERE lawd_cd5 = %s", (region,))
    names = [c.name for c in cur.description]
    rec = {by_id[r[0]]: dict(zip(names, r)) for r in cur.fetchall()}
    a = rec["A"]
    assert (a["peak_quarter"], a["peak_sample_size"], a["peak_price_per_m2"]) == (date(2021, 7, 1), 3, 1200)
    assert (a["recovery_pct"], a["trough_quarter"], a["trough_pct"]) == (Decimal("83.3"), date(2023, 1, 1), 75)
    assert (a["region_danji_count"], a["region_median_recovery_pct"]) == (3, 100)
    assert (a["recovery_vs_region_pp"], a["recovery_percentile"]) == (Decimal("-16.7"), 0)
    assert rec["C"]["recovery_percentile"] == 50  # B와 C는 둘 다 100.0으로 같다
    lease = rec["L"]
    assert (lease["scope"], lease["recovery_pct"]) == ("danji_only", Decimal("83.3"))
    assert (lease["recovery_vs_region_pp"], lease["recovery_percentile"]) == (None, None)

    region_may = one(
        conn, "SELECT * FROM v_region_monthly WHERE lawd_cd5 = %s AND month = '2026-05-01'", (region,)
    )
    assert (region_may["sample_size"], region_may["danji_count"], region_may["median_price_per_m2"]) == (9, 3, 1500)
