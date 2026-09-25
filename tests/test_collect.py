import os
import random
from datetime import date, datetime, timezone

import pytest

from etl.collect import (
    EmptyResponseError,
    Slice,
    add_months,
    collect_all,
    current_ym,
    load_slice,
    month_range,
    months_between,
    parse_ym,
    plan_slices,
    recent_months,
    record_error,
    refresh_danji_locations,
)
from etl.db import connect
from etl.rtms import ApiError, QuotaExceeded

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")

REGION, YM = "11410", "202506"


def test_month_range_rolls_over_year():
    assert month_range("202506") == (date(2025, 6, 1), date(2025, 7, 1))
    assert month_range("202512") == (date(2025, 12, 1), date(2026, 1, 1))


def test_months_between():
    assert months_between("202511", "202602") == ["202511", "202512", "202601", "202602"]
    assert months_between("202506", "202506") == ["202506"]
    assert months_between("202507", "202506") == []


@pytest.mark.parametrize(("value", "expected"), [("2025-06", "202506"), ("202506", "202506")])
def test_parse_ym(value, expected):
    assert parse_ym(value) == expected


def test_add_months():
    assert add_months("202609", -2) == "202607"
    assert add_months("202601", -1) == "202512"
    assert add_months("202512", 1) == "202601"


def test_current_ym_is_korean_time():
    # UTC 9월 30일 16시 = 한국 10월 1일 01시
    assert current_ym(datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)) == "202610"
    assert current_ym(datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc)) == "202609"


def test_recent_months_includes_current_month():
    assert recent_months("202609") == ["202607", "202608", "202609"]
    assert recent_months("202601") == ["202511", "202512", "202601"]


def test_recent_months_longer_window():
    months = recent_months("202609", 12)
    assert len(months) == 12
    assert (months[0], months[-1]) == ("202510", "202609")


def test_plan_skips_ok_slices_outside_recent_window():
    months = ["202605", "202606", "202607"]
    done = {Slice("sale", "11410", "202605"), Slice("sale", "11410", "202607"), Slice("rent", "11440", "202606")}
    plan = plan_slices(["sale", "rent"], ["11410", "11440"], months, done, ["202607"], force=False)
    assert Slice("sale", "11410", "202605") not in plan  # 'ok' → 건너뜀
    assert Slice("rent", "11440", "202606") not in plan
    assert Slice("sale", "11410", "202607") in plan  # 재수집 창이라 'ok'여도 받음
    assert len(plan) == 2 * 2 * 3 - 2
    # 순서: 시군구 → 월 → 종류
    assert plan[:3] == [Slice("rent", "11410", "202605"), Slice("sale", "11410", "202606"), Slice("rent", "11410", "202606")]


def test_plan_force_takes_everything():
    done = {Slice("sale", "11410", "202605")}
    assert len(plan_slices(["sale"], ["11410"], ["202605"], done, [], force=True)) == 1


# --- DB: 모든 변경은 테스트가 끝나면 롤백된다 ---


@pytest.fixture
def conn():
    with connect(autocommit=True) as c:
        with c.transaction(force_rollback=True):
            # 개발 DB에 실제로 수집해 둔 같은 구간이 있을 수 있어 트랜잭션 안에서 먼저 비운다
            for table in ("trade_sale", "trade_rent"):
                c.execute(
                    f"DELETE FROM {table} WHERE lawd_cd5 = %s AND deal_date >= %s AND deal_date < %s",
                    (REGION, *month_range(YM)),
                )
            c.execute("DELETE FROM ingest_log WHERE lawd_cd5 = %s AND deal_ymd = %s", (REGION, YM))
            yield c


def slice_ids(conn, table: str) -> dict[str, int]:
    return dict(
        conn.execute(
            f"SELECT src_hash, id FROM {table} WHERE lawd_cd5 = %s AND deal_date >= %s AND deal_date < %s",
            (REGION, *month_range(YM)),
        )
    )


