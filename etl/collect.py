"""실거래가를 (종류, 시군구, 계약년월) 단위로 받아 DB와 동기화한다.

    python -m etl.collect --mode full --dry-run     # 받을 구간 수만 본다
    python -m etl.collect --mode full               # 수도권 83개 × 2021-01~현재월, 매매·전월세
    python -m etl.collect --mode incremental        # 재수집 창(최근 3개월)만. cron용
    python -m etl.collect --mode incremental --kind sale --recent 12   # 최근 12개월 매매. 주 1회 cron용
    python -m etl.collect --mode full --region 11410 --from 2025-06 --kind sale --force

받을 구간
- full: --from(기본 2021-01) ~ --to(기본 현재월). ingest_log에 'ok'인 구간은 건너뛴다(재개).
  재수집 창(현재월 포함 최근 3개월, --recent로 바꾼다)은 'ok'여도 다시 받는다. --force면 전부 다시 받는다
- incremental: 재수집 창만 받는다
- 해제는 계약월+2개월 안에 77%, +11개월 안에 99%가 붙는다. 매일 3개월 창에 더해 주 1회 12개월 매매를 다시 받아
  늦게 붙은 해제를 반영한다(SPEC 변경 이력 Phase 2 착수 전 결정 3)
- 현재월은 한국 시간 기준이다. DB 타임존이 UTC라 DB 날짜로 계산하면 자정~오전 9시에 어긋난다
- --region을 생략하면 etl.regions.REGIONS 전체

멈춤
- 한 구간의 실패는 ingest_log에 'error'로 남기고 다음 구간으로 넘어간다
- 일일 한도 초과 응답이 오거나 같은 종류가 연속 5구간 실패하면 그 종류의 남은 구간은 호출하지 않는다.
  한도는 API(종류)마다 따로라 다른 종류는 계속한다. 같은 명령을 다시 실행하면 'ok'가 아닌 구간만 받는다

한 (종류, 시군구, 월)이 트랜잭션 하나다.
- 응답 행마다 src_hash(저장하는 모든 값 + 같은 값인 행 사이의 순번)를 만든다(records.src_hashes)
- DB의 같은 구간에 없는 해시만 넣고, 응답에서 사라진 해시는 지운다. 같은 응답이면 아무것도 바뀌지 않는다
- 값이 바뀐 행(해제, 등기일 추가, 정정)은 옛 행을 지우고 새로 넣는다. 안 바뀐 행은 id가 유지된다
- 해제된 원 신고와 같은 조건의 재신고는 해제여부·등기일이 달라 둘 다 남는다
- 단지는 aptSeq로 upsert하고 거래 행에 danji_id를 붙인다. 단지 위치(시군구·법정동코드·지번)는
  새로 만들 때 그 구간 값으로 넣고, 실행이 끝날 때 refresh_danji_locations가 거래 행 최빈값으로 다시 정한다
- 성공하면 같은 트랜잭션에서 ingest_log에 'ok'를, 실패하면 롤백하고 'error'를 남긴다.
  한 구간의 실패가 나머지 구간을 멈추지 않는다
"""

import argparse
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import astuple, dataclass, field, fields
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg import sql

from etl.db import connect
from etl.records import RentRow, SaleRow, normalize_name, parse_rent, parse_sale, split_jibun, src_hashes
from etl.regions import REGIONS
from etl.rtms import QuotaExceeded, RtmsClient

STATUS_OK = "ok"
STATUS_ERROR = "error"

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋이면 된다
DEFAULT_FROM = "202101"
RECENT_MONTHS = 3  # 재수집 창: 현재월 포함. 지연 신고와 해제를 반영한다
MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True)
class Kind:
    table: str
    row_type: type
    parse: Callable[[dict[str, str], str, str], SaleRow | RentRow]


KINDS = {
    "sale": Kind("trade_sale", SaleRow, parse_sale),
    "rent": Kind("trade_rent", RentRow, parse_rent),
}


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    inserted: int
    deleted: int

    @property
    def unchanged(self) -> int:
        return self.fetched - self.inserted


class EmptyResponseError(Exception):
    """응답이 0건인데 DB 구간에는 행이 있다. 일시 오류일 수 있어 지우지 않는다."""


