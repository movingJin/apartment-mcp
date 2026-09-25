"""건축물대장(건축HUB 15134735) 부가정보를 단지에 붙인다. 구현 순서 4단계.

    python -m etl.bldg --dry-run          # 조회할 법정동·필지 수만 본다
    python -m etl.bldg                    # 아직 조회하지 않은 것만 받고 단지에 반영한다(재개)
    python -m etl.bldg --region 11410     # 시군구 한정
    python -m etl.bldg --force            # 받은 적 있는 것도 다시 받는다
    python -m etl.bldg --rederive         # API 없이 저장된 원문에서 파생 값을 다시 계산한다
    python -m etl.bldg --report           # 부가정보가 없는 단지 리포트(v_bldg_missing)

조회 키는 필지(danji.lawd_cd + 대지구분 + bonbun + bubun)다. 대지구분은 plat_gb_of(jibun)로 정한다(산 지번이면 1).
본번이 없거나 '0000'인 단지(택지 블록, 전월세에만 나온 산 지번, 지번 미부여)는 키가 없어 조회하지 않는다.
- 총괄표제부는 법정동 단위로 받는다(지번 없이 조회된다. 법정동 1,083개, 대부분 1~3페이지).
  표제부는 필지 단위로 받는다(법정동 단위로 받으면 단독주택까지 전부 와서 훨씬 많다)
- 원문은 bldg_recap·bldg_title에, 필지별 결과와 파생 값은 bldg_parcel에 둔다
- 한 법정동은 총괄표제부 → 그 동의 필지 표제부 순서로 받는다. 파생 값에 총괄표제부가 필요해서
  총괄표제부를 못 받은 동의 필지는 이번 실행에서 건너뛴다
- 받은 법정동·필지는 다음 실행에서 건너뛴다('not_found'도 받은 것이다). 'error'는 다시 받는다
- 일일 한도 초과 응답이 오거나 연속 5회 실패하면 멈춘다. 같은 명령을 다시 실행하면 이어서 받는다
- 실행이 끝나면 bldg_parcel 값을 danji에 반영한다(apply_to_danji). 한 필지에 단지가 여럿이면
  모든 단지에 필지 값을 넣고 bldg_danji_cnt에 단지 수를 적는다(2 이상이면 필지 합계)
"""

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.types.json import Jsonb

from etl.datagokr import DataGoKrClient, QuotaExceeded
from etl.db import connect

HUB_URL = "https://apis.data.go.kr/1613000/BldRgstHubService/"
RESULT_OK = "00"  # 실거래가 API는 '000'
PAGE_SIZE = 100  # numOfRows를 더 크게 줘도 100건씩 온다(2026-09-25 확인)
MAX_CONSECUTIVE_FAILURES = 5

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
STATUS_ERROR = "error"


@dataclass(frozen=True, order=True)
class Parcel:
    lawd_cd: str
    plat_gb: str  # 건축HUB platGbCd: '0' 대지, '1' 산
    bonbun: str
    bubun: str

    def __str__(self) -> str:
        return f"{self.lawd_cd}-{'산' if self.plat_gb == '1' else ''}{self.bonbun}-{self.bubun}"


class HubClient(DataGoKrClient):
    def __init__(self, service_key: str, *, attempts: int = 8, **kwargs):
        # 건축HUB는 수십 초 동안 빈 200·503만 주는 구간이 있다(2026-09-25 관측, 6회·약 30초 대기로는 못 넘김).
        # 8회면 대기 합계가 약 2분이다
        super().__init__(service_key, attempts=attempts, **kwargs)

    def recap(self, lawd_cd: str) -> list[dict[str, str]]:
        """법정동 하나의 총괄표제부 전체."""
        params = {"sigunguCd": lawd_cd[:5], "bjdongCd": lawd_cd[5:]}
        return self._fetch_all(HUB_URL + "getBrRecapTitleInfo", params, ok_code=RESULT_OK, page_size=PAGE_SIZE)

    def title(self, p: Parcel) -> list[dict[str, str]]:
        """필지 하나의 표제부(동 단위) 전체."""
        params = {"sigunguCd": p.lawd_cd[:5], "bjdongCd": p.lawd_cd[5:], "platGbCd": p.plat_gb,
                  "bun": p.bonbun, "ji": p.bubun}
        return self._fetch_all(HUB_URL + "getBrTitleInfo", params, ok_code=RESULT_OK, page_size=PAGE_SIZE)


