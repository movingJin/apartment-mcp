"""Phase 3 MCP 서버: 읽기 전용 계정, 0008 뷰·함수, 도구 응답. MCP_DATABASE_URL이 없으면 건너뛴다.

도구는 개발 DB의 실제 mv_* 스냅숏을 읽으므로 값 대신 불변식(예산 이하, 세대수 필터, 정렬, 제외 사유 합계,
근거 거래와 중위가의 일치)을 확인한다. 연결은 서버와 같은 읽기 전용 계정(mcp_server.db.connect)이다.
근거 추적 테스트는 mv_*가 마지막 수집 뒤에 갱신됐다고 가정한다(etl.refresh를 빼먹으면 어긋난다).
"""

import json
import os
import statistics
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from etl.regions import REGIONS
from mcp_server import db, tools

pytestmark = pytest.mark.skipif(not os.environ.get("MCP_DATABASE_URL"), reason="MCP_DATABASE_URL 필요")

DMC1 = 1217  # DMC파크뷰자이1단지(11410-4479): 한 필지 7개 단지(임대 포함), 평형 S·M·XL
DMC1_RENTAL = 151505  # DMC파크뷰자이1단지(임대): 시세용 매매가 없어 excluded
LAND_LEASE = ("11680-4341", "11650-4174")  # 강남브리즈힐, 호반써밋서초파크뷰(토지임대부)


@pytest.fixture(scope="module")
def conn():
    with db.connect() as c:
        yield c


def fresh():
    """권한 오류는 트랜잭션을 깨므로 새 연결에서 확인한다."""
    return db.connect()


def one(conn, query, params=()):
    return conn.execute(query, params).fetchone()


# ---------------------------------------------------------------- 읽기 전용 계정

@pytest.mark.parametrize("table", ["trade_sale", "trade_rent", "ingest_log", "bldg_title", "danji_flag"])
def test_mcp_role_cannot_read_raw_tables(table):
    with fresh() as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(f"SELECT 1 FROM {table} LIMIT 1")


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO analysis_profile SELECT * FROM analysis_profile",
        "UPDATE danji SET name = name WHERE danji_id = 1217",
        "REFRESH MATERIALIZED VIEW mv_danji_latest",
        "CREATE TABLE mcp_should_not_create (x int)",
    ],
)
def test_mcp_role_cannot_write(statement):
    with fresh() as c, pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        c.execute(statement)