def month_range(deal_ymd: str) -> tuple[date, date]:
    start = date(int(deal_ymd[:4]), int(deal_ymd[4:]), 1)
    end = date(start.year + start.month // 12, start.month % 12 + 1, 1)
    return start, end


def load_slice(
    conn: psycopg.Connection, kind: str, lawd_cd5: str, deal_ymd: str, items: Sequence[dict[str, str]]
) -> SyncResult:
    """한 (종류, 시군구, 월)의 응답 전체를 DB와 맞춘다. items는 모든 페이지를 합친 것이어야 한다."""
    spec = KINDS[kind]
    rows = [spec.parse(item, lawd_cd5, deal_ymd) for item in items]
    hashes = src_hashes(rows)
    start, end = month_range(deal_ymd)
    table = sql.Identifier(spec.table)
    in_slice = sql.SQL("lawd_cd5 = %s AND deal_date >= %s AND deal_date < %s")
    slice_params = (lawd_cd5, start, end)

    with conn.transaction():
        existing = {
            h for (h,) in conn.execute(sql.SQL("SELECT src_hash FROM {} WHERE {}").format(table, in_slice), slice_params)
        }
        if not rows and existing:
            raise EmptyResponseError(f"응답 0건, DB {len(existing)}건. 지우지 않고 실패로 남긴다")

        stale = existing - set(hashes)
        if stale:
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE {} AND src_hash = ANY(%s)").format(table, in_slice),
                (*slice_params, list(stale)),
            )

        new = [(row, h) for row, h in zip(rows, hashes) if h not in existing]
        danji_ids = upsert_danji(conn, kind, lawd_cd5, [row for row, _ in new])
        columns = [f.name for f in fields(spec.row_type)] + ["danji_id", "src_hash"]
        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(table, sql.SQL(", ").join(map(sql.Identifier, columns)))
        with conn.cursor().copy(copy_sql) as copy:
            for row, h in new:
                copy.write_row((*astuple(row), danji_ids.get(row.apt_seq), h))

        _write_log(conn, kind, lawd_cd5, deal_ymd, STATUS_OK, len(rows), None)

    return SyncResult(fetched=len(rows), inserted=len(new), deleted=len(stale))


@dataclass(frozen=True)
class DanjiRow:
    apt_seq: str
    lawd_cd5: str
    lawd_cd: str | None
    bonbun: str | None
    bubun: str | None
    jibun: str
    name: str
    name_norm: str
    built_year: int | None


# 위치(lawd_cd5, lawd_cd, bonbun, bubun, jibun)는 새로 만들 때만 넣는다. 원본에는 시군구 코드가 잘못 붙은
# 행이 섞여 있어서(관악구 단지 거래 1건이 동작구 코드로 오는 식) 덮어쓰면 적재 순서에 따라 위치가 틀린다.
# 기존 단지의 위치는 refresh_danji_locations가 정한다. 단지명은 마지막으로 적재한 매매 값을 따른다.
_UPSERT_DANJI = {
    "sale": """
        INSERT INTO danji AS d (apt_seq, lawd_cd5, lawd_cd, bonbun, bubun, jibun, name, name_norm, built_year)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (apt_seq) DO UPDATE SET
          name       = EXCLUDED.name,
          name_norm  = EXCLUDED.name_norm,
          built_year = COALESCE(EXCLUDED.built_year, d.built_year)
        WHERE (d.name, d.name_norm, d.built_year)
              IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.name_norm, COALESCE(EXCLUDED.built_year, d.built_year))
    """,
    "rent": """
        INSERT INTO danji AS d (apt_seq, lawd_cd5, lawd_cd, bonbun, bubun, jibun, name, name_norm, built_year)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (apt_seq) DO UPDATE SET
          built_year = EXCLUDED.built_year
        WHERE d.built_year IS NULL AND EXCLUDED.built_year IS NOT NULL
    """,
}