# ---------------------------------------------------------------- 파생 규칙


@dataclass(frozen=True)
class BldgValues:
    recap_mgm_pk: str | None = None
    households: int | None = None
    buildings: int | None = None
    floors_max: int | None = None
    far: Decimal | None = None
    bcr: Decimal | None = None
    parking: int | None = None
    use_apr_date: date | None = None


_PARKING_FIELDS = ("indrautoutcnt", "oudrautoutcnt", "indrmechutcnt", "oudrmechutcnt")  # 옥내·옥외 자주식·기계식
_RATIO_MAX = Decimal("9999.99")  # bldg_parcel.far/bcr NUMERIC(6,2)


def _positive(item: dict[str, str], tag: str) -> Decimal | None:
    """양수만 값으로 본다. 옛 대장은 용적률·건폐율·주차수를 모르면 0으로 둔다. 숫자가 아니면 ValueError."""
    s = item.get(tag, "").replace(",", "").strip()
    if not s:
        return None
    try:
        n = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{tag}가 숫자가 아님: {s!r}") from None
    if not n.is_finite():
        raise ValueError(f"{tag}가 숫자가 아님: {s!r}")
    return n if n > 0 else None


def _int(n: Decimal | None) -> int | None:
    return None if n is None else int(n)


def _ratio(item: dict[str, str], tag: str) -> Decimal | None:
    n = _positive(item, tag)
    if n is not None and n > _RATIO_MAX:
        raise ValueError(f"{tag}가 범위를 벗어남: {n}")
    return n


def _date(value: str) -> date | None:
    """useAprDay 'YYYYMMDD'. 옛 대장은 비어 있거나 일자가 00인 경우가 있어 날짜가 아니면 None."""
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", value.strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def pick_recap(recaps: Sequence[dict[str, str]]) -> dict[str, str] | None:
    """한 필지에 총괄표제부가 여럿이면(집합/일반, 신대장/구대장이 함께 있다) 집합 > 일반,
    신대장 > 구대장, 세대수가 많은 쪽, mgmBldrgstPk 순으로 하나를 고른다."""
    if not recaps:
        return None
    return min(
        recaps,
        key=lambda r: (
            r.get("regstrgbcd") != "2",
            r.get("newoldregstrgbcd") != "1",
            -(_positive(r, "hhldcnt") or 0),
            r.get("mgmbldrgstpk", ""),
        ),
    )


def derive(recaps: Sequence[dict[str, str]], titles: Sequence[dict[str, str]]) -> BldgValues:
    """필지 하나의 총괄표제부·표제부 원문 → 단지 부가정보.

    - 총괄표제부가 있으면 세대수·용적률·건폐율·주차수는 총괄표제부 값(단지 전체 합계)을 쓴다(SPEC 주의 4)
    - 동이 하나인 단지는 총괄표제부가 없다(표본 300필지 중 절반). 이때는 표제부에서 계산한다
      - 세대수는 주거동 세대수 합
      - 용적률·건폐율·주차수는 주건축물이 1동일 때만 그 표제부 값. 여러 동의 표제부 값은 동별 값인지
        필지 값을 반복한 것인지 대장마다 달라 합하지 않는다
    - 동 수·최고층·사용승인일은 항상 표제부 주거동에서 센다. 주거동은 주건축물 중 주용도가 공동주택이고
      세대가 있는 동이다. 공동주택 용도로 등록된 상가·노유자시설동(세대 0)은 빠진다. 없으면 세대가 있는
      주건축물(주상복합·도시형생활주택은 주용도가 업무시설 등이다), 그것도 없으면 공동주택 주건축물이다
    - 0은 값 없음이다
    """
    recap = pick_recap(recaps) or {}
    main = [t for t in titles if t.get("mainatchgbcd") == "0"]
    apartments = [t for t in main if t.get("mainpurpscd") == "02000"]
    homes = (
        [t for t in apartments if _positive(t, "hhldcnt")]
        or [t for t in main if _positive(t, "hhldcnt")]
        or apartments
    )
    single = main[0] if len(main) == 1 else {}

    def title_sum(items, tags) -> int | None:
        return sum(_int(_positive(i, tag)) or 0 for i in items for tag in tags) or None

    return BldgValues(
        recap_mgm_pk=recap.get("mgmbldrgstpk"),
        households=_int(_positive(recap, "hhldcnt")) or title_sum(homes, ["hhldcnt"]),
        buildings=len(homes) or None,
        floors_max=max(filter(None, (_int(_positive(t, "grndflrcnt")) for t in homes)), default=None),
        far=_first(_ratio(recap, "vlrat"), _ratio(single, "vlrat")),
        bcr=_first(_ratio(recap, "bcrat"), _ratio(single, "bcrat")),
        parking=_first(_int(_positive(recap, "totpkngcnt")), title_sum([single], _PARKING_FIELDS)),
        use_apr_date=_date(recap.get("useaprday", ""))
        or min(filter(None, (_date(t.get("useaprday", "")) for t in homes)), default=None),
    )