def test_mcp_session_is_read_only_with_timeout(conn):
    row = one(conn, "SELECT current_setting('transaction_read_only') AS ro, current_setting('statement_timeout') AS t")
    assert row == {"ro": "on", "t": "30s"}
    assert one(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")["rolsuper"] is False


def test_database_url_does_not_fall_back_to_etl_account(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://superuser@localhost/apartment")
    monkeypatch.setattr(db, "ENV_FILE", tmp_path / ".env")
    with pytest.raises(RuntimeError, match="MCP_DATABASE_URL"):
        db.database_url()
    (tmp_path / ".env").write_text("# c\nDATABASE_URL=x\nMCP_DATABASE_URL=postgresql://ro@h/db\n", encoding="utf-8")
    assert db.database_url() == "postgresql://ro@h/db"


# ---------------------------------------------------------------- 0008 뷰·함수

def test_per_pyeong(conn):
    assert one(conn, "SELECT per_pyeong(302.5) AS v")["v"] == Decimal("1000.0")
    assert one(conn, "SELECT per_pyeong(NULL) AS v")["v"] is None


def test_l_and_xl_are_exactly_the_rural_tax_areas(conn):
    # max_purchase_price_for_group은 L·XL을 85m2 초과로 보고 102를 대표 면적으로 넘긴다
    row = one(conn, """
        SELECT policy_num('rural_tax_area_limit') AS lim, area_group_of(85) AS g85, area_group_of(85.0001) AS g85p,
               area_group_of(102) AS g102
    """)
    assert row == {"lim": 85, "g85": "M", "g85p": "L", "g102": "L"}


@pytest.mark.parametrize(("group", "area"), [("XS", 39), ("S", 59.9), ("M", 84.99), ("L", 90), ("XL", 135)])
@pytest.mark.parametrize("regulated", [True, False])
def test_budget_for_group_equals_budget_for_actual_area(conn, group, area, regulated):
    by_group = one(conn, "SELECT * FROM max_purchase_price_for_group(%s, 30000, 8000, %s, true, 8000)",
                   (group, regulated))
    by_area = one(conn, """
        SELECT * FROM max_purchase_price(30000, 8000, %s, homeless_household_head => true,
                                         household_income_manwon => 8000, area_excl => %s::numeric)
    """, (regulated, area))
    assert by_group == by_area


def test_region_distribution_recovery_median_matches_recovery_view(conn):
    rows = conn.execute("""
        SELECT d.lawd_cd5, d.area_group, d.recovery_pct_p50, r.region_median_recovery_pct, d.recovery_danji_count,
               r.region_danji_count
        FROM v_region_distribution d
        JOIN (SELECT DISTINCT lawd_cd5, area_group, region_median_recovery_pct, region_danji_count
              FROM mv_danji_recovery WHERE region_danji_count IS NOT NULL) r USING (lawd_cd5, area_group)
    """).fetchall()
    assert len(rows) > 300
    for r in rows:
        assert r["recovery_pct_p50"] == r["region_median_recovery_pct"], r
        assert r["recovery_danji_count"] == r["region_danji_count"], r


def test_region_distribution_quantiles_are_ordered(conn):
    rows = conn.execute("SELECT * FROM v_region_distribution WHERE price_danji_count > 0").fetchall()
    for r in rows:
        assert r["price_per_m2_p10"] <= r["price_per_m2_p25"] <= r["price_per_m2_p50"] \
               <= r["price_per_m2_p75"] <= r["price_per_m2_p90"], r
    counts = {(r["lawd_cd5"], r["area_group"]): r["price_danji_count"] for r in rows}
    expected = {(r["lawd_cd5"], r["area_group"]): r["n"] for r in conn.execute(
        "SELECT lawd_cd5, area_group, count(*) AS n FROM mv_danji_percentile GROUP BY 1, 2").fetchall()}
    assert counts == expected


def test_danji_summary_matches_snapshots(conn):
    s = conn.execute("SELECT * FROM v_danji_summary WHERE danji_id = %s ORDER BY area_group", (DMC1,)).fetchall()
    latest = {r["area_group"]: r for r in conn.execute(
        "SELECT * FROM mv_danji_latest WHERE danji_id = %s", (DMC1,)).fetchall()}
    assert {r["area_group"] for r in s} >= set(latest)
    for r in s:
        if r["area_group"] in latest:
            assert r["median_price_manwon"] == latest[r["area_group"]]["median_price_manwon"]
            assert r["sample_size"] == latest[r["area_group"]]["sample_size"]
        assert r["is_regulated"] is True  # 서대문구
        assert r["households_is_parcel_total"] is True


# ---------------------------------------------------------------- 공통 규약

def assert_meta(meta, *, aggregate=True):
    assert meta["data_as_of"]
    assert "price" in meta["units"] and meta["units"]["price"] == "만원"
    if aggregate:
        assert meta["excludes_canceled"] is True
        assert meta["data_completeness"] in ("partial", "complete")


def test_limit_is_capped():
    assert tools.clamp_limit(None) == 50
    assert tools.clamp_limit(1000) == 200
    assert tools.clamp_limit(0) == 1


def test_unknown_region_is_a_tool_error(conn):
    with pytest.raises(ToolError, match="list_regions"):
        tools.search_candidates(conn, lawd_cd5=["11000"])
    with pytest.raises(ToolError):
        tools.compute_budget(conn, lawd_cd5="41110")  # 구가 있는 상위 시 코드는 대상이 아니다


# ---------------------------------------------------------------- list_regions

def test_list_regions_all_and_by_dong(conn):
    everything = tools.list_regions(conn)
    assert [r["lawd_cd5"] for r in everything["data"]["regions"]] == sorted(REGIONS)
    assert everything["data"]["dongs"] == []

    jeongja = tools.list_regions(conn, "정자동")["data"]
    assert {d["lawd_cd5"] for d in jeongja["dongs"]} == {"41111", "41135"}  # 수원 장안구, 성남 분당구

    bundang = tools.list_regions(conn, "성남 분당")["data"]["regions"]
    assert [r["lawd_cd5"] for r in bundang] == ["41135"]
    assert bundang[0]["is_regulated"] is True
    assert "토지거래허가구역" in {z["zone_type"] for z in bundang[0]["zones"]}


def test_list_regions_escapes_like_wildcards(conn):
    assert tools.list_regions(conn, "%")["data"] == {"regions": [], "dongs": []}


# ---------------------------------------------------------------- compute_budget

def test_compute_budget_defaults_match_sql_function(conn):
    result = tools.compute_budget(conn)
    prof = one(conn, "SELECT * FROM analysis_profile WHERE profile_key = 'default'")
    assert [s["is_regulated"] for s in result["data"]["scenarios"]] == [True, False]
    for s in result["data"]["scenarios"]:
        expected = one(conn, """
            SELECT * FROM max_purchase_price(%s, %s, %s, homeless_household_head => %s, household_income_manwon => %s)
        """, (prof["equity_manwon"], prof["annual_income_manwon"], s["is_regulated"], prof["homeless_household_head"],
              prof["household_income_manwon"]))
        assert s["max_price"] == expected["max_price"]
        assert s["binding_factor"] == expected["binding_factor"]
        assert s["binding_note"]
    assert set(result["meta"]["inputs"]["source"].values()) == {"analysis_profile"}


def test_compute_budget_income_override_assumes_single(conn):
    result = tools.compute_budget(conn, annual_income_manwon=12000, lawd_cd5="11410")
    inputs = result["meta"]["inputs"]
    assert inputs["household_income_manwon"] == 12000
    assert inputs["source"]["annual_income_manwon"] == "argument"
    scenario = result["data"]["scenarios"][0]
    assert scenario["is_regulated"] is True
    assert scenario["seomin"] is False  # 부부합산 1.2억은 서민·실수요자 소득 요건(9천)을 넘는다
    assert result["data"]["region"]["name"] == "서울특별시 서대문구"


def test_compute_budget_differs_by_regulation_when_ltv_binds(conn):
    # 서민·실수요자가 아니면 규제지역은 LTV 40%가 막고 비규제지역은 DSR이 막는다(SPEC 검증 체크리스트)
    result = tools.compute_budget(conn, homeless_household_head=False)
    regulated, free = result["data"]["scenarios"]
    assert regulated["binding_factor"] == "LTV"
    assert free["binding_factor"] == "DSR"
    assert regulated["max_price"] < free["max_price"]


# ---------------------------------------------------------------- search_candidates

@pytest.fixture(scope="module")
def seoul_search(conn):
    return tools.search_candidates(conn, lawd_cd5=["11410", "11440", "11380", "11290"], limit=200)


def test_search_candidates_respects_budget_and_filters(conn, seoul_search):
    rows, meta = seoul_search["data"], seoul_search["meta"]
    assert_meta(meta)
    assert rows
    budget = {(b["is_regulated"], b["area_group"]): b["max_price"] for b in meta["budget"]}
    for r in rows:
        assert r["area_group"] in ("S", "M")
        assert r["households"] >= 300
        assert r["median_price_manwon"] <= r["budget_manwon"] == budget[(r["is_regulated"], r["area_group"])]
        assert r["budget_headroom_manwon"] == r["budget_manwon"] - r["median_price_manwon"]
        assert r["sample_size"] >= 1


def test_search_candidates_budget_matches_compute_budget(conn, seoul_search):
    scenario = tools.compute_budget(conn, lawd_cd5="11410", area_group="M")["data"]["scenarios"][0]
    [b] = [b for b in seoul_search["meta"]["budget"] if b["is_regulated"] and b["area_group"] == "M"]
    assert b["max_price"] == scenario["max_price"]


def test_search_candidates_counts_add_up(seoul_search):
    c = seoul_search["meta"]["counts"]
    assert c["considered"] == (c["excluded_insufficient_recent_sales"] + c["excluded_over_budget"]
                               + c["excluded_unknown_households"] + c["excluded_below_min_households"] + c["matched"])
    assert c["returned"] == len(seoul_search["data"]) == min(c["matched"], 200)


def test_search_candidates_sorts_confident_rows_by_percentile(seoul_search):
    keys = [(r["low_confidence"], r["price_percentile"] if r["price_percentile"] is not None else 1000)
            for r in seoul_search["data"]]
    assert keys == sorted(keys)


def test_search_candidates_only_full_scope(conn):
    rows = tools.search_candidates(conn, lawd_cd5=["11410", "11680", "11650"], min_households=0,
                                   area_group=list(tools.AREA_GROUPS), max_price_manwon=10_000_000, limit=200)["data"]
    ids = [r["danji_id"] for r in rows]
    scopes = {r["scope"] for r in conn.execute(
        "SELECT scope FROM v_danji_status WHERE danji_id = ANY(%s)", (ids,)).fetchall()}
    assert scopes == {"full"}
    excluded = [r["danji_id"] for r in conn.execute(
        "SELECT danji_id FROM danji WHERE apt_seq = ANY(%s)", (list(LAND_LEASE),)).fetchall()] + [DMC1_RENTAL]
    assert not set(ids) & set(excluded)


def test_search_candidates_user_cap_lowers_budget(conn):
    result = tools.search_candidates(conn, lawd_cd5=["11410"], max_price_manwon=50000, limit=200)
    assert all(r["budget_manwon"] == 50000 and r["median_price_manwon"] <= 50000 for r in result["data"])


def test_search_candidates_min_households_zero_keeps_unknown(conn):
    result = tools.search_candidates(conn, lawd_cd5=["41135", "28185"], min_households=0, limit=200)
    assert result["meta"]["counts"]["excluded_unknown_households"] == 0
    assert result["meta"]["counts"]["excluded_below_min_households"] == 0


def test_search_candidates_other_sorts(conn):
    by_price = tools.search_candidates(conn, lawd_cd5=["11410"], sort_by="median_price_desc", limit=200)["data"]
    confident = [r["median_price_manwon"] for r in by_price if not r["low_confidence"]]
    assert confident == sorted(confident, reverse=True)
    with pytest.raises(ToolError):
        tools.search_candidates(conn, sort_by="random()")


# ---------------------------------------------------------------- find_danji

def test_find_danji_ignores_spaces_and_shows_scope(conn):
    result = tools.find_danji(conn, "DMC 파크뷰자이", lawd_cd5="11410")
    by_id = {r["danji_id"]: r for r in result["data"]}
    assert by_id[DMC1]["scope"] == "full"
    assert by_id[DMC1_RENTAL]["scope"] == "excluded"
    assert by_id[DMC1_RENTAL]["scope_reason"] == "no_market_sale"


def test_find_danji_rejects_empty_and_escapes(conn):
    with pytest.raises(ToolError):
        tools.find_danji(conn, " 아파트 ")
    assert tools.find_danji(conn, "%_")["data"] == []


# ---------------------------------------------------------------- get_danji

def test_get_danji_detail(conn):
    result = tools.get_danji(conn, DMC1)
    assert_meta(result["meta"])
    info = result["data"]["danji"]
    assert info["apt_seq"] == "11410-4479"
    assert info["households_is_parcel_total"] is True
    assert any("필지" in n for n in result["meta"]["notes"])
    groups = result["data"]["by_area_group"]
    assert [g["area_group"] for g in groups] == sorted([g["area_group"] for g in groups], key=tools.AREA_GROUPS.index)
    for g in groups:
        assert {"sample_size", "low_confidence", "data_completeness", "jeonse_ratio", "recovery_pct"} <= set(g)
    trades = result["data"]["recent_trades"]
    assert 0 < len(trades) <= 10
    assert [t["deal_date"] for t in trades] == sorted([t["deal_date"] for t in trades], reverse=True)


def test_get_danji_excluded_has_reason_and_no_stats(conn):
    result = tools.get_danji(conn, DMC1_RENTAL)
    assert result["data"]["danji"]["scope"] == "excluded"
    assert result["data"]["by_area_group"] == []
    assert any("분석 범위 밖" in n for n in result["meta"]["notes"])


def test_get_danji_unknown_id(conn):
    with pytest.raises(ToolError, match="find_danji"):
        tools.get_danji(conn, 999_999_999)


def test_low_confidence_is_returned_for_two_sample_danji(conn):
    # SPEC 검증 체크리스트: 표본 2건짜리 단지 조회 시 low_confidence: true
    row = one(conn, "SELECT danji_id, area_group FROM mv_danji_latest WHERE sample_size = 2 AND scope = 'full' LIMIT 1")
    [g] = [g for g in tools.get_danji(conn, row["danji_id"])["data"]["by_area_group"]
           if g["area_group"] == row["area_group"]]
    assert g["sample_size"] == 2 and g["low_confidence"] is True


# ---------------------------------------------------------------- 근거 추적

def half_up(x) -> int:
    return int(Decimal(str(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@pytest.mark.parametrize("group", ["S", "M", "XL"])
def test_recent_trades_reproduce_danji_median(conn, group):
    # SPEC 검증 체크리스트: MCP 도구가 반환한 시세의 근거 거래를 get_recent_trades로 확인할 수 있다
    detail = tools.get_danji(conn, DMC1)
    [g] = [g for g in detail["data"]["by_area_group"] if g["area_group"] == group]
    period_from, period_to = detail["meta"]["period"].split(" ~ ")
    trades = tools.get_recent_trades(conn, danji_id=DMC1, area_group=group, since=date.fromisoformat(period_from),
                                     limit=200)["data"]
    basis = [t for t in trades if t["is_basis"] and t["deal_date"] <= period_to]
    assert len(basis) == g["sample_size"]
    assert half_up(statistics.median(t["price_manwon"] for t in basis)) == g["median_price_manwon"]


# ---------------------------------------------------------------- get_recent_trades

def test_recent_trades_exclude_canceled_by_default(conn):
    row = one(conn, "SELECT danji_id FROM v_sale_basis WHERE danji_id = %s AND is_canceled LIMIT 1", (DMC1,))
    assert row, "DMC파크뷰자이1단지에 해제 거래가 있어야 한다"
    default = tools.get_recent_trades(conn, danji_id=DMC1, limit=200)
    assert default["meta"]["excludes_canceled"] is True
    assert not any(t["is_canceled"] for t in default["data"])
    with_canceled = tools.get_recent_trades(conn, danji_id=DMC1, include_canceled=True, limit=200)
    assert with_canceled["meta"]["excludes_canceled"] is False
    assert any(t["is_canceled"] for t in with_canceled["data"])


def test_recent_trades_by_region_and_rent(conn):
    region = tools.get_recent_trades(conn, lawd_cd5="11410", limit=20)
    assert len(region["data"]) == 20 and region["meta"]["truncated"] is True
    ids = {t["danji_id"] for t in region["data"]}
    assert {r["lawd_cd5"] for r in conn.execute("SELECT lawd_cd5 FROM danji WHERE danji_id = ANY(%s)",
                                                  (list(ids),)).fetchall()} == {"11410"}
    rent = tools.get_recent_trades(conn, danji_id=DMC1, kind="rent", limit=5)
    assert rent["meta"]["excludes_canceled"] is False
    assert {"deposit_manwon", "monthly_manwon", "is_jeonse_basis"} <= set(rent["data"][0])


@pytest.mark.parametrize("kwargs", [{}, {"danji_id": DMC1, "lawd_cd5": "11410"}, {"danji_id": DMC1, "kind": "x"}])
def test_recent_trades_argument_errors(conn, kwargs):
    with pytest.raises(ToolError):
        tools.get_recent_trades(conn, **kwargs)


# ---------------------------------------------------------------- get_price_history

def test_price_history_window_and_partial(conn):
    result = tools.get_price_history(conn, DMC1, "M", months=24)
    assert_meta(result["meta"])
    months = [r["month"] for r in result["data"]]
    assert months == sorted(months)
    start, end = result["meta"]["period"].split(" ~ ")
    assert all(start <= m <= end for m in months)
    [summary] = result["meta"]["by_area_group"]
    assert summary["sample_size"] == sum(r["sample_size"] for r in result["data"])


def test_price_history_marks_recent_months_partial(conn):
    # SPEC 검증 체크리스트: 최근 조회 시 data_completeness: partial(기준월 포함 3개월, 정제 규칙 4)
    result = tools.get_price_history(conn, DMC1, months=12)
    y, m = map(int, result["meta"]["period"].split(" ~ ")[1].split("-"))
    first_partial = f"{y if m > 2 else y - 1}-{(m - 3) % 12 + 1:02d}"
    rows = result["data"]
    assert any(r["month"] >= first_partial for r in rows)
    assert all((r["data_completeness"] == "partial") == (r["month"] >= first_partial) for r in rows)
    assert result["meta"]["data_completeness"] == "partial"
    assert tools.get_price_history(conn, DMC1, months=10_000)["meta"]["months"] == tools.MAX_MONTHS


# ---------------------------------------------------------------- get_region_stats

def test_region_stats(conn):
    result = tools.get_region_stats(conn, "11410", months=12)
    assert_meta(result["meta"])
    data = result["data"]
    assert data["region"]["name"] == "서울특별시 서대문구"
    assert {r["area_group"] for r in data["monthly"]} <= {"S", "M"}
    assert [d["area_group"] for d in data["distribution"]] == ["S", "M"]
    by_month = {}
    for r in data["monthly"]:
        by_month.setdefault(r["month"], 0)
        by_month[r["month"]] += r["trade_count"]
    volume = {v["month"]: v["trade_count"] for v in data["volume"]}
    assert all(by_month[m] <= volume[m] for m in by_month)  # 그룹 합계(전 평형) ≥ S·M 합


# ---------------------------------------------------------------- compare_danji

def test_compare_danji(conn):
    result = tools.compare_danji(conn, [1261, DMC1, DMC1_RENTAL], ["M"])
    assert [d["danji_id"] for d in result["data"]["danji"]] == [1261, DMC1, DMC1_RENTAL]
    rows = result["data"]["by_area_group"]
    assert [r["danji_id"] for r in rows] == [1261, DMC1]  # 임대는 시세 행이 없다
    assert all(r["area_group"] == "M" for r in rows)
    assert any("분석 범위 밖" in n for n in result["meta"]["notes"])


@pytest.mark.parametrize("ids", [[DMC1], [DMC1, 999_999_999], list(range(1, 13))])
def test_compare_danji_argument_errors(conn, ids):
    with pytest.raises(ToolError):
        tools.compare_danji(conn, ids)


# ---------------------------------------------------------------- MCP 프로토콜(서버 인스턴스에 메모리로 붙는다)

TOOL_NAMES = {"list_regions", "compute_budget", "search_candidates", "find_danji", "get_danji", "get_price_history",
              "get_recent_trades", "get_region_stats", "compare_danji"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    from mcp.client import Client

    from mcp_server.server import mcp

    async with Client(mcp) as c:
        yield c


@pytest.mark.anyio
async def test_server_exposes_only_typed_read_only_tools(client):
    listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == TOOL_NAMES  # 범용 SQL 도구 없음
    for t in listed:
        assert t.annotations.read_only_hint is True
        assert "쓴다" in t.description  # 언제 쓰는지 적혀 있다


@pytest.mark.anyio
async def test_server_returns_compact_json_with_structured_content(client):
    result = await client.call_tool("search_candidates", {"lawd_cd5": ["11410"], "limit": 1000})
    assert not result.is_error
    assert result.structured_content["meta"]["limit"] == 200
    text = result.content[0].text
    assert json.loads(text) == result.structured_content
    assert "\n" not in text and "서대문구" in text  # 들여쓰기 없음, 한글 그대로


@pytest.mark.anyio
async def test_server_reports_errors_to_the_model(client):
    missing = await client.call_tool("get_danji", {"danji_id": 999_999_999})
    assert missing.is_error and "find_danji" in missing.content[0].text
    invalid = await client.call_tool("search_candidates", {"area_group": ["M", "84"]})
    assert invalid.is_error
