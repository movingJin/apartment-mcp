import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from tenacity import wait_none

from etl.bldg import (
    HubClient,
    Parcel,
    Task,
    apply_to_danji,
    derive,
    done_sets,
    pick_recap,
    plan_parcels,
    plan_tasks,
    rederive,
    run_tasks,
)
from etl.datagokr import ApiError, parse_page
from etl.db import connect

FIXTURES = Path(__file__).parent / "fixtures" / "bldg"
# 건축HUB 실제 응답(2026-09-25). 필지별 {parcel, recap(대지구분 0), title}. 태그는 parse_page처럼 소문자
PARCELS = json.loads((FIXTURES / "parcels.json").read_text(encoding="utf-8"))
# 서대문구 남가좌동(1141012000) 법정동 단위 총괄표제부 45건
RECAP_DONG = json.loads((FIXTURES / "recap_1141012000.json").read_text(encoding="utf-8"))

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")


def hub_xml(items: list[dict[str, str]], total: int) -> bytes:
    body = "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in i.items()) + "</item>" for i in items)
    return (
        "<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE</resultMsg></header>"
        f"<body><items>{body}</items><numOfRows>100</numOfRows><pageNo>1</pageNo>"
        f"<totalCount>{total}</totalCount></body></response>"
    ).encode()


def case(name: str) -> tuple[list[dict], list[dict]]:
    return PARCELS[name]["recap"], PARCELS[name]["title"]


# ---------------------------------------------------------------- 파생 규칙


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # 총괄표제부 값. 한 필지에 단지 6개(DMC파크뷰자이 1~5단지 + 2단지(임대))의 합계다
        ("shared_recap", dict(recap_mgm_pk="10141100216842", households=4300, buildings=61, floors_max=33,
                              far=Decimal("233.35"), bcr=Decimal("19.71"), parking=5823,
                              use_apr_date=date(2015, 10, 26))),
        # 동이 하나라 총괄표제부가 없다. 표제부 값, 주차는 옥내자주식 1112 + 옥외자주식 15
        ("single", dict(recap_mgm_pk=None, households=265, buildings=1, floors_max=40, far=Decimal("1030.53"),
                        bcr=Decimal("40.22"), parking=1127, use_apr_date=date(2009, 2, 20))),
        # 일반 구대장 441세대와 집합 신대장 420세대 중 집합 신대장. 표제부 공동주택 합계도 420(상가동 21세대 제외)
        ("two_recaps", dict(recap_mgm_pk="10151804", households=420, buildings=4, floors_max=21,
                            far=Decimal("343.67"), bcr=Decimal("25.47"), parking=266,
                            use_apr_date=date(1994, 9, 29))),
        # 총괄표제부 없이 7동. 용적률은 동별 값(34.71, 18.72, ...)이고 주차 268은 동마다 반복돼 합하지 않는다.
        # 공동주택 용도의 노유자시설동(세대 0, 사용승인 1992-09-09)은 주거동이 아니다
        ("multi_no_recap", dict(recap_mgm_pk=None, households=612, buildings=7, floors_max=15, far=None,
                                bcr=None, parking=None, use_apr_date=date(1992, 9, 15))),
        # 옛 대장은 용적률·건폐율·주차를 0으로 둔다. 공동주택 용도의 상가(세대 0)는 동 수에서 빠진다
        ("zero_ratio", dict(recap_mgm_pk=None, households=364, buildings=6, floors_max=13, far=None, bcr=None,
                            parking=None, use_apr_date=date(1992, 11, 25))),
        # 주상복합: 주용도가 업무시설이지만 세대가 있다
        ("mixed_use", dict(recap_mgm_pk=None, households=64, buildings=1, floors_max=18, far=Decimal("898.45"),
                           bcr=Decimal("77.72"), parking=132, use_apr_date=date(2020, 6, 26))),
        # 재건축 뒤 대장이 이 지번에 없다(길음역롯데캐슬트윈골드 542-1)
        ("not_found", dict(recap_mgm_pk=None, households=None, buildings=None, floors_max=None, far=None,
                           bcr=None, parking=None, use_apr_date=None)),
    ],
)
def test_derive_real_responses(name, expected):
    assert derive(*case(name)).__dict__ == expected