def _first(*values):
    return next((v for v in values if v is not None), None)


# ---------------------------------------------------------------- 계획


@dataclass(frozen=True)
class Task:
    kind: str  # 'recap' | 'title'
    lawd_cd: str
    parcel: Parcel | None = None

    def __str__(self) -> str:
        return f"recap {self.lawd_cd}" if self.kind == "recap" else f"title {self.parcel}"


def plan_parcels(conn: psycopg.Connection, regions: Sequence[str] | None = None) -> list[Parcel]:
    """단지의 조회 키. 본번 NULL(택지 블록·산 지번)과 '0000'(지번 미부여)은 키가 아니다(v_bldg_missing과 같은 조건)."""
    query = """
        SELECT DISTINCT lawd_cd, plat_gb_of(jibun), bonbun, bubun FROM danji
        WHERE lawd_cd IS NOT NULL AND bonbun IS NOT NULL AND bonbun <> '0000' AND bubun IS NOT NULL
    """
    params: tuple = ()
    if regions:
        query += " AND left(lawd_cd, 5) = ANY(%s)"
        params = (list(regions),)
    return sorted(Parcel(*row) for row in conn.execute(query + " ORDER BY 1, 2, 3, 4", params))


def plan_tasks(parcels: Sequence[Parcel], recap_done: set[str], parcel_done: set[Parcel], force: bool) -> list[Task]:
    """법정동마다 총괄표제부 → 그 동 필지들의 표제부 순서. 받은 것은 force가 아니면 건너뛴다."""
    by_dong: dict[str, list[Parcel]] = defaultdict(list)
    for p in sorted(parcels):
        by_dong[p.lawd_cd].append(p)
    tasks = []
    for lawd_cd in sorted(by_dong):
        if force or lawd_cd not in recap_done:
            tasks.append(Task("recap", lawd_cd))
        tasks.extend(Task("title", lawd_cd, p) for p in by_dong[lawd_cd] if force or p not in parcel_done)
    return tasks


def done_sets(conn: psycopg.Connection) -> tuple[set[str], set[Parcel]]:
    recap_done = {cd for (cd,) in conn.execute("SELECT lawd_cd FROM bldg_recap_log WHERE status = %s", (STATUS_OK,))}
    parcel_done = {
        Parcel(*row)
        for row in conn.execute(
            "SELECT lawd_cd, plat_gb, bonbun, bubun FROM bldg_parcel WHERE status IN (%s, %s)",
            (STATUS_OK, STATUS_NOT_FOUND),
        )
    }
    return recap_done, parcel_done


# ---------------------------------------------------------------- 저장


def store_recap(conn: psycopg.Connection, lawd_cd: str, items: Sequence[dict[str, str]]) -> None:
    """법정동 하나의 총괄표제부 응답으로 그 동의 원문을 바꾼다."""
    with conn.transaction():
        conn.execute("DELETE FROM bldg_recap WHERE lawd_cd = %s", (lawd_cd,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO bldg_recap (lawd_cd, mgm_pk, plat_gb, bonbun, bubun, item) VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (lawd_cd, i["mgmbldrgstpk"], i.get("platgbcd", ""), i.get("bun", ""), i.get("ji", ""), Jsonb(i))
                    for i in items
                ],
            )
        _write_recap_log(conn, lawd_cd, STATUS_OK, len(items), None)


