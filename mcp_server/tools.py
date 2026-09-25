"""MCP 도구의 조회 로직. 서버(server.py)와 테스트가 함께 쓴다.

모든 함수는 첫 인자로 읽기 전용 연결(mcp_server.db.connect)을 받고 {"data": ..., "meta": ...}를 돌려준다.
- 계산은 SQL 뷰·함수(db/migrations 0005~0008)에 있고 여기서는 조회·필터·정렬만 한다
- 읽는 것은 mv_*와 그 위의 뷰(v_danji_summary 등), 함수, 마스터 테이블이다. 원시 거래는 get_recent_trades만
  v_sale_basis·v_rent_basis를 단지로 좁혀 읽는다(시군구로 걸면 전체를 계산해 6초, 단지 id 배열로 좁히면 수십 ms)
- 값은 모두 파라미터로 바인딩한다. 정렬처럼 SQL 조각을 고르는 곳은 고정된 목록에서만 고른다
- 모델이 고칠 수 있는 인자 오류는 ToolError로 알린다(메시지가 모델에 그대로 간다)
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError
from psycopg import sql

from etl.records import normalize_name
from etl.regions import REGIONS

AREA_GROUPS = ("XS", "S", "M", "L", "XL")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_MONTHS = 120
MAX_COMPARE = 10
RECENT_TRADES_IN_DANJI = 10

UNITS = {
    "price": "만원",
    "area": "m2(전용면적)",
    "price_per_m2": "만원/m2(전용)",
    "price_per_pyeong": "만원/평(전용, 1평 = 3.3058m2)",
    "jeonse_ratio": "%(전세 m2당 중위 ÷ 매매 m2당 중위 × 100)",
    "percentile": "0 = 같은 시군구·평형그룹에서 m2당가가 가장 쌈 ~ 100 = 가장 비쌈",
    "recovery_pct": "%(최근 6개월 m2당 중위 ÷ 2021Q1~2022Q2 분기 중위 최고 × 100)",
    "recovery_vs_region_pp": "%p(단지 회복률 − 같은 시군구·평형그룹 회복률 중위)",
    "ratio": "0~1 비율(ltv, 금리, dsr_ratio)",
}

SUMMARY_UNITS = tuple(k for k in UNITS if k != "ratio")

BINDING_NOTES = {
    "LTV": "주택가격 대비 대출 비율(LTV)이 막는다. 규제지역이면 비규제지역에서 LTV가 높아 더 살 수 있다",
    "DSR": "소득 대비 원리금 상환액(DSR)이 막는다. 지역을 바꿔도 예산이 같다. 소득이나 자기자본이 늘어야 한다",
    "CAP": "주택담보대출 금액 상한이 막는다",
    "EQUITY": "대출 없이 자기자본만으로 산다",
}
BINDING_DETAIL_NOTES = {
    "seomin_price_limit": "서민·실수요자 주택가격 요건을 넘으면 규제지역 LTV가 낮아져 더 비싼 집은 못 산다",
    "loan_cap_tier": "주택가격 구간 경계를 넘으면 대출 상한이 낮아져 더 비싼 집은 못 산다",
}

AREA_ORDER = sql.SQL("array_position(ARRAY['XS', 'S', 'M', 'L', 'XL'], area_group)")

# search_candidates 정렬. 키는 도구 인자, 값은 고정 SQL 조각이다
SORTS = {
    "price_percentile": sql.SQL("price_percentile ASC NULLS LAST, median_price_per_m2 ASC"),
    "median_price_desc": sql.SQL("median_price_manwon DESC"),
    "recovery_vs_region": sql.SQL("recovery_vs_region_pp ASC NULLS LAST, recovery_pct ASC"),
}

# search_candidates·compare_danji가 행마다 싣는 v_danji_summary 컬럼(get_danji는 전부)
CANDIDATE_COLUMNS = (
    "danji_id", "name", "sigungu", "dong", "built_year", "households", "households_is_parcel_total", "is_regulated",
    "area_group", "median_price_manwon", "median_area_excl", "median_price_per_m2", "median_price_per_pyeong",
    "sample_size", "low_confidence", "median_jeonse_manwon", "jeonse_ratio", "jeonse_sample_size",
    "price_percentile", "recovery_pct", "recovery_vs_region_pp", "data_completeness",
)
COMPARE_COLUMNS = CANDIDATE_COLUMNS + (
    "min_price_manwon", "max_price_manwon", "last_sale_date", "jeonse_low_confidence", "percentile_danji_count",
    "peak_quarter", "peak_price_per_m2", "region_median_recovery_pct", "recovery_percentile", "trough_pct",
)


# ---------------------------------------------------------------- 공통

def jsonable(value: Any) -> Any:
    """Decimal·날짜를 JSON 값으로 바꾼다. 정수인 Decimal은 int, 아니면 float."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def fetch(conn: psycopg.Connection, query, params=None) -> list[dict]:
    return conn.execute(query, params).fetchall()


def fetch_one(conn: psycopg.Connection, query, params=None) -> dict | None:
    return conn.execute(query, params).fetchone()


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> int:
    """SPEC 보안: limit 상한을 서버에서 강제한다(기본 50, 최대 200)."""
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def check_region(code: str) -> str:
    code = code.strip()
    if code not in REGIONS:
        raise ToolError(f"수집 대상 시군구 코드가 아니다: {code!r}. list_regions로 5자리 코드를 찾을 것")
    return code