@needs_db
def test_load_is_idempotent(conn, sale_items, rent_items):
    r = load_slice(conn, "sale", REGION, YM, sale_items)
    assert (r.fetched, r.inserted, r.deleted) == (578, 578, 0)
    r = load_slice(conn, "rent", REGION, YM, rent_items)
    assert (r.fetched, r.inserted, r.deleted) == (639, 639, 0)
    sale_before, rent_before = slice_ids(conn, "trade_sale"), slice_ids(conn, "trade_rent")

    # 두 번째 적재: 순서가 달라도 바뀌는 행이 없다
    random.Random(0).shuffle(sale_items)
    r = load_slice(conn, "sale", REGION, YM, sale_items)
    assert (r.inserted, r.deleted, r.unchanged) == (0, 0, 578)
    r = load_slice(conn, "rent", REGION, YM, rent_items)
    assert (r.inserted, r.deleted, r.unchanged) == (0, 0, 639)
    assert slice_ids(conn, "trade_sale") == sale_before
    assert slice_ids(conn, "trade_rent") == rent_before


@needs_db
def test_canceled_original_and_rereport_both_kept(conn, sale_items):
    load_slice(conn, "sale", REGION, YM, sale_items)
    start, end = month_range(YM)
    assert conn.execute(
        "SELECT count(*) FILTER (WHERE is_canceled), count(*) FROM trade_sale"
        " WHERE lawd_cd5 = %s AND deal_date >= %s AND deal_date < %s",
        (REGION, start, end),
    ).fetchone() == (77, 578)
    pair = conn.execute(
        "SELECT is_canceled, canceled_date, registered_date, area_group FROM trade_sale"
        " WHERE apt_seq = '11410-104' AND deal_date = '2025-06-21' AND floor = 3 AND price_manwon = 88000"
        " ORDER BY is_canceled"
    ).fetchall()
    assert pair == [(False, None, date(2025, 11, 28), "M"), (True, date(2025, 10, 30), None, "M")]


@needs_db
def test_changed_row_is_replaced_and_others_keep_ids(conn, sale_items):
    load_slice(conn, "sale", REGION, YM, sale_items)
    before = slice_ids(conn, "trade_sale")

    # 등기 전 정상 거래가 나중에 해제된 경우. fixture에는 신촌럭키 2025-06-13 한 건이 있다
    i = next(i for i, it in enumerate(sale_items) if it["cdealtype"] == "" and it["rgstdate"] == "")
    sale_items[i] = {**sale_items[i], "cdealtype": "O", "cdealday": "25.10.01"}
    r = load_slice(conn, "sale", REGION, YM, sale_items)
    assert (r.inserted, r.deleted) == (1, 1)

    after = slice_ids(conn, "trade_sale")
    kept = before.keys() & after.keys()
    assert len(kept) == 577
    assert all(before[h] == after[h] for h in kept)


@needs_db
def test_row_missing_from_response_is_deleted(conn, sale_items):
    load_slice(conn, "sale", REGION, YM, sale_items)
    r = load_slice(conn, "sale", REGION, YM, sale_items[1:])
    assert (r.inserted, r.deleted) == (0, 1)
    assert len(slice_ids(conn, "trade_sale")) == 577


@needs_db
def test_empty_response_does_not_wipe_slice(conn, sale_items):
    load_slice(conn, "sale", REGION, YM, sale_items)
    with pytest.raises(EmptyResponseError):
        load_slice(conn, "sale", REGION, YM, [])
    assert len(slice_ids(conn, "trade_sale")) == 578


@needs_db
def test_parse_error_rolls_back_whole_slice(conn, sale_items):
    sale_items[-1] = {**sale_items[-1], "dealamount": ""}
    with pytest.raises(ValueError):
        load_slice(conn, "sale", REGION, YM, sale_items)
    assert slice_ids(conn, "trade_sale") == {}


@needs_db
def test_one_lot_with_several_danji(conn, sale_items, rent_items):
    """남가좌동 385 = DMC파크뷰자이 1~5단지 + 2단지(임대). 지번이 같아도 aptSeq마다 다른 단지다."""
    load_slice(conn, "sale", REGION, YM, sale_items)
    load_slice(conn, "rent", REGION, YM, rent_items)
    rows = conn.execute(
        """
        SELECT DISTINCT d.apt_seq, d.danji_id, d.lawd_cd, d.bonbun, d.bubun
        FROM danji d
        JOIN (SELECT danji_id FROM trade_sale
              WHERE lawd_cd5 = %(r)s AND deal_date >= %(s)s AND deal_date < %(e)s
                AND lawd_cd = '1141012000' AND jibun = '385'
              UNION
              SELECT danji_id FROM trade_rent
              WHERE lawd_cd5 = %(r)s AND deal_date >= %(s)s AND deal_date < %(e)s
                AND umd_nm = '남가좌동' AND jibun = '385') t USING (danji_id)
        ORDER BY 1
        """,
        dict(zip("rse", (REGION, *month_range(YM)))),
    ).fetchall()
    assert [r[0] for r in rows] == ["11410-4479", "11410-4480", "11410-4481", "11410-4482", "11410-4483", "11410-4509"]
    assert len({r[1] for r in rows}) == 6
    assert {r[2:] for r in rows} == {("1141012000", "0385", "0000")}