def record_recap_error(conn: psycopg.Connection, lawd_cd: str, msg: str) -> None:
    """실패 기록. 전에 받은 원문은 그대로 둔다."""
    _write_recap_log(conn, lawd_cd, STATUS_ERROR, None, msg)


def _write_recap_log(conn, lawd_cd, status, row_count, error_msg) -> None:
    conn.execute(
        """
        INSERT INTO bldg_recap_log (lawd_cd, fetched_at, row_count, status, error_msg)
        VALUES (%s, now(), %s, %s, %s)
        ON CONFLICT (lawd_cd) DO UPDATE SET
          fetched_at = EXCLUDED.fetched_at, row_count = EXCLUDED.row_count,
          status = EXCLUDED.status, error_msg = EXCLUDED.error_msg
        """,
        (lawd_cd, row_count, status, error_msg),
    )


def load_recaps(conn: psycopg.Connection, lawd_cd: str) -> dict[Parcel, list[dict[str, str]]]:
    """법정동의 총괄표제부를 필지별로."""
    by_parcel: dict[Parcel, list[dict[str, str]]] = defaultdict(list)
    for plat_gb, bonbun, bubun, item in conn.execute(
        "SELECT plat_gb, bonbun, bubun, item FROM bldg_recap WHERE lawd_cd = %s ORDER BY mgm_pk", (lawd_cd,)
    ):
        by_parcel[Parcel(lawd_cd, plat_gb, bonbun, bubun)].append(item)
    return by_parcel


_UPSERT_PARCEL = """
    INSERT INTO bldg_parcel (lawd_cd, plat_gb, bonbun, bubun, status, recap_mgm_pk, households, buildings,
                             floors_max, far, bcr, parking, use_apr_date, fetched_at, error_msg)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {fetched_at}, NULL)
    ON CONFLICT (lawd_cd, plat_gb, bonbun, bubun) DO UPDATE SET
      status = EXCLUDED.status, recap_mgm_pk = EXCLUDED.recap_mgm_pk, households = EXCLUDED.households,
      buildings = EXCLUDED.buildings, floors_max = EXCLUDED.floors_max, far = EXCLUDED.far, bcr = EXCLUDED.bcr,
      parking = EXCLUDED.parking, use_apr_date = EXCLUDED.use_apr_date,
      fetched_at = {fetched_at_update}, error_msg = NULL
"""


def _parcel_result(recaps: Sequence[dict], titles: Sequence[dict]) -> tuple[str, BldgValues]:
    return STATUS_OK if recaps or titles else STATUS_NOT_FOUND, derive(recaps, titles)


def store_parcel(
    conn: psycopg.Connection, p: Parcel, recaps: Sequence[dict[str, str]], titles: Sequence[dict[str, str]]
) -> tuple[str, BldgValues]:
    """필지 하나의 표제부 응답으로 원문을 바꾸고 파생 값을 적는다. (상태, 파생 값)을 돌려준다."""
    status, values = _parcel_result(recaps, titles)  # 형식 오류면 여기서 ValueError. 아무것도 쓰지 않는다
    with conn.transaction():
        conn.execute("DELETE FROM bldg_title WHERE (lawd_cd, plat_gb, bonbun, bubun) = (%s, %s, %s, %s)", astuple(p))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO bldg_title (lawd_cd, plat_gb, bonbun, bubun, mgm_pk, item) VALUES (%s, %s, %s, %s, %s, %s)",
                [(*astuple(p), t["mgmbldrgstpk"], Jsonb(t)) for t in titles],
            )
        conn.execute(
            _UPSERT_PARCEL.format(fetched_at="now()", fetched_at_update="EXCLUDED.fetched_at"),
            (*astuple(p), status, *astuple(values)),
        )
    return status, values


def record_parcel_error(conn: psycopg.Connection, p: Parcel, msg: str) -> None:
    """실패 기록. 전에 받은 원문과 파생 값은 그대로 둔다."""
    conn.execute(
        """
        INSERT INTO bldg_parcel (lawd_cd, plat_gb, bonbun, bubun, status, error_msg) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (lawd_cd, plat_gb, bonbun, bubun) DO UPDATE SET
          status = EXCLUDED.status, error_msg = EXCLUDED.error_msg, fetched_at = now()
        """,
        (*astuple(p), STATUS_ERROR, msg),
    )