def check_area_groups(groups: list[str] | None, default: list[str]) -> list[str]:
    groups = list(dict.fromkeys(groups)) if groups else list(default)
    bad = [g for g in groups if g not in AREA_GROUPS]
    if bad:
        raise ToolError(f"평형그룹은 {', '.join(AREA_GROUPS)} 중에서 고른다: {bad}")
    return sorted(groups, key=AREA_GROUPS.index)


def snapshot(conn: psycopg.Connection) -> dict:
    """mv_* 갱신 기준(mv_refresh_log 마지막 행)과 최근 6개월 창."""
    row = fetch_one(
        conn,
        """
        SELECT g.as_of, g.refreshed_at, as_of_date() AS today,
               (SELECT period_from FROM mv_danji_latest LIMIT 1) AS period_from,
               (SELECT period_to FROM mv_danji_latest LIMIT 1) AS period_to
        FROM mv_refresh_log g
        ORDER BY g.refreshed_at DESC
        LIMIT 1
        """,
    )
    if row is None:
        raise ToolError("파생 지표(mv_*)가 아직 계산되지 않았다. 운영자가 python -m etl.refresh를 실행해야 한다")
    return row


def base_meta(snap: dict, *, units: tuple, period: tuple | None = None, excludes_canceled: bool = True,
              **extra) -> dict:
    meta: dict[str, Any] = {}
    if period is not None:
        meta["period"] = f"{period[0]} ~ {period[1]}"
    meta["data_as_of"] = snap["as_of"]
    meta["excludes_canceled"] = excludes_canceled
    meta["units"] = {k: UNITS[k] for k in units}
    stale_days = (snap["today"] - snap["as_of"]).days
    if stale_days > 0:
        meta["warning"] = (f"파생 지표가 {stale_days}일 전({snap['as_of']}) 기준이다. "
                           "최근 거래와 data_completeness가 오늘과 다를 수 있다")
    meta.update(extra)
    return meta


def latest_period(snap: dict) -> tuple:
    return (snap["period_from"], snap["period_to"])


def profile(conn: psycopg.Connection) -> dict:
    row = fetch_one(conn, "SELECT * FROM analysis_profile WHERE profile_key = 'default'")
    if row is None:
        raise ToolError("analysis_profile에 default 행이 없다")
    return row


def zones_by_region(conn: psycopg.Connection, codes: list[str]) -> dict[str, list[dict]]:
    zones: dict[str, list[dict]] = {c: [] for c in codes}
    for z in fetch(
        conn,
        """
        SELECT lawd_cd5, zone_type, effective_from, effective_to
        FROM v_regulated_area_current
        WHERE lawd_cd5 = ANY(%s)
        ORDER BY lawd_cd5, zone_type
        """,
        (codes,),
    ):
        zones[z.pop("lawd_cd5")].append(z)
    return zones


def resolve_budget_inputs(
    prof: dict,
    equity_manwon: int | None,
    annual_income_manwon: int | None,
    household_income_manwon: int | None,
    homeless_household_head: bool | None,
) -> dict:
    """예산 인자. 비운 값은 분석 기준값(analysis_profile 'default')을 쓴다.
    연소득만 바꾸고 부부합산 소득을 비우면 부부합산 = 연소득(미혼)으로 본다."""
    source = {}

    def pick(name, given, fallback):
        source[name] = "argument" if given is not None else "analysis_profile"
        return given if given is not None else fallback

    inputs = {
        "equity_manwon": pick("equity_manwon", equity_manwon, prof["equity_manwon"]),
        "annual_income_manwon": pick("annual_income_manwon", annual_income_manwon, prof["annual_income_manwon"]),
        "homeless_household_head": pick("homeless_household_head", homeless_household_head,
                                        prof["homeless_household_head"]),
    }
    if household_income_manwon is not None:
        inputs["household_income_manwon"] = household_income_manwon
        source["household_income_manwon"] = "argument"
    elif annual_income_manwon is not None:
        inputs["household_income_manwon"] = annual_income_manwon
        source["household_income_manwon"] = "annual_income_manwon(미혼으로 봄)"
    else:
        inputs["household_income_manwon"] = prof["household_income_manwon"]
        source["household_income_manwon"] = "analysis_profile"
    if inputs["equity_manwon"] < 0 or inputs["annual_income_manwon"] < 0 or inputs["household_income_manwon"] < 0:
        raise ToolError("자기자본과 소득은 0 이상이어야 한다")
    inputs["source"] = source
    return inputs


def budgets(conn: psycopg.Connection, inputs: dict, regulated: list[bool], groups: list[str]) -> list[dict]:
    """(규제 여부, 평형그룹)마다 max_purchase_price_for_group 결과."""
    return fetch(
        conn,
        """
        SELECT r.is_regulated, g.area_group, m.*
        FROM unnest(%(regulated)s::boolean[]) AS r (is_regulated)
        CROSS JOIN unnest(%(groups)s::text[]) WITH ORDINALITY AS g (area_group, ord)
        CROSS JOIN LATERAL max_purchase_price_for_group(
          g.area_group, %(equity)s, %(income)s, r.is_regulated, %(homeless)s, %(household)s) m
        ORDER BY r.is_regulated DESC, g.ord
        """,
        {
            "regulated": regulated, "groups": groups,
            "equity": inputs["equity_manwon"], "income": inputs["annual_income_manwon"],
            "homeless": inputs["homeless_household_head"], "household": inputs["household_income_manwon"],
        },
    )