@needs_db
def test_rent_rows_link_to_danji_with_location(conn, sale_items, rent_items):
    """전월세 행은 aptSeq로 단지에 붙는다. 전월세에만 나온 단지도 읍면동 이름으로 법정동코드를 얻는다."""
    load_slice(conn, "sale", REGION, YM, sale_items)
    load_slice(conn, "rent", REGION, YM, rent_items)
    start, end = month_range(YM)
    unlinked, wrong_location = conn.execute(
        """
        SELECT count(*) FILTER (WHERE d.danji_id IS NULL),
               count(*) FILTER (WHERE d.lawd_cd IS DISTINCT FROM l.lawd_cd)
        FROM trade_rent r
        LEFT JOIN danji d ON d.danji_id = r.danji_id AND d.apt_seq = r.apt_seq
        LEFT JOIN lawd l ON l.lawd_cd5 = r.lawd_cd5 AND l.dong = r.umd_nm AND l.is_active
        WHERE r.lawd_cd5 = %s AND r.deal_date >= %s AND r.deal_date < %s
        """,
        (REGION, start, end),
    ).fetchone()
    assert (unlinked, wrong_location) == (0, 0)


@needs_db
def test_ingest_log(conn, sale_items):
    load_slice(conn, "sale", REGION, YM, sale_items)
    query = "SELECT status, row_count, error_msg FROM ingest_log WHERE kind = 'sale' AND lawd_cd5 = %s AND deal_ymd = %s"
    assert conn.execute(query, (REGION, YM)).fetchone() == ("ok", 578, None)

    record_error(conn, "sale", REGION, YM, "ApiError: boom")
    assert conn.execute(query, (REGION, YM)).fetchone() == ("error", None, "ApiError: boom")
    assert len(slice_ids(conn, "trade_sale")) == 578  # 실패 기록은 이전 데이터를 건드리지 않는다


class FakeClient:
    """fail(slice)가 예외를 돌려주면 그 구간 호출은 실패한다. 성공하면 0건 응답."""

    def __init__(self, fail):
        self.fail = fail
        self.calls: list[Slice] = []

    def fetch(self, kind, lawd_cd5, deal_ymd):
        s = Slice(kind, lawd_cd5, deal_ymd)
        self.calls.append(s)
        if error := self.fail(s):
            raise error
        return []

    def redact(self, text):
        return text


FAKE_REGION = "99999"  # 실제 데이터와 겹치지 않는 코드
FAKE_MONTHS = months_between("202401", "202408")


@needs_db
def test_consecutive_failures_stop_only_that_kind(conn):
    slices = plan_slices(["sale", "rent"], [FAKE_REGION], FAKE_MONTHS, set(), [], force=False)
    client = FakeClient(lambda s: ApiError("HTTP 503") if s.kind == "sale" else None)
    summary = collect_all(conn, client, slices)

    assert [s.kind for s in client.calls].count("sale") == 5
    assert [s.kind for s in client.calls].count("rent") == 8  # 다른 종류는 계속한다
    assert summary.stopped == {"sale": "연속 5구간 실패"}
    assert (summary.ok, len(summary.failures), summary.not_attempted) == (8, 5, 3)
    statuses = dict(
        conn.execute(
            "SELECT kind || deal_ymd, status FROM ingest_log WHERE lawd_cd5 = %s", (FAKE_REGION,)
        ).fetchall()
    )
    assert statuses["sale202405"] == "error"
    assert "sale202406" not in statuses  # 호출하지 않은 구간은 기록도 없다 → 다음 실행에서 받는다