def rederive(conn: psycopg.Connection, lawd_cds: Iterable[str] | None = None) -> int:
    """저장된 원문에서 파생 값을 다시 계산한다. None이면 받은 모든 법정동. 'error' 필지는 건너뛴다.
    값이 바뀐 필지 수를 돌려준다."""
    if lawd_cds is None:
        lawd_cds = [cd for (cd,) in conn.execute("SELECT DISTINCT lawd_cd FROM bldg_parcel ORDER BY 1")]
    changed = 0
    for lawd_cd in lawd_cds:
        recaps = load_recaps(conn, lawd_cd)
        titles: dict[Parcel, list[dict]] = defaultdict(list)
        for *key, item in conn.execute(
            "SELECT lawd_cd, plat_gb, bonbun, bubun, item FROM bldg_title WHERE lawd_cd = %s ORDER BY mgm_pk", (lawd_cd,)
        ):
            titles[Parcel(*key)].append(item)
        parcels = [
            Parcel(*row)
            for row in conn.execute(
                "SELECT lawd_cd, plat_gb, bonbun, bubun FROM bldg_parcel WHERE lawd_cd = %s AND status IN (%s, %s)",
                (lawd_cd, STATUS_OK, STATUS_NOT_FOUND),
            )
        ]
        rows = []
        for p in parcels:
            status, values = _parcel_result(recaps.get(p, []), titles[p])
            rows.append((*astuple(p), status, *astuple(values)))
        if not rows:
            continue
        with conn.transaction():
            for row in rows:  # 조회 시각은 그대로 둔다
                cur = conn.execute(
                    _UPSERT_PARCEL.format(fetched_at="now()", fetched_at_update="bldg_parcel.fetched_at")
                    + """ WHERE (bldg_parcel.status, bldg_parcel.recap_mgm_pk, bldg_parcel.households,
                                 bldg_parcel.buildings, bldg_parcel.floors_max, bldg_parcel.far, bldg_parcel.bcr,
                                 bldg_parcel.parking, bldg_parcel.use_apr_date)
                          IS DISTINCT FROM (EXCLUDED.status, EXCLUDED.recap_mgm_pk, EXCLUDED.households,
                                 EXCLUDED.buildings, EXCLUDED.floors_max, EXCLUDED.far, EXCLUDED.bcr,
                                 EXCLUDED.parking, EXCLUDED.use_apr_date)""",
                    row,
                )
                changed += cur.rowcount
    return changed


# ---------------------------------------------------------------- 단지 반영

_APPLY = """
    WITH v AS (
      SELECT d.danji_id, p.households, p.buildings, p.floors_max, p.far, p.bcr, p.parking,
             CASE WHEN num_nonnulls(p.households, p.buildings, p.floors_max, p.far, p.bcr, p.parking) > 0
                  THEN count(*) OVER (PARTITION BY d.lawd_cd, plat_gb_of(d.jibun), d.bonbun, d.bubun) END AS cnt
      FROM danji d
      LEFT JOIN bldg_parcel p
        ON (p.lawd_cd, p.plat_gb, p.bonbun, p.bubun) = (d.lawd_cd, plat_gb_of(d.jibun), d.bonbun, d.bubun)
    )
    UPDATE danji d
    SET households = v.households, buildings = v.buildings, floors_max = v.floors_max,
        far = v.far, bcr = v.bcr, parking = v.parking, bldg_danji_cnt = v.cnt
    FROM v
    WHERE d.danji_id = v.danji_id
      AND (d.households, d.buildings, d.floors_max, d.far, d.bcr, d.parking, d.bldg_danji_cnt)
          IS DISTINCT FROM (v.households, v.buildings, v.floors_max, v.far, v.bcr, v.parking, v.cnt)
"""


def apply_to_danji(conn: psycopg.Connection) -> int:
    """bldg_parcel 값을 모든 단지에 반영하고 바뀐 단지 수를 돌려준다. 단지 위치가 바뀌었거나
    키가 없는 단지는 NULL이 된다. 한 필지의 단지들은 같은 값을 받고, bldg_danji_cnt가 단지 수다."""
    with conn.transaction():
        return conn.execute(_APPLY).rowcount


# ---------------------------------------------------------------- 실행


