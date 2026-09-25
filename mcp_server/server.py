"""MCP 서버: 도구 등록과 설명문. 조회 로직은 mcp_server.tools에 있다.

도구 하나가 질문 하나에 대응하고 정제 규칙(해제 제외, 전용면적, 평형그룹, 임대 제외)은 도구 안에 있다.
범용 SQL 실행 도구는 두지 않는다(SPEC Phase 3). 도구 호출마다 읽기 전용 연결을 열고 닫는다(약 15ms).
"""

import json
from datetime import date
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from mcp_server import tools
from mcp_server.db import connect

AreaGroup = Literal["XS", "S", "M", "L", "XL"]
AREA_GROUP_HELP = "평형그룹(전용면적, 상한 포함): XS ≤40, S 40~60(59타입), M 60~85(74~84타입), L 85~102, XL >102"
LIMIT_HELP = "돌려줄 최대 행 수. 기본 50, 최대 200(넘으면 200)"

INSTRUCTIONS = """\
수도권(서울·인천·경기 83개 시군구) 아파트 실거래 DB(2021-01~). 무주택자가 살 아파트 후보를 근거 있게 좁히는 도구다.
목적은 싼 단지가 아니라 저평가됐지만 가치 있는 단지(싼 이유가 사라질 단지)를 찾는 것이다.

흐름: compute_budget(예산과 그 이유) → search_candidates(예산 안 후보) → get_danji / compare_danji(상세·비교)
→ get_recent_trades(근거 거래 확인). 사용자가 단지 이름을 말하면 find_danji로 danji_id를 찾는다.
시군구 코드(lawd_cd5)를 모르면 list_regions.

원칙
- 값을 직접 계산하지 않는다. 평당가·전세가율·분위·회복률·예산·차이는 모두 도구가 준 값을 인용한다
- 금액은 만원, 면적은 전용 m2(meta.units). 억으로 바꿔 말할 때만 1억 = 10,000만원
- sample_size < 3(low_confidence)인 시세는 단정하지 않는다. 표본 수를 함께 말한다
- data_completeness = partial(최근 3개월)은 신고가 덜 들어온 구간이다. 거래 감소를 시장 위축으로 읽지 않는다
- 해제 거래는 모든 집계에서 빠져 있다. 임대·토지임대부 단지는 후보와 비교 통계에서 빠진다(scope)
- 주장은 get_recent_trades의 거래(계약일·층·면적·금액)로 확인할 수 있어야 한다
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

mcp = MCPServer(name="apartment", instructions=INSTRUCTIONS)


def respond(result: dict[str, Any]) -> CallToolResult:
    """구조화 결과와 같은 내용의 JSON 텍스트. 들여쓰기를 빼 모델이 읽는 분량을 30% 안팎 줄이고 한글은 그대로 둔다."""
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result)


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "시군구·법정동 이름으로 5자리 시군구 코드(lawd_cd5)와 규제지역 여부를 찾는다. "
        "다른 도구에 lawd_cd5를 넘겨야 하는데 코드를 모를 때 쓴다. 검색어를 비우면 대상 83개 시군구 전체를 준다. "
        "동 이름으로 찾으면 그 동이 속한 시군구 코드가 나온다(같은 동 이름이 여러 시군구에 있을 수 있다). "
        "단지를 찾을 때는 쓰지 않는다(find_danji)."
    ),
)
def list_regions(
    query: Annotated[str | None, Field(description="예: '분당', '서대문구', '정자동', '수원 영통'. 여러 단어는 모두 포함")] = None,
    limit: Annotated[int | None, Field(description="동 결과 최대 행 수. 기본 50, 최대 200")] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.list_regions(conn, query, limit))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "자기자본·소득으로 살 수 있는 최대 매수가(max_price), 대출액, 예산을 묶는 한도(binding_factor: LTV·DSR·CAP·EQUITY)와 "
        "취득세·필요 현금을 계산한다. 인자를 비우면 분석 기준값(analysis_profile: 자기자본·연소득·무주택 세대주)을 쓴다. "
        "lawd_cd5를 주면 그 시군구의 규제 여부로, 비우면 규제지역·비규제지역 두 경우를 준다. "
        "예산을 묻거나 지역·소득이 바뀌면 예산이 어떻게 되는지 볼 때 쓴다. 대출 한도·세금을 직접 계산하지 말고 이 값을 인용할 것. "
        "search_candidates에 이 결과를 옮겨 넣을 필요는 없다(같은 함수로 예산을 직접 계산한다)."
    ),
)
def compute_budget(
    equity_manwon: Annotated[int | None, Field(ge=0, description="자기자본(만원). 비우면 분석 기준값")] = None,
    annual_income_manwon: Annotated[int | None, Field(ge=0, description="차주 연소득(만원, DSR). 비우면 분석 기준값")] = None,
    lawd_cd5: Annotated[str | None, Field(description="시군구 5자리 코드. 비우면 규제·비규제 두 경우")] = None,
    household_income_manwon: Annotated[
        int | None, Field(ge=0, description="부부합산 연소득(서민·실수요자 요건). 비우면 기준값, 연소득만 주면 연소득과 같다고 본다")
    ] = None,
    homeless_household_head: Annotated[bool | None, Field(description="무주택 세대주 여부. 비우면 기준값")] = None,
    area_group: Annotated[AreaGroup | None, Field(description=AREA_GROUP_HELP + ". L·XL은 농어촌특별세. 비우면 M")] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.compute_budget(conn, equity_manwon, annual_income_manwon, lawd_cd5,
                                            household_income_manwon, homeless_household_head, area_group))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "예산 안에서 조건(지역·평형·세대수·준공)에 맞는 매수 후보 단지·평형을 찾을 때 쓴다. "
        "예산은 서버가 compute_budget과 같은 함수로 시군구(규제 여부)·평형그룹마다 계산하고, "
        "최근 6개월 시세용 매매 중위가가 예산 이하인 것만 돌려준다(budget_manwon, budget_headroom_manwon). "
        "예산을 어림잡아 넣지 말 것: max_price_manwon은 사용자가 예산보다 낮은 상한을 원할 때만 준다. "
        "기본 정렬은 같은 시군구·평형그룹 안 가격 분위가 낮은 순(저평가 후보부터)이다. "
        "임대·토지임대부 단지는 빠져 있다. meta.counts에서 세대수를 몰라서·예산 초과로·최근 거래 부족으로 빠진 수를 확인할 것. "
        "단지를 이미 특정했으면 get_danji, 이름만 알면 find_danji. "
        "분위·회복률 차이·전세가율은 계산하지 말고 인용하고, sample_size < 3(low_confidence)이면 시세로 단정하지 말 것."
    ),
)
def search_candidates(
    lawd_cd5: Annotated[list[str] | None, Field(description="시군구 5자리 코드 목록. 비우면 83개 전체")] = None,
    area_group: Annotated[list[AreaGroup] | None, Field(description=AREA_GROUP_HELP + ". 비우면 기준값(S, M)")] = None,
    min_households: Annotated[
        int | None, Field(ge=0, description="최소 세대수(건축물대장). 비우면 기준값 300. 0이면 세대수를 모르는 단지도 포함")
    ] = None,
    built_after: Annotated[int | None, Field(description="이 연도 이후(포함) 준공. 예: 2000")] = None,
    max_price_manwon: Annotated[
        int | None, Field(ge=0, description="사용자가 따로 두는 가격 상한(만원). 계산한 예산보다 높으면 예산이 적용된다")
    ] = None,
    equity_manwon: Annotated[int | None, Field(ge=0, description="예산 계산용 자기자본(만원). 비우면 기준값")] = None,
    annual_income_manwon: Annotated[int | None, Field(ge=0, description="예산 계산용 연소득(만원). 비우면 기준값")] = None,
    household_income_manwon: Annotated[int | None, Field(ge=0, description="부부합산 연소득(만원). 비우면 기준값")] = None,
    homeless_household_head: Annotated[bool | None, Field(description="무주택 세대주 여부. 비우면 기준값")] = None,
    min_sample_size: Annotated[int, Field(ge=1, description="최근 6개월 시세용 매매 최소 건수. 3이면 low_confidence 제외")] = 1,
    sort_by: Annotated[
        Literal["price_percentile", "median_price_desc", "recovery_vs_region"],
        Field(description="price_percentile: 구 안에서 싼 순 / median_price_desc: 예산 안에서 비싼 순 / "
                          "recovery_vs_region: 전고점 대비 회복률이 구 중위보다 많이 뒤처진 순"),
    ] = "price_percentile",
    limit: Annotated[int | None, Field(description=LIMIT_HELP)] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.search_candidates(
            conn, lawd_cd5, area_group, min_households, built_after, max_price_manwon, equity_manwon,
            annual_income_manwon, household_income_manwon, homeless_household_head, min_sample_size, sort_by, limit,
        ))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "단지명(일부)으로 danji_id를 찾는다. 사용자가 단지 이름을 말했을 때 먼저 쓴다. "
        "공백·'아파트'·괄호는 무시한다. 같은 이름이 여러 곳이면 시군구·동·지번으로 사용자와 확인한다. "
        "scope가 excluded인 단지(임대 등)는 매수 후보가 아니다. 조건으로 찾을 때는 search_candidates."
    ),
)
def find_danji(
    query: Annotated[str, Field(min_length=1, description="단지명 또는 그 일부. 예: 'DMC파크뷰자이', '은마'")],
    lawd_cd5: Annotated[str | None, Field(description="시군구 5자리 코드로 좁힐 때")] = None,
    limit: Annotated[int | None, Field(description="기본 20, 최대 200")] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.find_danji(conn, query, lawd_cd5, limit))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "특정 단지의 상세: 주소, 건축물대장(세대수·동수·최고층·용적률·건폐율·주차), 분석 범위(scope), 규제지역, "
        "평형그룹별 최근 6개월 매매 중위가·평당가·전세가율·가격 분위·전고점 대비 회복률, 최근 매매 10건. "
        "danji_id를 알 때 쓴다. 조건으로 찾을 때는 search_candidates, 이름만 알면 find_danji를 먼저. "
        "시세는 최근 6개월 중위값이며 sample_size가 3 미만이면 신뢰하지 말 것."
    ),
)
def get_danji(danji_id: Annotated[int, Field(description="단지 id(find_danji·search_candidates 결과)")]) -> CallToolResult:
    with connect() as conn:
        return respond(tools.get_danji(conn, danji_id))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "단지의 평형그룹별 월별 매매 중위가 시계열(시세용 매매만). 가격 추이나 하락장 이후 흐름을 보여 줄 때 쓴다. "
        "전고점 대비 회복률은 직접 계산하지 말고 get_danji의 recovery_pct를 쓸 것. "
        "월 표본이 1~2건인 달이 많으니 low_confidence인 달은 추세 판단에서 가볍게 본다."
    ),
)
def get_price_history(
    danji_id: Annotated[int, Field(description="단지 id")],
    area_group: Annotated[AreaGroup | None, Field(description=AREA_GROUP_HELP + ". 비우면 모든 평형그룹")] = None,
    months: Annotated[int, Field(ge=1, description="최근 몇 개월. 기본 36, 최대 120(수집은 2021-01부터)")] = 36,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.get_price_history(conn, danji_id, area_group, months))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "원시 거래 목록(계약일·층·전용면적·금액·거래유형·등기일, 시세 포함 여부 is_basis). 시세의 근거를 확인하거나 "
        "사용자가 실제 거래를 보고 싶을 때 쓴다. danji_id와 lawd_cd5 중 하나만 준다. "
        "get_danji 최근 6개월 중위가의 근거는 danji_id, area_group, since=기간 시작일로 불러 is_basis=true 행을 본다. "
        "해제 거래는 기본 제외(include_canceled=true면 포함). 전월세는 kind='rent'. "
        "이 목록으로 통계를 직접 계산하지 말 것(시세는 get_danji·search_candidates 값을 쓴다)."
    ),
)
def get_recent_trades(
    danji_id: Annotated[int | None, Field(description="단지 id")] = None,
    lawd_cd5: Annotated[str | None, Field(description="시군구 5자리 코드(시군구 전체의 최근 거래)")] = None,
    kind: Annotated[Literal["sale", "rent"], Field(description="sale = 매매, rent = 전월세")] = "sale",
    area_group: Annotated[AreaGroup | None, Field(description=AREA_GROUP_HELP)] = None,
    since: Annotated[date | None, Field(description="이 날짜 이후(포함) 계약. YYYY-MM-DD")] = None,
    include_canceled: Annotated[bool, Field(description="매매 해제 거래 포함 여부. 기본 false")] = False,
    limit: Annotated[int | None, Field(description=LIMIT_HELP)] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.get_recent_trades(conn, danji_id, lawd_cd5, kind, area_group, since, include_canceled,
                                               limit))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "시군구 통계: 평형그룹별 월별 m2당·평당 중위가와 거래량 추이, 시군구 전체 거래량, "
        "최근 6개월 단지별 가격 분포(분위수)와 전고점 대비 회복률 분포. 지역 시세 수준과 흐름을 볼 때 쓴다. "
        "단지끼리 비교는 compare_danji. 최근 3개월(partial)의 거래량 감소는 신고 지연이다."
    ),
)
def get_region_stats(
    lawd_cd5: Annotated[str, Field(description="시군구 5자리 코드")],
    months: Annotated[int, Field(ge=1, description="최근 몇 개월. 기본 24, 최대 120")] = 24,
    area_group: Annotated[list[AreaGroup] | None, Field(description=AREA_GROUP_HELP + ". 비우면 기준값(S, M)")] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.get_region_stats(conn, lawd_cd5, months, area_group))


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "단지 2~10개를 같은 지표로 나란히 놓는다: 평형그룹별 최근 6개월 매매 중위가·평당가·전세가율·가격 분위·회복률, "
        "세대수·준공·최고층·용적률. 후보를 좁힌 뒤 비교할 때 쓴다. 같은 평형그룹 행끼리만 비교하고 "
        "차이를 직접 계산하지 말고 값을 인용할 것."
    ),
)
def compare_danji(
    danji_ids: Annotated[list[int], Field(min_length=2, max_length=10, description="단지 id 2~10개")],
    area_group: Annotated[list[AreaGroup] | None, Field(description=AREA_GROUP_HELP + ". 비우면 전부")] = None,
) -> CallToolResult:
    with connect() as conn:
        return respond(tools.compare_danji(conn, danji_ids, area_group))