def danji_exists(conn: psycopg.Connection, danji_id: int) -> None:
    if fetch_one(conn, "SELECT 1 FROM danji WHERE danji_id = %s", (danji_id,)) is None:
        raise ToolError(f"danji_id {danji_id} 단지가 없다. find_danji나 search_candidates로 찾을 것")


def pick(row: dict, columns: tuple) -> dict:
    return {c: row[c] for c in columns}


# ---------------------------------------------------------------- 도구

def list_regions(conn: psycopg.Connection, query: str | None = None, limit: int | None = None) -> dict:
    limit = clamp_limit(limit)
    tokens = (query or "").split()
    patterns = [f"%{escape_like(t)}%" for t in tokens]
    codes = list(REGIONS)
    regions = fetch(
        conn,
        """
        SELECT r.lawd_cd5, r.name, is_regulated(r.lawd_cd5) AS is_regulated
        FROM unnest(%(codes)s::text[], %(names)s::text[]) AS r (lawd_cd5, name)
        WHERE r.name ILIKE ALL (%(patterns)s::text[])
        ORDER BY r.lawd_cd5
        """,
        {"codes": codes, "names": [REGIONS[c] for c in codes], "patterns": patterns},
    )
    dongs = []
    if tokens:
        # 동 이름에 검색어가 하나 이상 들어간 것만(구 이름만 맞는 동 전체가 쏟아지지 않게)
        dongs = fetch(
            conn,
            """
            SELECT lawd_cd, lawd_cd5, dong, is_regulated(lawd_cd5) AS is_regulated
            FROM lawd
            WHERE is_active AND dong IS NOT NULL AND lawd_cd5 = ANY(%(codes)s)
              AND concat_ws(' ', sido, sigungu, dong) ILIKE ALL (%(patterns)s::text[])
              AND dong ILIKE ANY (%(patterns)s::text[])
            ORDER BY lawd_cd
            LIMIT %(limit)s
            """,
            {"codes": codes, "patterns": patterns, "limit": limit + 1},
        )
        for d in dongs:
            d["region_name"] = REGIONS[d["lawd_cd5"]]
    truncated = len(dongs) > limit
    dongs = dongs[:limit]
    zones = zones_by_region(conn, [r["lawd_cd5"] for r in regions])
    for r in regions:
        r["zones"] = zones[r["lawd_cd5"]]
    today = fetch_one(conn, "SELECT as_of_date() AS d")["d"]
    return jsonable({
        "data": {"regions": regions, "dongs": dongs},
        "meta": {
            "query": query,
            "on_date": today,
            "region_count": len(regions),
            "dong_count": len(dongs),
            "dongs_truncated": truncated,
            "notes": [
                "대상은 수도권 83개 시군구(서울 25, 인천 11, 경기 47)다. 다른 도구의 lawd_cd5는 regions의 5자리 코드를 쓴다",
                "is_regulated는 투기과열지구·조정대상지역 여부(LTV 판정)다. 토지거래허가구역(실거주 의무)은 zones에만 있다",
            ],
        },
    })


def compute_budget(
    conn: psycopg.Connection,
    equity_manwon: int | None = None,
    annual_income_manwon: int | None = None,
    lawd_cd5: str | None = None,
    household_income_manwon: int | None = None,
    homeless_household_head: bool | None = None,
    area_group: str | None = None,
) -> dict:
    prof = profile(conn)
    inputs = resolve_budget_inputs(prof, equity_manwon, annual_income_manwon, household_income_manwon,
                                   homeless_household_head)
    group = check_area_groups([area_group] if area_group else None, ["M"])[0]
    region = None
    if lawd_cd5:
        code = check_region(lawd_cd5)
        reg = fetch_one(conn, "SELECT is_regulated(%s) AS r", (code,))["r"]
        region = {"lawd_cd5": code, "name": REGIONS[code], "is_regulated": reg,
                  "zones": zones_by_region(conn, [code])[code]}
        regulated = [reg]
    else:
        regulated = [True, False]
    scenarios = budgets(conn, inputs, regulated, [group])
    for s in scenarios:
        s["binding_note"] = BINDING_NOTES.get(s["binding_factor"])
        if s["binding_detail"]:
            s["binding_detail_note"] = BINDING_DETAIL_NOTES.get(s["binding_detail"])
    assumptions = fetch_one(
        conn,
        """
        SELECT as_of_date() AS on_date,
               policy_num('assumed_mortgage_rate') AS assumed_mortgage_rate,
               policy_num('stress_rate') AS stress_rate,
               policy_num('loan_years') AS loan_years,
               policy_num('dsr_ratio') AS dsr_ratio
        """,
    )
    on_date = assumptions.pop("on_date")
    return jsonable({
        "data": {"region": region, "scenarios": scenarios},
        "meta": {
            "on_date": on_date,
            "inputs": {**inputs, "area_group": group},
            "assumptions": assumptions,
            "units": {k: UNITS[k] for k in ("price", "ratio")},
            "notes": [
                "max_price는 가격 − 대출 + 취득세 ≤ 자기자본인 가장 큰 가격이다(SQL 함수 max_purchase_price)",
                "binding_factor는 max_price보다 1만원 비싼 집을 막는 한도다",
                "모형에 없는 것: 기존 부채, 정책대출(디딤돌·보금자리), 생애최초 우대, 고정금리 상품의 스트레스 금리 감면"
                "(스트레스 금리를 전부 더해 변동금리 기준으로 보수적이다)",
                "L·XL(전용 85m2 초과)은 농어촌특별세가 붙어 최대 매수가가 조금 낮다",
            ],
        },
    })