def upsert_danji(
    conn: psycopg.Connection, kind: str, lawd_cd5: str, rows: Sequence[SaleRow | RentRow]
) -> dict[str, int]:
    """rows에 나온 aptSeq마다 단지를 만들거나 갱신하고 {aptSeq: danji_id}를 돌려준다."""
    if kind == "sale":
        candidates = [
            DanjiRow(
                r.apt_seq, lawd_cd5, r.lawd_cd, r.bonbun, r.bubun,
                r.jibun or "", r.apt_name or "", normalize_name(r.apt_name or ""), r.built_year,
            )
            for r in rows
            if r.apt_seq
        ]
    else:
        lawd_cds = lookup_lawd_cd(conn, lawd_cd5, {r.umd_nm for r in rows if r.umd_nm})
        candidates = [
            DanjiRow(
                r.apt_seq, lawd_cd5, lawd_cds.get(r.umd_nm), *split_jibun(r.jibun),
                r.jibun or "", r.apt_name or "", normalize_name(r.apt_name or ""), r.built_year,
            )
            for r in rows
            if r.apt_seq
        ]
    if not candidates:
        return {}

    # 같은 aptSeq인데 지번·단지명이 다른 행이 섞여 있으면 가장 많은 쪽을 쓴다(원본 값은 거래 행에 남는다)
    by_seq: dict[str, Counter[DanjiRow]] = defaultdict(Counter)
    for d in candidates:
        by_seq[d.apt_seq][d] += 1
    # aptSeq 순서로 잠근다. 매매·전월세를 두 프로세스로 동시에 돌려도 서로 교착하지 않는다
    danji = [by_seq[seq].most_common(1)[0][0] for seq in sorted(by_seq)]

    with conn.cursor() as cur:
        cur.executemany(_UPSERT_DANJI[kind], [astuple(d) for d in danji])
    return dict(conn.execute("SELECT apt_seq, danji_id FROM danji WHERE apt_seq = ANY(%s)", (list(by_seq),)))


# 매매 행에서 가장 많이 나온 위치. 같으면 최근 계약이 있는 쪽, 그래도 같으면 값 순서로 정한다
_SALE_LOCATION_MODE = """
    SELECT DISTINCT ON (danji_id) danji_id, lawd_cd5, lawd_cd, bonbun, bubun, jibun
    FROM (SELECT danji_id, lawd_cd5, lawd_cd, bonbun, bubun, COALESCE(jibun, '') AS jibun,
                 count(*) AS n, max(deal_date) AS last_deal
          FROM trade_sale WHERE danji_id IS NOT NULL AND ({filter})
          GROUP BY 1, 2, 3, 4, 5, 6) s
    ORDER BY danji_id, n DESC, last_deal DESC, lawd_cd5, lawd_cd, bonbun, bubun, jibun
"""

_RENT_LOCATION_MODE = """
    SELECT DISTINCT ON (danji_id) danji_id, lawd_cd5, umd_nm, jibun
    FROM (SELECT danji_id, lawd_cd5, umd_nm, jibun, count(*) AS n, max(deal_date) AS last_deal
          FROM trade_rent WHERE danji_id = ANY(%s)
          GROUP BY 1, 2, 3, 4) r
    ORDER BY danji_id, n DESC, last_deal DESC, lawd_cd5, umd_nm, jibun
"""

_UPDATE_LOCATION = """
    UPDATE danji d
    SET lawd_cd5 = v.lawd_cd5, lawd_cd = v.lawd_cd, bonbun = v.bonbun, bubun = v.bubun, jibun = v.jibun
    FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::text[], %s::text[], %s::text[])
         AS v(danji_id, lawd_cd5, lawd_cd, bonbun, bubun, jibun)
    WHERE d.danji_id = v.danji_id
      AND (d.lawd_cd5, d.lawd_cd, d.bonbun, d.bubun, d.jibun)
          IS DISTINCT FROM (v.lawd_cd5::bpchar, v.lawd_cd::bpchar, v.bonbun::bpchar, v.bubun::bpchar, v.jibun)
    RETURNING d.apt_seq
"""


