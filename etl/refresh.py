"""파생 지표 머티리얼라이즈드 뷰(mv_*)를 한 트랜잭션에서 모두 다시 계산한다.

    python -m etl.refresh

- 계산 로직은 일반 뷰(v_*)에 있고 mv_*는 그 스냅숏이다(db/migrations/0005_phase2_views.sql)
- 한 트랜잭션이라 모든 mv_*가 같은 시점·같은 기준일(as_of_date(), 한국 시간 오늘)이 된다.
  갱신하는 동안 mv_*를 읽는 쿼리는 기다린다
- 끝나면 mv_refresh_log에 기준일과 소요시간을 남긴다. MCP 응답의 기준일은 여기서 온다
- 수집(etl.collect) 뒤에 실행한다. 단지 예외 목록(danji_flag)을 바꾼 뒤에도 실행해야 반영된다
"""

import sys
import time

import psycopg
from psycopg import sql

from etl.db import connect

# 서로 의존하지 않는다(각자 v_*에서 계산). 순서는 출력용
MATERIALIZED_VIEWS = (
    "mv_danji_price_monthly",
    "mv_danji_latest",
    "mv_region_monthly",
    "mv_danji_percentile",
    "mv_danji_recovery",
)


def refresh_all(conn: psycopg.Connection, log=print) -> dict[str, int]:
    """모든 mv_*를 다시 계산하고 뷰별 행 수를 돌려준다."""
    counts = {}
    started = time.monotonic()
    with conn.transaction():
        for name in MATERIALIZED_VIEWS:
            t = time.monotonic()
            conn.execute(sql.SQL("REFRESH MATERIALIZED VIEW {}").format(sql.Identifier(name)))
            counts[name] = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
            log(f"{name}: {counts[name]:,}행 ({time.monotonic() - t:.1f}초)")
        conn.execute(
            "INSERT INTO mv_refresh_log (as_of, duration_ms) VALUES (as_of_date(), %s)",
            (round((time.monotonic() - started) * 1000),),
        )
    return counts


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    with connect(autocommit=True) as conn:
        as_of = conn.execute("SELECT as_of_date()").fetchone()[0]
        print(f"기준일 {as_of} (한국 시간)")
        started = time.monotonic()
        refresh_all(conn)
    print(f"끝 ({time.monotonic() - started:.0f}초)")


if __name__ == "__main__":
    main()