def test_pick_recap_order():
    recaps, _ = case("two_recaps")
    assert pick_recap(recaps)["mgmbldrgstpk"] == "10151804"
    assert pick_recap(list(reversed(recaps)))["mgmbldrgstpk"] == "10151804"  # 응답 순서와 무관
    assert pick_recap([]) is None


def test_recap_zero_falls_back_to_single_title():
    _, titles = case("single")
    recap = {"mgmbldrgstpk": "1", "regstrgbcd": "2", "hhldcnt": "0", "vlrat": "0", "bcrat": "", "totpkngcnt": "0"}
    v = derive([recap], titles)
    assert (v.recap_mgm_pk, v.households, v.far, v.parking) == ("1", 265, Decimal("1030.53"), 1127)


def test_old_registry_without_households_keeps_buildings_and_floors():
    title = {"mainatchgbcd": "0", "mainpurpscd": "02000", "hhldcnt": "0", "grndflrcnt": "5", "mgmbldrgstpk": "1"}
    v = derive([], [title, {**title, "mgmbldrgstpk": "2", "grndflrcnt": "6"}])
    assert (v.households, v.buildings, v.floors_max) == (None, 2, 6)


def test_non_numeric_value_is_an_error():
    _, titles = case("single")
    with pytest.raises(ValueError, match="vlrat"):
        derive([], [{**titles[0], "vlrat": "N/A"}])


# ---------------------------------------------------------------- 계획

P385 = Parcel("1141012000", "0", "0385", "0000")
P1 = Parcel("1141012000", "0", "0001", "0001")
Q = Parcel("1141011800", "0", "0100", "0000")


def test_parcel_label_marks_mountain_lot():
    assert str(P385) == "1141012000-0385-0000"
    assert str(Parcel("1141011800", "1", "0011", "0244")) == "1141011800-산0011-0244"


def test_plan_tasks_recap_before_parcels_of_each_dong():
    tasks = plan_tasks([P385, Q, P1], recap_done=set(), parcel_done=set(), force=False)
    assert [str(t) for t in tasks] == [
        "recap 1141011800", f"title {Q}", "recap 1141012000", f"title {P1}", f"title {P385}",
    ]


def test_plan_tasks_skips_done_unless_forced():
    done = plan_tasks([P385, Q, P1], recap_done={"1141012000"}, parcel_done={P385, Q}, force=False)
    assert [str(t) for t in done] == ["recap 1141011800", f"title {P1}"]
    assert len(plan_tasks([P385, Q, P1], {"1141012000"}, {P385, Q}, force=True)) == 5


# ---------------------------------------------------------------- 클라이언트


def mock_hub(titles: dict[Parcel, list[dict] | int], recap: list[dict] | int = RECAP_DONG, calls: list | None = None):
    """정수를 주면 그 HTTP 상태로 실패한다. 페이지는 100건씩 자른다."""

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        if calls is not None:
            calls.append(request)
        if request.url.path.endswith("getBrRecapTitleInfo"):
            items = recap
        else:
            items = titles[Parcel(q["sigunguCd"] + q["bjdongCd"], q["platGbCd"], q["bun"], q["ji"])]
        if isinstance(items, int):
            return httpx.Response(items, content=b"")
        page = int(q["pageNo"])
        return httpx.Response(200, content=hub_xml(items[(page - 1) * 100: page * 100], len(items)))

    return HubClient("KEY", rps=0, http=httpx.Client(transport=httpx.MockTransport(handler)), wait=wait_none())