def refresh_danji_locations(conn: psycopg.Connection, danji_ids: Sequence[int] | None = None) -> list[str]:
    """단지 위치를 거래 행 최빈값으로 다시 정하고, 바뀐 단지의 aptSeq를 돌려준다. None이면 모든 단지.

    매매 행이 있으면 매매 행의 (시군구, 법정동코드, 본번, 부번, 지번) 최빈값을 쓴다. 매매가 없는 단지는
    전월세 행의 (시군구, 읍면동 이름, 지번) 최빈값에서 법정동코드와 본번·부번을 만든다(upsert_danji와 같은 규칙).
    적재 순서와 무관하게 같은 결과가 나온다.
    """
    ids = None if danji_ids is None else list(danji_ids)
    with conn.transaction():
        if ids is None:
            sale = conn.execute(_SALE_LOCATION_MODE.format(filter="TRUE")).fetchall()
            rent_ids = [i for (i,) in conn.execute(
                "SELECT danji_id FROM danji d WHERE NOT EXISTS (SELECT 1 FROM trade_sale s WHERE s.danji_id = d.danji_id)"
            )]
        else:
            sale = conn.execute(_SALE_LOCATION_MODE.format(filter="danji_id = ANY(%s)"), (ids,)).fetchall()
            rent_ids = sorted(set(ids) - {row[0] for row in sale})

        locations = [tuple(row) for row in sale]
        rent = conn.execute(_RENT_LOCATION_MODE, (rent_ids,)).fetchall() if rent_ids else []
        by_sgg: dict[str, set[str]] = defaultdict(set)
        for _, lawd_cd5, umd_nm, _ in rent:
            if umd_nm:
                by_sgg[lawd_cd5].add(umd_nm)
        lawd_cds = {sgg: lookup_lawd_cd(conn, sgg, names) for sgg, names in by_sgg.items()}
        for danji_id, lawd_cd5, umd_nm, jibun in rent:
            lawd_cd = lawd_cds.get(lawd_cd5, {}).get(umd_nm) if umd_nm else None
            locations.append((danji_id, lawd_cd5, lawd_cd, *split_jibun(jibun), jibun or ""))

        if not locations:
            return []
        columns = [list(col) for col in zip(*locations)]
        return [seq for (seq,) in conn.execute(_UPDATE_LOCATION, columns)]


def lookup_lawd_cd(conn: psycopg.Connection, lawd_cd5: str, umd_names: set[str]) -> dict[str, str]:
    """전월세 읍면동 이름 → 10자리 법정동코드. 리 단위는 '양평읍 양근리'로 와서 lawd.dong과 형식이 같다.

    활성 코드가 있으면 활성 코드 중에서, 없으면 폐지 코드 중에서 정확히 하나일 때만 쓴다.
    """
    if not umd_names:
        return {}
    found: dict[str, list[tuple[bool, str]]] = defaultdict(list)
    for dong, lawd_cd, is_active in conn.execute(
        "SELECT dong, lawd_cd, is_active FROM lawd WHERE lawd_cd5 = %s AND dong = ANY(%s)",
        (lawd_cd5, list(umd_names)),
    ):
        found[dong].append((is_active, lawd_cd))
    result = {}
    for dong, candidates in found.items():
        best = [cd for active, cd in candidates if active] or [cd for _, cd in candidates]
        if len(best) == 1:
            result[dong] = best[0]
    return result