def search_candidates(
    conn: psycopg.Connection,
    lawd_cd5: list[str] | None = None,
    area_group: list[str] | None = None,
    min_households: int | None = None,
    built_after: int | None = None,
    max_price_manwon: int | None = None,
    equity_manwon: int | None = None,
    annual_income_manwon: int | None = None,
    household_income_manwon: int | None = None,
    homeless_household_head: bool | None = None,
    min_sample_size: int = 1,
    sort_by: str = "price_percentile",
    limit: int | None = None,
) -> dict:
    snap = snapshot(conn)
    prof = profile(conn)
    limit = clamp_limit(limit)
    if sort_by not in SORTS:
        raise ToolError(f"sort_by는 {', '.join(SORTS)} 중 하나다")
    regions = list(dict.fromkeys(check_region(c) for c in lawd_cd5)) if lawd_cd5 else list(REGIONS)
    groups = check_area_groups(area_group, prof["preferred_area_groups"])
    min_hh = prof["min_households"] if min_households is None else max(0, min_households)
    min_sample = max(1, min_sample_size)
    inputs = resolve_budget_inputs(prof, equity_manwon, annual_income_manwon, household_income_manwon,
                                   homeless_household_head)

    regulated = [r["r"] for r in fetch(
        conn, "SELECT DISTINCT is_regulated(c) AS r FROM unnest(%s::text[]) AS c ORDER BY 1 DESC", (regions,))]
    budget_rows = budgets(conn, inputs, regulated, groups)

    params = {
        "b_regulated": [b["is_regulated"] for b in budget_rows],
        "b_group": [b["area_group"] for b in budget_rows],
        "b_price": [b["max_price"] for b in budget_rows],
        "cap": max_price_manwon,
        "regions": regions,
        "built_after": built_after,
        "min_hh": min_hh,
        "min_sample": min_sample,
        "limit": limit,
    }
    # 후보 판정 한 곳: 가격(표본) → 예산 → 세대수. 세대수 NULL은 min_households가 있으면 빠지고 따로 센다
    base = sql.SQL(
        """
        WITH b AS (
          SELECT * FROM unnest(%(b_regulated)s::boolean[], %(b_group)s::text[], %(b_price)s::int[])
            AS b (is_regulated, area_group, max_price)
        ), base AS (
          SELECT s.*,
                 LEAST(b.max_price, %(cap)s::int) AS budget_manwon,
                 LEAST(b.max_price, %(cap)s::int) - s.median_price_manwon AS budget_headroom_manwon,
                 s.sample_size >= %(min_sample)s AS priced,
                 s.median_price_manwon <= LEAST(b.max_price, %(cap)s::int) AS within_budget,
                 %(min_hh)s = 0 OR COALESCE(s.households >= %(min_hh)s, false) AS households_ok,
                 %(min_hh)s > 0 AND s.households IS NULL AS households_unknown
          FROM v_danji_summary s
          JOIN b USING (is_regulated, area_group)
          WHERE s.lawd_cd5 = ANY(%(regions)s::text[]) AND s.scope = 'full'
            AND (%(built_after)s::int IS NULL OR s.built_year >= %(built_after)s::int)
        )
        """
    )
    # 빠진 이유는 가격(표본) → 예산 → 세대수 순서로 하나만 센다. 합이 considered와 같다
    counts = fetch_one(conn, base + sql.SQL(
        """
        SELECT count(*) AS considered,
               count(*) FILTER (WHERE NOT priced) AS excluded_insufficient_recent_sales,
               count(*) FILTER (WHERE priced AND NOT within_budget) AS excluded_over_budget,
               count(*) FILTER (WHERE priced AND within_budget AND households_unknown) AS excluded_unknown_households,
               count(*) FILTER (WHERE priced AND within_budget AND NOT households_ok AND NOT households_unknown)
                 AS excluded_below_min_households,
               count(*) FILTER (WHERE priced AND within_budget AND households_ok) AS matched,
               count(*) FILTER (WHERE priced AND within_budget AND households_ok AND low_confidence)
                 AS matched_low_confidence
        FROM base
        """
    ), params)
    # 표본 3건 미만은 빼지 않되 뒤로 보낸다. 1~2건짜리 중위가 분위 양 끝을 차지하기 쉽다
    rows = fetch(conn, base + sql.SQL(
        """
        SELECT * FROM base
        WHERE priced AND within_budget AND households_ok
        ORDER BY low_confidence, {order}, danji_id, {area_order}
        LIMIT %(limit)s
        """
    ).format(order=SORTS[sort_by], area_order=AREA_ORDER), params)
    data = [{**pick(r, CANDIDATE_COLUMNS),
             "budget_manwon": r["budget_manwon"], "budget_headroom_manwon": r["budget_headroom_manwon"]}
            for r in rows]

    notes = [
        "시세는 최근 6개월 시세용 매매(해제·직거래·이상치 제외)의 중위값이다. sample_size < 3이면 low_confidence이고 "
        "정렬에서 뒤로 보낸다(빼지는 않는다)",
        "후보는 median_price_manwon ≤ budget_manwon인 단지·평형그룹이다. budget_manwon은 규제 여부·평형그룹별 "
        "최대 매수가(compute_budget과 같은 함수)이고 max_price_manwon을 주면 그보다 낮게 잡는다",
        "임대(시세용 매매 없음)·토지임대부 단지는 빠져 있다(scope 'full'만)",
    ]
    if counts["excluded_unknown_households"]:
        notes.append(f"세대수(건축물대장)를 모르는 {counts['excluded_unknown_households']}칸이 min_households 때문에 빠졌다. "
                     "보려면 min_households=0으로 다시 부른다")
    if any(r["households_is_parcel_total"] for r in rows):
        notes.append("households_is_parcel_total이 true면 세대수가 같은 필지 여러 단지의 합계다(get_danji의 bldg_danji_cnt)")
    return jsonable({
        "data": data,
        "meta": base_meta(
            snap,
            units=SUMMARY_UNITS,
            period=latest_period(snap),
            filters={
                "lawd_cd5": "전체 83개" if not lawd_cd5 else regions,
                "area_group": groups,
                "min_households": min_hh,
                "built_after": built_after,
                "min_sample_size": min_sample,
                "max_price_manwon": max_price_manwon,
                "sort_by": sort_by,
            },
            budget_inputs=inputs,
            budget=[{k: b[k] for k in ("is_regulated", "area_group", "max_price", "loan_amount",
                                       "binding_factor", "binding_detail")} for b in budget_rows],
            counts={**counts, "returned": len(data)},
            truncated=counts["matched"] > len(data),
            limit=limit,
            low_confidence_rows=sum(r["low_confidence"] for r in data),
            data_completeness="partial" if any(r["data_completeness"] == "partial" for r in data) else "complete",
            notes=notes,
        ),
    })