@needs_db
def test_success_resets_failure_streak(conn):
    slices = plan_slices(["sale"], [FAKE_REGION], FAKE_MONTHS, set(), [], force=False)
    fail_months = {"202401", "202402", "202403", "202404", "202406", "202407", "202408"}
    client = FakeClient(lambda s: ApiError("boom") if s.deal_ymd in fail_months else None)
    summary = collect_all(conn, client, slices)
    assert len(client.calls) == 8
    assert summary.stopped == {}


@needs_db
def test_quota_exceeded_stops_that_kind_immediately(conn):
    slices = plan_slices(["sale", "rent"], [FAKE_REGION], FAKE_MONTHS, set(), [], force=False)
    client = FakeClient(lambda s: QuotaExceeded("resultCode '22'") if s.kind == "rent" else None)
    summary = collect_all(conn, client, slices)
    assert [s.kind for s in client.calls].count("rent") == 1
    assert summary.stopped == {"rent": "일일 한도 초과"}
    assert (summary.ok, summary.not_attempted) == (8, 7)


LOCATION = "SELECT lawd_cd5, lawd_cd, bonbun, bubun, jibun FROM danji WHERE danji_id = %s"


@needs_db
def test_misfiled_row_does_not_move_danji(conn, sale_items):
    """원본에 시군구 코드가 잘못 붙은 행이 있어도 단지 위치는 매매 행 최빈값으로 정해진다(적재 순서 무관)."""
    item = next(it for it in sale_items if it["aptseq"] == "11410-104")  # 남가좌동현대
    expected = ("11410", "11410" + item["umdcd"], item["bonbun"], item["bubun"], item["jibun"])
    danji_id = conn.execute("SELECT danji_id FROM danji WHERE apt_seq = '11410-104'").fetchone()[0]
    conn.execute(
        "UPDATE danji SET lawd_cd5 = '99999', lawd_cd = '9999910100', bonbun = '0001', bubun = '0000', jibun = '1'"
        " WHERE danji_id = %s",
        (danji_id,),
    )

    # 잘못 붙은 1건(다른 시군구 응답)과 원래 구간을 적재해도 upsert는 기존 단지 위치를 덮어쓰지 않는다
    load_slice(conn, "sale", FAKE_REGION, YM, [{**item, "sggcd": FAKE_REGION}])
    load_slice(conn, "sale", REGION, YM, sale_items)
    assert conn.execute(LOCATION, (danji_id,)).fetchone()[0] == "99999"

    assert refresh_danji_locations(conn, [danji_id]) == ["11410-104"]
    assert conn.execute(LOCATION, (danji_id,)).fetchone() == expected
    assert refresh_danji_locations(conn, [danji_id]) == []  # 다시 돌려도 바뀌지 않는다


@needs_db
def test_rent_only_danji_location_from_rent_rows(conn, rent_items):
    """매매가 없는 단지는 전월세 행의 읍면동 이름·지번 최빈값으로 위치를 정한다."""
    load_slice(conn, "rent", REGION, YM, rent_items)
    danji_id, umd_nm, jibun = conn.execute(
        """
        SELECT r.danji_id, min(r.umd_nm), min(r.jibun) FROM trade_rent r
        WHERE r.lawd_cd5 = %s AND r.jibun ~ '^[0-9]+-[0-9]+$'
          AND NOT EXISTS (SELECT 1 FROM trade_sale s WHERE s.danji_id = r.danji_id)
        GROUP BY 1 HAVING count(DISTINCT (r.umd_nm, r.jibun)) = 1 ORDER BY 1 LIMIT 1
        """,
        (REGION,),
    ).fetchone()
    lawd_cd = conn.execute(
        "SELECT lawd_cd FROM lawd WHERE lawd_cd5 = %s AND dong = %s AND is_active", (REGION, umd_nm)
    ).fetchone()[0]
    bonbun, bubun = jibun.split("-")
    conn.execute(
        "UPDATE danji SET lawd_cd5 = '99999', lawd_cd = NULL, bonbun = NULL, bubun = NULL, jibun = '' WHERE danji_id = %s",
        (danji_id,),
    )

    assert len(refresh_danji_locations(conn, [danji_id])) == 1
    assert conn.execute(LOCATION, (danji_id,)).fetchone() == (REGION, lawd_cd, bonbun.zfill(4), bubun.zfill(4), jibun)