def test_title_request_params_and_paging():
    calls = []
    with mock_hub({P385: case("shared_recap")[1]}, calls=calls) as client:
        items = client.title(P385)
    assert len(items) == 102
    params = [dict(r.url.params) for r in calls]
    assert [p["pageNo"] for p in params] == ["1", "2"]
    assert {k: params[0][k] for k in ("sigunguCd", "bjdongCd", "platGbCd", "bun", "ji", "numOfRows")} == {
        "sigunguCd": "11410", "bjdongCd": "12000", "platGbCd": "0", "bun": "0385", "ji": "0000", "numOfRows": "100",
    }
    assert calls[0].url.path == "/1613000/BldRgstHubService/getBrTitleInfo"


def test_hub_result_code_is_two_digits():
    assert parse_page(hub_xml([], 0), "00").total_count == 0
    with pytest.raises(ApiError):
        parse_page(hub_xml([], 0), "000")


# ---------------------------------------------------------------- DB

DONG = "1141012000"
MISSING = Parcel(DONG, "0", "9999", "0000")  # 대장 없는 필지
MOUNTAIN = Parcel(DONG, "1", "0011", "0244")  # 산 지번. 매매 응답은 본번·부번 코드만 주고 '산'은 지번 문자열에 있다


@pytest.fixture
def conn():
    with connect(autocommit=True) as c:
        with c.transaction(force_rollback=True):
            # 개발 DB에 실제로 받아 둔 같은 동 자료가 있을 수 있어 트랜잭션 안에서 먼저 비운다
            for table in ("bldg_recap", "bldg_recap_log", "bldg_title", "bldg_parcel"):
                c.execute(f"DELETE FROM {table} WHERE lawd_cd = %s", (DONG,))
            c.execute(
                """INSERT INTO danji (apt_seq, lawd_cd5, lawd_cd, bonbun, bubun, jibun, name, name_norm)
                   VALUES ('T-385', '11410', %s, '0385', '0000', '385', 'T', 'T'),
                          ('T-9999', '11410', %s, '9999', '0000', '9999', 'T', 'T'),
                          ('T-M', '11410', %s, '0011', '0244', '산11-244', 'T', 'T')""",
                (DONG, DONG, DONG),
            )
            yield c


def danji_bldg(conn, apt_seq):
    return conn.execute(
        "SELECT households, buildings, floors_max, far, bcr, parking, bldg_danji_cnt FROM danji WHERE apt_seq = %s",
        (apt_seq,),
    ).fetchone()


def run(conn, titles, recap=RECAP_DONG, force=False):
    recap_done, parcel_done = done_sets(conn)
    tasks = plan_tasks([P385, MISSING, MOUNTAIN], recap_done, parcel_done, force)
    with mock_hub(titles, recap) as client:
        return tasks, run_tasks(conn, client, tasks, recap_done)


@needs_db
def test_fetch_store_and_apply(conn):
    tasks, summary = run(conn, {P385: case("shared_recap")[1], MISSING: [], MOUNTAIN: []})
    assert (summary.ok, summary.failures) == (4, [])
    assert conn.execute("SELECT count(*) FROM bldg_recap WHERE lawd_cd = %s", (DONG,)).fetchone()[0] == 45
    assert conn.execute("SELECT count(*) FROM bldg_title WHERE lawd_cd = %s", (DONG,)).fetchone()[0] == 102
    assert conn.execute(
        "SELECT status, recap_mgm_pk, households FROM bldg_parcel WHERE (lawd_cd, bonbun, bubun) = (%s, %s, %s)",
        (DONG, "0385", "0000"),
    ).fetchone() == ("ok", "10141100216842", 4300)

    apply_to_danji(conn)
    shared = conn.execute(
        "SELECT count(*) FROM danji WHERE (lawd_cd, bonbun, bubun) = (%s, '0385', '0000')", (DONG,)
    ).fetchone()[0]
    assert shared >= 2  # 테스트 단지 + 개발 DB의 DMC파크뷰자이들
    assert danji_bldg(conn, "T-385") == (4300, 61, 33, Decimal("233.35"), Decimal("19.71"), 5823, shared)
    assert danji_bldg(conn, "T-9999") == (None,) * 7
    assert conn.execute("SELECT reason FROM v_bldg_missing WHERE apt_seq = 'T-9999'").fetchone() == ("not_found",)

    # 다시 실행하면 받을 것이 없다. 원문에서 다시 계산해도 바뀌지 않는다
    tasks, _ = run(conn, {})
    assert tasks == []
    assert rederive(conn, [DONG]) == 0
    assert apply_to_danji(conn) == 0