def find_danji(conn: psycopg.Connection, query: str, lawd_cd5: str | None = None, limit: int | None = None) -> dict:
    limit = clamp_limit(limit, default=20)
    raw = (query or "").strip()
    norm = normalize_name(raw)
    if not norm:
        raise ToolError("단지명 검색어가 비었다('아파트'·공백·괄호는 빼고 찾는다)")
    code = check_region(lawd_cd5) if lawd_cd5 else None
    rows = fetch(
        conn,
        """
        WITH hit AS (
          SELECT danji_id FROM danji
          WHERE (name_norm ILIKE %(pat_norm)s OR name ILIKE %(pat_raw)s)
            AND (%(code)s::text IS NULL OR lawd_cd5 = %(code)s::text)
        )
        SELECT d.danji_id, d.apt_seq, d.name, d.lawd_cd5, lw.sido, lw.sigungu, lw.dong, d.jibun, d.built_year,
               d.households, COALESCE(d.bldg_danji_cnt >= 2, false) AS households_is_parcel_total,
               st.scope, st.scope_reason,
               EXISTS (SELECT 1 FROM mv_danji_latest l WHERE l.danji_id = d.danji_id AND l.sample_size > 0)
                 AS has_recent_sale
        FROM danji d
        JOIN v_danji_status st USING (danji_id)
        LEFT JOIN lawd lw ON lw.lawd_cd = d.lawd_cd
        WHERE d.danji_id = ANY(ARRAY(SELECT danji_id FROM hit))
        ORDER BY d.name_norm = %(norm)s DESC, d.lawd_cd5, d.name, d.danji_id
        LIMIT %(limit)s
        """,
        {"pat_norm": f"%{escape_like(norm)}%", "pat_raw": f"%{escape_like(raw)}%", "code": code,
         "norm": norm, "limit": limit + 1},
    )
    truncated = len(rows) > limit
    return jsonable({
        "data": rows[:limit],
        "meta": {
            "query": raw,
            "lawd_cd5": code,
            "count": min(len(rows), limit),
            "truncated": truncated,
            "notes": [
                "이름은 찾는 데만 쓴다. 같은 이름이 여러 곳이면 시군구·동·지번으로 사용자와 확인하고 danji_id로 이어서 조회한다",
                "scope: full = 통계·후보 대상, danji_only = 토지임대부(단지 시세만), excluded = 임대 등 매수 불가"
                "(scope_reason: no_market_sale = 5년간 시세용 매매 없음, rental = 임대로 확인)",
                "has_recent_sale이 false면 최근 6개월 시세가 없다",
            ],
        },
    })