@dataclass
class RunSummary:
    ok: int = 0
    failures: list[tuple[Task, str]] = field(default_factory=list)
    stopped: str | None = None
    skipped: int = 0  # 멈췄거나 총괄표제부를 못 받아 호출하지 않은 작업
    rederived: int = 0


def run_tasks(conn: psycopg.Connection, client: HubClient, tasks: Sequence[Task], recap_done: set[str]) -> RunSummary:
    summary = RunSummary()
    consecutive = 0
    recap_ready = set(recap_done)
    recaps: dict[Parcel, list[dict]] = {}
    recaps_of = None  # recaps를 읽어 둔 법정동
    width = len(str(len(tasks)))
    for i, task in enumerate(tasks, start=1):
        if summary.stopped or (task.kind == "title" and task.lawd_cd not in recap_ready):
            summary.skipped += 1
            continue
        label = f"[{i:>{width}}/{len(tasks)}] {task}"
        try:
            if task.kind == "recap":
                items = client.recap(task.lawd_cd)
                store_recap(conn, task.lawd_cd, items)
                recap_ready.add(task.lawd_cd)
                recaps_of = None
                # 총괄표제부가 바뀌었으니 이미 받아 둔 이 동 필지의 파생 값도 다시 계산한다(처음 받는 동이면 0)
                summary.rederived += rederive(conn, [task.lawd_cd])
                msg = f"{len(items)}건"
            else:
                if recaps_of != task.lawd_cd:
                    recaps, recaps_of = load_recaps(conn, task.lawd_cd), task.lawd_cd
                p = task.parcel
                items = client.title(p)
                status, values = store_parcel(conn, p, recaps.get(p, []), items)
                msg = f"{len(items)}건 {status} 세대 {values.households}"
        except Exception as e:  # 에러 격리: 기록하고 다음으로
            msg = client.redact(f"{type(e).__name__}: {e}")
            if task.kind == "recap":
                record_recap_error(conn, task.lawd_cd, msg)
            else:
                record_parcel_error(conn, task.parcel, msg)
            summary.failures.append((task, msg))
            print(f"{label}: FAILED {msg}", file=sys.stderr)
            consecutive += 1
            if isinstance(e, QuotaExceeded):
                summary.stopped = "일일 한도 초과"
            elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                summary.stopped = f"연속 {MAX_CONSECUTIVE_FAILURES}회 실패"
            if summary.stopped:
                print(f"{summary.stopped}. 남은 작업은 호출하지 않는다", file=sys.stderr)
            continue
        consecutive = 0
        summary.ok += 1
        print(f"{label}: {msg}")
    return summary


def print_report(conn: psycopg.Connection, examples: int = 10) -> None:
    total, filled, shared = conn.execute(
        "SELECT count(*), count(households), count(*) FILTER (WHERE bldg_danji_cnt > 1) FROM danji"
    ).fetchone()
    print(f"단지 {total}: 세대수 있음 {filled}, 없음 {total - filled} ({(total - filled) / total:.1%})"
          f" | 필지 합계를 쓰는 단지 {shared}")
    for reason, n in conn.execute("SELECT reason, count(*) FROM v_bldg_missing GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {reason:14s} {n}")
    print("필지:", ", ".join(f"{s} {n}" for s, n in conn.execute(
        "SELECT status, count(*) FROM bldg_parcel GROUP BY 1 ORDER BY 2 DESC")))
    row = conn.execute(
        """SELECT count(*), count(far), count(bcr), count(parking), count(floors_max), count(buildings)
           FROM danji WHERE households IS NOT NULL"""
    ).fetchone()
    if row[0]:
        names = ["용적률", "건폐율", "주차", "최고층", "동수"]
        print(f"세대수 있는 단지 {row[0]} 중:", ", ".join(f"{n} {v / row[0]:.1%}" for n, v in zip(names, row[1:])))

    # 사용승인연도와 실거래 건축년도가 2년 이상 다르면 재건축 전 대장 등 엉뚱한 건물일 수 있다
    mismatch = conn.execute(
        """SELECT d.apt_seq, d.name, d.built_year, p.use_apr_date, d.lawd_cd || ' ' || d.jibun
           FROM danji d JOIN bldg_parcel p
             ON (p.lawd_cd, p.plat_gb, p.bonbun, p.bubun) = (d.lawd_cd, plat_gb_of(d.jibun), d.bonbun, d.bubun)
           WHERE d.households IS NOT NULL AND abs(extract(year FROM p.use_apr_date) - d.built_year) >= 2
           ORDER BY abs(extract(year FROM p.use_apr_date) - d.built_year) DESC, d.apt_seq"""
    ).fetchall()
    print(f"사용승인연도와 건축년도가 2년 이상 다른 단지: {len(mismatch)}")
    for row in mismatch[:examples]:
        print("  ", " | ".join(map(str, row)))
    rows = conn.execute(
        "SELECT reason, apt_seq, name, jibun, coalesce(error_msg, '') FROM v_bldg_missing"
        " WHERE reason <> 'not_fetched' ORDER BY reason, apt_seq LIMIT %s",
        (examples * 3,),
    ).fetchall()
    if rows:
        print("부가정보 없는 단지 예 (전체는 SELECT * FROM v_bldg_missing):")
        for row in rows:
            print("  ", " | ".join(map(str, row)))