def _write_log(
    conn: psycopg.Connection, kind: str, lawd_cd5: str, deal_ymd: str,
    status: str, row_count: int | None, error_msg: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO ingest_log (kind, lawd_cd5, deal_ymd, fetched_at, row_count, status, error_msg)
        VALUES (%s, %s, %s, now(), %s, %s, %s)
        ON CONFLICT (kind, lawd_cd5, deal_ymd) DO UPDATE SET
          fetched_at = EXCLUDED.fetched_at,
          row_count  = EXCLUDED.row_count,
          status     = EXCLUDED.status,
          error_msg  = EXCLUDED.error_msg
        """,
        (kind, lawd_cd5, deal_ymd, row_count, status, error_msg),
    )


def record_error(conn: psycopg.Connection, kind: str, lawd_cd5: str, deal_ymd: str, error_msg: str) -> None:
    """실패 기록. 이전에 성공한 데이터는 그대로 두고 로그만 'error'로 바꾼다."""
    with conn.transaction():
        _write_log(conn, kind, lawd_cd5, deal_ymd, STATUS_ERROR, None, error_msg)


def collect_slice(
    conn: psycopg.Connection, client: RtmsClient, kind: str, lawd_cd5: str, deal_ymd: str
) -> SyncResult:
    return load_slice(conn, kind, lawd_cd5, deal_ymd, client.fetch(kind, lawd_cd5, deal_ymd))


@dataclass(frozen=True)
class Slice:
    kind: str
    lawd_cd5: str
    deal_ymd: str

    def __str__(self) -> str:
        return f"{self.kind} {self.lawd_cd5} {self.deal_ymd}"


def ok_slices(conn: psycopg.Connection) -> set[Slice]:
    return {
        Slice(*row)
        for row in conn.execute("SELECT kind, lawd_cd5, deal_ymd FROM ingest_log WHERE status = %s", (STATUS_OK,))
    }


def plan_slices(
    kinds: Sequence[str], regions: Sequence[str], months: Sequence[str],
    done: set[Slice], recent: Iterable[str], force: bool,
) -> list[Slice]:
    """받을 구간. 'ok' 기록이 있는 구간은 재수집 창에 들거나 force일 때만 다시 받는다."""
    recent = set(recent)
    slices = [Slice(kind, region, ym) for region in regions for ym in months for kind in kinds]
    return [s for s in slices if force or s.deal_ymd in recent or s not in done]


@dataclass
class RunSummary:
    ok: int = 0
    failures: list[tuple[Slice, str]] = field(default_factory=list)
    stopped: dict[str, str] = field(default_factory=dict)  # 종류 → 멈춘 이유
    not_attempted: int = 0  # 멈춘 종류라 호출하지 않은 구간


def collect_all(conn: psycopg.Connection, client: RtmsClient, slices: Sequence[Slice]) -> RunSummary:
    summary = RunSummary()
    consecutive: Counter[str] = Counter()
    width = len(str(len(slices)))
    for i, s in enumerate(slices, start=1):
        if s.kind in summary.stopped:
            summary.not_attempted += 1
            continue
        label = f"[{i:>{width}}/{len(slices)}] {s}"
        try:
            r = collect_slice(conn, client, s.kind, s.lawd_cd5, s.deal_ymd)
        except Exception as e:  # 에러 격리: 기록하고 다음 구간으로
            msg = client.redact(f"{type(e).__name__}: {e}")
            record_error(conn, s.kind, s.lawd_cd5, s.deal_ymd, msg)
            summary.failures.append((s, msg))
            print(f"{label}: FAILED {msg}", file=sys.stderr)
            consecutive[s.kind] += 1
            if isinstance(e, QuotaExceeded):
                summary.stopped[s.kind] = "일일 한도 초과"
            elif consecutive[s.kind] >= MAX_CONSECUTIVE_FAILURES:
                summary.stopped[s.kind] = f"연속 {MAX_CONSECUTIVE_FAILURES}구간 실패"
            if s.kind in summary.stopped:
                print(f"{s.kind}: {summary.stopped[s.kind]}. 남은 {s.kind} 구간은 호출하지 않는다", file=sys.stderr)
            continue
        consecutive[s.kind] = 0
        summary.ok += 1
        print(f"{label}: fetched {r.fetched} | inserted {r.inserted}, deleted {r.deleted}, unchanged {r.unchanged}")
    return summary


def parse_ym(value: str) -> str:
    """'2025-06' 또는 '202506' → '202506'."""
    m = re.fullmatch(r"(\d{4})-?(\d{2})", value)
    if not m or not 1 <= int(m[2]) <= 12:
        raise argparse.ArgumentTypeError(f"YYYY-MM 형식이 아님: {value!r}")
    return m[1] + m[2]


def parse_region(value: str) -> str:
    if not re.fullmatch(r"\d{5}", value):
        raise argparse.ArgumentTypeError(f"시군구 코드 5자리가 아님: {value!r}")
    return value


def months_between(ym_from: str, ym_to: str) -> list[str]:
    months = []
    y, m = int(ym_from[:4]), int(ym_from[4:])
    while f"{y:04d}{m:02d}" <= ym_to:
        months.append(f"{y:04d}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def add_months(ym: str, n: int) -> str:
    i = int(ym[:4]) * 12 + int(ym[4:]) - 1 + n
    return f"{i // 12:04d}{i % 12 + 1:02d}"


def current_ym(now: datetime | None = None) -> str:
    """한국 시간 기준 현재 계약년월."""
    return (now or datetime.now(timezone.utc)).astimezone(KST).strftime("%Y%m")


def recent_months(cur_ym: str, n: int = RECENT_MONTHS) -> list[str]:
    """재수집 창: 현재월을 포함한 최근 n개월."""
    return months_between(add_months(cur_ym, 1 - n), cur_ym)


def parse_recent(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"1 이상이어야 함: {value!r}")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="국토부 아파트 실거래가를 수집해 DB와 동기화한다.")
    parser.add_argument("--mode", choices=["full", "incremental"], required=True,
                        help="full: 기간 전체, 'ok' 구간은 건너뜀(재개). incremental: 재수집 창만")
    parser.add_argument("--region", type=parse_region, nargs="+",
                        help="시군구 코드 5자리. 생략하면 수도권 83개(etl/regions.py)")
    parser.add_argument("--from", dest="ym_from", type=parse_ym, help="full 전용. 계약년월 YYYY-MM (기본 2021-01)")
    parser.add_argument("--to", dest="ym_to", type=parse_ym, help="full 전용. 계약년월 YYYY-MM (기본 현재월)")
    parser.add_argument("--kind", nargs="+", choices=sorted(KINDS), default=["sale", "rent"])
    parser.add_argument("--force", action="store_true", help="full 전용. 'ok' 기록이 있어도 다시 받는다")
    parser.add_argument("--recent", type=parse_recent, default=RECENT_MONTHS,
                        help=f"재수집 창 개월 수. 현재월 포함 (기본 {RECENT_MONTHS})")
    parser.add_argument("--rps", type=float, default=5, help="초당 요청 수 상한 (기본 5)")
    parser.add_argument("--dry-run", action="store_true", help="받을 구간 수만 보여 주고 API는 호출하지 않는다")
    args = parser.parse_args()

    cur = current_ym()
    recent = recent_months(cur, args.recent)
    if args.mode == "incremental":
        if args.ym_from or args.ym_to or args.force:
            parser.error("--from, --to, --force는 full 모드에서만 쓴다")
        months = recent
    else:
        ym_to = args.ym_to or cur
        if ym_to > cur:
            parser.error(f"--to가 현재월({cur}, 한국 시간)보다 뒤")
        months = months_between(args.ym_from or DEFAULT_FROM, ym_to)
        if not months:
            parser.error("--to가 --from보다 앞섬")
    regions = args.region or list(REGIONS)

    sys.stdout.reconfigure(line_buffering=True)  # 파일로 돌려도 줄마다 바로 보이게
    with connect(autocommit=True) as conn:
        slices = plan_slices(args.kind, regions, months, ok_slices(conn), recent, args.force)
        total = len(regions) * len(months) * len(args.kind)
        per_kind = Counter(s.kind for s in slices)
        print(
            f"{args.mode}: 시군구 {len(regions)} × {len(months)}개월({months[0]}~{months[-1]}) × {len(args.kind)}종류"
            f" = {total}구간 | 재수집 창 {recent[0]}~{recent[-1]} (현재월 {cur}, 한국 시간)\n"
            f"'ok'라 건너뜀 {total - len(slices)} | 받을 구간 {len(slices)} "
            f"({', '.join(f'{k} {per_kind[k]}' for k in args.kind)})"
        )
        if args.dry_run or not slices:
            return

        for year in sorted({int(s.deal_ymd[:4]) for s in slices}):
            conn.execute("SELECT ensure_trade_partitions(%s)", (year,))
        started = time.monotonic()
        with RtmsClient(os.environ.get("SERVICE_KEY", ""), rps=args.rps) as client:
            try:
                summary = collect_all(conn, client, slices)
            except KeyboardInterrupt:
                print("\n중단됨. 진행 중이던 구간은 롤백됐다. 같은 명령을 다시 실행하면 이어서 받는다", file=sys.stderr)
                sys.exit(130)

        if summary.ok:  # 중간에 끊긴 실행은 여기까지 오지 않지만 다음 실행이 모든 단지를 다시 계산한다
            moved = refresh_danji_locations(conn)
            print(f"\n단지 위치 다시 계산: {len(moved)}개 바뀜 {' '.join(moved[:20])}")

    elapsed = timedelta(seconds=round(time.monotonic() - started))
    print(
        f"\n끝 ({elapsed}): 성공 {summary.ok}, 실패 {len(summary.failures)}, "
        f"멈춰서 호출하지 않음 {summary.not_attempted}"
    )
    for kind, reason in summary.stopped.items():
        print(f"멈춘 종류: {kind} ({reason})", file=sys.stderr)
    if summary.failures:
        print(f"\n실패 {len(summary.failures)}건:", file=sys.stderr)
        for s, msg in summary.failures:
            print(f"  {s}: {msg}", file=sys.stderr)
    if summary.failures or summary.stopped:
        print("\n같은 명령을 다시 실행하면 'ok'가 아닌 구간만 이어서 받는다", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