def get_danji(conn: psycopg.Connection, danji_id: int) -> dict:
    snap = snapshot(conn)
    info = fetch_one(
        conn,
        """
        SELECT d.danji_id, d.apt_seq, d.name, d.lawd_cd5, lw.sido, lw.sigungu, lw.dong, d.jibun, d.built_year,
               d.households, d.buildings, d.floors_max, d.far, d.bcr, d.parking, d.bldg_danji_cnt,
               COALESCE(d.bldg_danji_cnt >= 2, false) AS households_is_parcel_total,
               (SELECT m.reason FROM v_bldg_missing m WHERE m.danji_id = d.danji_id) AS bldg_missing_reason,
               st.scope, st.scope_reason, st.flag, st.flag_note, st.market_sale_count, st.first_market_sale,
               is_regulated(d.lawd_cd5) AS is_regulated
        FROM danji d
        JOIN v_danji_status st USING (danji_id)
        LEFT JOIN lawd lw ON lw.lawd_cd = d.lawd_cd
        WHERE d.danji_id = %s
        """,
        (danji_id,),
    )
    if info is None:
        raise ToolError(f"danji_id {danji_id} 단지가 없다. find_danji나 search_candidates로 찾을 것")
    info["region_name"] = REGIONS.get(info["lawd_cd5"])
    info["zones"] = zones_by_region(conn, [info["lawd_cd5"]])[info["lawd_cd5"]]
    groups = fetch(
        conn,
        sql.SQL("SELECT * FROM v_danji_summary WHERE danji_id = %s ORDER BY {}").format(AREA_ORDER),
        (danji_id,),
    )
    for g in groups:
        for c in ("danji_id", "apt_seq", "name", "lawd_cd5", "sido", "sigungu", "dong", "jibun", "built_year",
                  "households", "bldg_danji_cnt", "households_is_parcel_total", "is_regulated", "scope",
                  "period_from", "period_to"):
            g.pop(c)
    trades = fetch(
        conn,
        """
        SELECT b.deal_date, b.area_excl, b.area_group, b.floor, b.price_manwon, round(b.price_per_m2, 1) AS price_per_m2,
               b.dealing_type, b.registered_date, b.is_direct, b.is_outlier, b.is_low_floor, b.is_basis
        FROM v_sale_basis b
        WHERE b.danji_id = %s AND NOT b.is_canceled
        ORDER BY b.deal_date DESC, b.id DESC
        LIMIT %s
        """,
        (danji_id, RECENT_TRADES_IN_DANJI),
    )
    notes = []
    if info["scope"] == "excluded":
        notes.append("이 단지는 분석 범위 밖이다(scope_reason: no_market_sale = 5년간 시세용 매매 없음·임대로 보임, "
                     "rental = 임대로 확인). 시세·비교 통계가 없고 매수 후보가 아니다")
    elif info["scope"] == "danji_only":
        notes.append("토지임대부 등 땅값이 빠진 가격이라 분위·회복률 비교·지역 통계·후보에서 빠진다. 자체 시세만 참고한다")
    if info["households_is_parcel_total"]:
        notes.append(f"세대수·동수·용적률 등 건축물대장 값은 같은 필지 {info['bldg_danji_cnt']}개 단지의 합계다")
    if info["households"] is None:
        notes.append(f"건축물대장 값이 없다(이유: {info['bldg_missing_reason']})")
    notes.append("by_area_group의 시세는 최근 6개월 중위값이다. 근거 거래는 get_recent_trades(danji_id, area_group, "
                 "since=period 시작일)에서 is_basis=true인 행이다")
    return jsonable({
        "data": {"danji": info, "by_area_group": groups, "recent_trades": trades},
        "meta": base_meta(
            snap,
            units=SUMMARY_UNITS,
            period=latest_period(snap),
            data_completeness="partial" if any(g["data_completeness"] == "partial" for g in groups) else "complete",
            recent_trades_note=f"최근 매매 {RECENT_TRADES_IN_DANJI}건(해제 제외). is_basis=false는 시세에서 빠진 거래",
            notes=notes,
        ),
    })


def get_price_history(conn: psycopg.Connection, danji_id: int, area_group: str | None = None,
                      months: int = 36) -> dict:
    snap = snapshot(conn)
    danji_exists(conn, danji_id)
    months = max(1, min(int(months), MAX_MONTHS))
    group = check_area_groups([area_group], [])[0] if area_group else None
    params = {"id": danji_id, "group": group, "as_of": snap["as_of"], "months": months}
    window = sql.SQL(
        """
        WITH w AS (
          SELECT (date_trunc('month', %(as_of)s::date) - make_interval(months => %(months)s - 1))::date AS m_from,
                 date_trunc('month', %(as_of)s::date)::date AS m_to
        )
        """
    )
    rows = fetch(conn, window + sql.SQL(
        """
        SELECT area_group, to_char(month, 'YYYY-MM') AS month, sample_size, low_confidence,
               median_price_manwon, min_price_manwon, max_price_manwon,
               median_price_per_m2, per_pyeong(median_price_per_m2) AS median_price_per_pyeong, data_completeness
        FROM mv_danji_price_monthly, w
        WHERE danji_id = %(id)s AND (%(group)s::text IS NULL OR area_group = %(group)s::text) AND month >= w.m_from
        ORDER BY {}, month
        """
    ).format(AREA_ORDER), params)
    summary = fetch(conn, window + sql.SQL(
        """
        SELECT area_group, count(*) AS months_with_trades, sum(sample_size)::int AS sample_size,
               count(*) FILTER (WHERE low_confidence) AS low_confidence_months
        FROM mv_danji_price_monthly, w
        WHERE danji_id = %(id)s AND (%(group)s::text IS NULL OR area_group = %(group)s::text) AND month >= w.m_from
        GROUP BY area_group
        ORDER BY {}
        """
    ).format(AREA_ORDER), params)
    span = fetch_one(conn, window + sql.SQL("SELECT to_char(m_from, 'YYYY-MM') AS a, to_char(m_to, 'YYYY-MM') AS b FROM w"),
                     params)
    return jsonable({
        "data": rows,
        "meta": base_meta(
            snap,
            units=("price", "price_per_m2", "price_per_pyeong"),
            period=(span["a"], span["b"]),
            area_group=group or "전체",
            months=months,
            by_area_group=summary,
            data_completeness="partial" if any(r["data_completeness"] == "partial" for r in rows) else "complete",
            notes=[
                "월별 시세용 매매(해제·직거래·이상치 제외) 중위값이다. 거래가 없는 달은 행이 없다",
                "data_completeness가 partial인 최근 3개월은 신고가 덜 들어와 거래가 적게 보인다. 시장 위축으로 읽지 않는다",
                "평형그룹을 섞지 않는다. 수집 기간은 2021-01부터다",
            ],
        ),
    })