def parse_region(value: str) -> str:
    if not re.fullmatch(r"\d{5}", value):
        raise argparse.ArgumentTypeError(f"시군구 코드 5자리가 아님: {value!r}")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="건축물대장(건축HUB) 부가정보를 받아 단지에 반영한다.")
    parser.add_argument("--region", type=parse_region, nargs="+", help="시군구 코드 5자리. 생략하면 모든 단지")
    parser.add_argument("--force", action="store_true", help="받은 적 있는 법정동·필지도 다시 받는다")
    parser.add_argument("--rps", type=float, default=5, help="초당 요청 수 상한 (기본 5)")
    parser.add_argument("--dry-run", action="store_true", help="호출할 수만 보여 주고 API는 호출하지 않는다")
    parser.add_argument("--rederive", action="store_true", help="API 없이 저장된 원문에서 파생 값을 다시 계산하고 반영한다")
    parser.add_argument("--report", action="store_true", help="부가정보가 없는 단지 리포트만 출력한다")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)
    with connect(autocommit=True) as conn:
        if args.report:
            print_report(conn)
            return
        if args.rederive:
            changed = rederive(conn)
            print(f"파생 값 다시 계산: 필지 {changed}개 바뀜 | 단지 반영: {apply_to_danji(conn)}개 바뀜\n")
            print_report(conn)
            return

        parcels = plan_parcels(conn, args.region)
        recap_done, parcel_done = done_sets(conn)
        tasks = plan_tasks(parcels, recap_done, parcel_done, args.force)
        n_recap = sum(t.kind == "recap" for t in tasks)
        print(
            f"조회 키 필지 {len(parcels)} (법정동 {len({p.lawd_cd for p in parcels})})"
            f" | 받을 것: 총괄표제부 법정동 {n_recap}, 표제부 필지 {len(tasks) - n_recap}"
            f" (호출 약 {len(tasks)}회 + 추가 페이지)"
        )
        if args.dry_run:
            return

        started = time.monotonic()
        summary = RunSummary()
        if tasks:
            with HubClient(os.environ.get("SERVICE_KEY", ""), rps=args.rps) as client:
                try:
                    summary = run_tasks(conn, client, tasks, recap_done)
                except KeyboardInterrupt:
                    print("\n중단됨. 받은 것은 저장됐다. 같은 명령을 다시 실행하면 이어서 받는다", file=sys.stderr)
                    sys.exit(130)
        changed = apply_to_danji(conn)

        elapsed = timedelta(seconds=round(time.monotonic() - started))
        print(
            f"\n끝 ({elapsed}): 성공 {summary.ok}, 실패 {len(summary.failures)}, 호출하지 않음 {summary.skipped}"
            f" | 총괄표제부로 다시 계산한 필지 {summary.rederived} | 단지 반영 {changed}개 바뀜\n"
        )
        print_report(conn)
    if summary.stopped:
        print(f"\n멈춤: {summary.stopped}", file=sys.stderr)
    if summary.failures:
        print(f"\n실패 {len(summary.failures)}건:", file=sys.stderr)
        for task, msg in summary.failures[:50]:
            print(f"  {task}: {msg}", file=sys.stderr)
    if summary.failures or summary.stopped:
        print("\n같은 명령을 다시 실행하면 받지 못한 것만 이어서 받는다", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