@needs_db
def test_failed_recap_skips_titles_of_that_dong(conn):
    tasks, summary = run(conn, {P385: case("shared_recap")[1], MISSING: [], MOUNTAIN: []}, recap=503)
    assert [str(t) for t, _ in summary.failures] == [f"recap {DONG}"]
    assert summary.skipped == 3
    assert conn.execute("SELECT status FROM bldg_recap_log WHERE lawd_cd = %s", (DONG,)).fetchone() == ("error",)
    assert conn.execute("SELECT count(*) FROM bldg_parcel WHERE lawd_cd = %s", (DONG,)).fetchone()[0] == 0


@needs_db
def test_refetch_error_keeps_previous_values(conn):
    run(conn, {P385: case("shared_recap")[1], MISSING: [], MOUNTAIN: []})
    _, summary = run(conn, {P385: 403, MISSING: [], MOUNTAIN: []}, force=True)
    assert [str(t) for t, _ in summary.failures] == [f"title {P385}"]
    assert conn.execute(
        "SELECT status, households FROM bldg_parcel WHERE (lawd_cd, bonbun, bubun) = (%s, %s, %s)",
        (DONG, "0385", "0000"),
    ).fetchone() == ("error", 4300)
    assert conn.execute("SELECT count(*) FROM bldg_title WHERE lawd_cd = %s", (DONG,)).fetchone()[0] == 102
    assert Task("title", DONG, P385) in plan_tasks([P385], *done_sets(conn), force=False)  # 다음 실행이 다시 받는다


@needs_db
def test_new_recap_rederives_parcels_already_fetched(conn):
    run(conn, {P385: case("shared_recap")[1], MISSING: [], MOUNTAIN: []})
    changed = [{**r, "hhldcnt": "4301"} if r["mgmbldrgstpk"] == "10141100216842" else r for r in RECAP_DONG]
    recap_done, _ = done_sets(conn)
    with mock_hub({}, changed) as client:  # 총괄표제부만 다시 받는다. 표제부는 호출하지 않는다
        summary = run_tasks(conn, client, [Task("recap", DONG)], recap_done - {DONG})
    assert summary.rederived == 1
    assert conn.execute(
        "SELECT households FROM bldg_parcel WHERE (lawd_cd, bonbun, bubun) = (%s, %s, %s)", (DONG, "0385", "0000")
    ).fetchone() == (4301,)


@needs_db
def test_mountain_lot_is_queried_as_mountain(conn):
    assert conn.execute("SELECT plat_gb_of('산11-244'), plat_gb_of('385'), plat_gb_of('')").fetchone() == ("1", "0", "0")
    assert {MOUNTAIN, P385, MISSING} <= set(plan_parcels(conn, ["11410"]))
    calls = []
    title = {**case("single")[1][0], "platgbcd": "1", "bun": "0011", "ji": "0244"}
    recap_done, parcel_done = done_sets(conn)
    with mock_hub({MOUNTAIN: [title]}, calls=calls) as client:
        run_tasks(conn, client, plan_tasks([MOUNTAIN], recap_done, parcel_done, False), recap_done)
    assert calls[-1].url.params["platGbCd"] == "1"
    apply_to_danji(conn)
    assert danji_bldg(conn, "T-M")[0] == 265