def get_recent_trades(
    conn: psycopg.Connection,
    danji_id: int | None = None,
    lawd_cd5: str | None = None,
    kind: str = "sale",
    area_group: str | None = None,
    since: date | None = None,
    include_canceled: bool = False,
    limit: int | None = None,
) -> dict:
    snap = snapshot(conn)
    if (danji_id is None) == (lawd_cd5 is None):
        raise ToolError("danji_id와 lawd_cd5 중 하나만 준다")
    if kind not in ("sale", "rent"):
        raise ToolError("kind는 sale(매매) 또는 rent(전월세)다")
    if danji_id is not None:
        danji_exists(conn, danji_id)
    code = check_region(lawd_cd5) if lawd_cd5 else None
    group = check_area_groups([area_group], [])[0] if area_group else None
    limit = clamp_limit(limit)
    params = {"id": danji_id, "code": code, "group": group, "since": since, "inc": include_canceled,
              "limit": limit + 1}
    # 단지 하나는 = 조건이어야 v_sale_basis·v_danji_status 안의 단지별 집계까지 조건이 내려간다(8ms).
    # 시군구는 단지 id 배열로 거는 것이 가장 빠르다(약 1.5초. 집계는 전체를 돈다. lawd_cd5 조건은 6초, LATERAL은 9초)
    if danji_id is not None:
        target = sql.SQL("b.danji_id = %(id)s::bigint")
    else:
        target = sql.SQL("b.danji_id = ANY(ARRAY(SELECT danji_id FROM danji WHERE lawd_cd5 = %(code)s::bpchar))")
    where = sql.SQL(
        """
        WHERE {target}
          AND (%(group)s::text IS NULL OR b.area_group = %(group)s::text)
          AND (%(since)s::date IS NULL OR b.deal_date >= %(since)s::date)
        """
    ).format(target=target)
    if kind == "sale":
        rows = fetch(conn, sql.SQL(
            """
            SELECT b.deal_date, b.danji_id, d.name AS danji_name, b.area_excl, b.area_group, b.floor, b.price_manwon,
                   round(b.price_per_m2, 1) AS price_per_m2, b.dealing_type, b.is_canceled, b.canceled_date,
                   b.registered_date, b.is_direct, b.is_outlier, b.is_low_floor, b.is_basis
            FROM v_sale_basis b
            JOIN danji d USING (danji_id)
            {where} AND (%(inc)s OR NOT b.is_canceled)
            ORDER BY b.deal_date DESC, b.id DESC
            LIMIT %(limit)s
            """
        ).format(where=where), params)
        notes = [
            "is_basis=true인 거래만 시세(중위가)에 쓴다. false 이유: is_direct(직거래), is_outlier(같은 단지·평형·연도 "
            "m2당 중위의 50% 미만·200% 초과), is_canceled(해제), 분석 범위 밖 단지",
            "is_low_floor(층 0 이하)는 표시만 하고 시세에서 빼지 않는다",
            "거래 id는 원본 값이 바뀌면(등기·해제) 바뀌므로 계약일·층·면적·금액으로 거래를 가리킨다",
        ]
        excludes_canceled = not include_canceled
    else:
        rows = fetch(conn, sql.SQL(
            """
            SELECT b.deal_date, b.danji_id, d.name AS danji_name, b.area_excl, b.area_group, b.floor,
                   b.deposit_manwon, b.monthly_manwon, round(b.deposit_per_m2, 1) AS deposit_per_m2,
                   b.contract_type, b.contract_term, b.renewal_right,
                   b.is_jeonse, b.is_renewal, b.is_before_conversion, b.is_basis, b.is_jeonse_basis
            FROM v_rent_basis b
            JOIN danji d USING (danji_id)
            {where}
            ORDER BY b.deal_date DESC, b.id DESC
            LIMIT %(limit)s
            """
        ).format(where=where), params)
        notes = [
            "전월세 응답에는 해제여부가 없어 해제 거래를 거를 수 없다",
            "is_jeonse_basis=true인 거래만 전세 시세·전세가율에 쓴다: 월세 0, 갱신 계약 제외, 분양전환 전 계약 제외",
        ]
        excludes_canceled = False
    truncated = len(rows) > limit
    return jsonable({
        "data": rows[:limit],
        "meta": base_meta(
            snap,
            units=("price", "area", "price_per_m2"),
            excludes_canceled=excludes_canceled,
            filters={"danji_id": danji_id, "lawd_cd5": code, "kind": kind, "area_group": group, "since": since,
                     "include_canceled": include_canceled},
            count=min(len(rows), limit),
            truncated=truncated,
            limit=limit,
            notes=notes,
        ),
    })


def get_region_stats(conn: psycopg.Connection, lawd_cd5: str, months: int = 24,
                     area_group: list[str] | None = None) -> dict:
    snap = snapshot(conn)
    prof = profile(conn)
    code = check_region(lawd_cd5)
    groups = check_area_groups(area_group, prof["preferred_area_groups"])
    months = max(1, min(int(months), MAX_MONTHS))
    params = {"code": code, "groups": groups, "as_of": snap["as_of"], "months": months}
    window = sql.SQL(
        """
        WITH w AS (
          SELECT (date_trunc('month', %(as_of)s::date) - make_interval(months => %(months)s - 1))::date AS m_from,
                 date_trunc('month', %(as_of)s::date)::date AS m_to
        )
        """
    )
    monthly = fetch(conn, window + sql.SQL(
        """
        SELECT area_group, to_char(month, 'YYYY-MM') AS month, median_price_per_m2,
               per_pyeong(median_price_per_m2) AS median_price_per_pyeong, median_price_manwon,
               sample_size, low_confidence, danji_count, trade_count, data_completeness
        FROM mv_region_monthly, w
        WHERE lawd_cd5 = %(code)s AND area_group = ANY(%(groups)s::text[]) AND month >= w.m_from
        ORDER BY {}, month
        """
    ).format(AREA_ORDER), params)
    volume = fetch(conn, window + sql.SQL(
        """
        SELECT to_char(month, 'YYYY-MM') AS month, trade_count, sample_size, data_completeness
        FROM v_region_volume_monthly, w
        WHERE lawd_cd5 = %(code)s AND month >= w.m_from
        ORDER BY month
        """
    ), params)
    distribution = fetch(conn, sql.SQL(
        """
        SELECT * FROM v_region_distribution
        WHERE lawd_cd5 = %(code)s AND area_group = ANY(%(groups)s::text[])
        ORDER BY {}
        """
    ).format(AREA_ORDER), params)
    for d in distribution:
        d.pop("lawd_cd5")
    span = fetch_one(conn, window + sql.SQL("SELECT to_char(m_from, 'YYYY-MM') AS a, to_char(m_to, 'YYYY-MM') AS b FROM w"),
                     params)
    reg = fetch_one(conn, "SELECT is_regulated(%s) AS r", (code,))["r"]
    return jsonable({
        "data": {
            "region": {"lawd_cd5": code, "name": REGIONS[code], "is_regulated": reg,
                       "zones": zones_by_region(conn, [code])[code]},
            "monthly": monthly,
            "volume": volume,
            "distribution": distribution,
        },
        "meta": base_meta(
            snap,
            units=("price", "price_per_m2", "price_per_pyeong", "recovery_pct"),
            period=(span["a"], span["b"]),
            distribution_period=f"{latest_period(snap)[0]} ~ {latest_period(snap)[1]}",
            area_group=groups,
            months=months,
            data_completeness="partial" if any(r["data_completeness"] == "partial" for r in monthly + volume)
            else "complete",
            notes=[
                "monthly: 평형그룹별 월 시세. sample_size는 시세에 쓴 거래 수, trade_count는 해제를 뺀 거래량"
                "(직거래·이상치 포함). 거래가 없는 달은 행이 없다",
                "volume: 평형그룹 합계 거래량. 임대·토지임대부 단지는 빠져 있다",
                "distribution: 최근 6개월 단지별 m2당 중위가와 전고점 대비 회복률의 분위수(단지 하나가 값 하나). "
                "price_danji_count가 작으면 분위수가 거칠다",
                "data_completeness가 partial인 최근 3개월은 신고가 덜 들어와 거래량이 적게 보인다. 시장 위축으로 읽지 않는다",
            ],
        ),
    })


def compare_danji(conn: psycopg.Connection, danji_ids: list[int], area_group: list[str] | None = None) -> dict:
    snap = snapshot(conn)
    ids = list(dict.fromkeys(danji_ids))
    if not 2 <= len(ids) <= MAX_COMPARE:
        raise ToolError(f"비교할 단지는 2~{MAX_COMPARE}개다")
    groups = check_area_groups(area_group, list(AREA_GROUPS))
    danji = fetch(
        conn,
        """
        SELECT d.danji_id, d.apt_seq, d.name, d.lawd_cd5, lw.sigungu, lw.dong, d.jibun, d.built_year, d.households,
               d.bldg_danji_cnt, d.floors_max, d.far, d.bcr, d.parking,
               st.scope, st.scope_reason, is_regulated(d.lawd_cd5) AS is_regulated
        FROM danji d
        JOIN v_danji_status st USING (danji_id)
        LEFT JOIN lawd lw ON lw.lawd_cd = d.lawd_cd
        WHERE d.danji_id = ANY(%(ids)s::bigint[])
        ORDER BY array_position(%(ids)s::bigint[], d.danji_id)
        """,
        {"ids": ids},
    )
    missing = sorted(set(ids) - {d["danji_id"] for d in danji})
    if missing:
        raise ToolError(f"없는 danji_id: {missing}. find_danji로 찾을 것")
    metrics = fetch(
        conn,
        sql.SQL(
            """
            SELECT * FROM v_danji_summary
            WHERE danji_id = ANY(%(ids)s::bigint[]) AND area_group = ANY(%(groups)s::text[])
            ORDER BY {}, array_position(%(ids)s::bigint[], danji_id)
            """
        ).format(AREA_ORDER),
        {"ids": ids, "groups": groups},
    )
    notes = ["같은 평형그룹끼리만 비교한다. 시세는 최근 6개월 중위값, sample_size < 3이면 low_confidence"]
    excluded = [d["name"] for d in danji if d["scope"] == "excluded"]
    if excluded:
        notes.append(f"분석 범위 밖(임대 등) 단지는 시세 행이 없다: {excluded}")
    if any((d["bldg_danji_cnt"] or 0) >= 2 for d in danji):
        notes.append("bldg_danji_cnt가 2 이상이면 세대수 등 건축물대장 값은 같은 필지 단지들의 합계다")
    return jsonable({
        "data": {"danji": danji, "by_area_group": [pick(m, COMPARE_COLUMNS) for m in metrics]},
        "meta": base_meta(
            snap,
            units=SUMMARY_UNITS,
            period=latest_period(snap),
            area_group=groups,
            data_completeness="partial" if any(m["data_completeness"] == "partial" for m in metrics) else "complete",
            notes=notes,
        ),
    })
