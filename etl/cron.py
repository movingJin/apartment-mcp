"""매일 00:10(한국 시간)에 증분 수집 → 건축물대장 → 파생 지표 갱신을 차례로 돌린다. NAS의 etl 컨테이너가 띄운다.

    python -m etl.cron           # 스케줄러. 다음 실행 시각까지 기다렸다가 돌고, 끝나면 다음 날을 기다린다
    python -m etl.cron --once    # 지금 한 번만 돌린다(수동 실행)
    python -m etl.cron --plan    # 다음 실행 시각과 단계만 보여 준다(API·DB 접속 없음)

한 번의 실행은 아래 단계다. 단계마다 기존 CLI를 별도 프로세스(python -m …)로 띄운다
1. 매매 재수집 창. 일요일(한국 시간)은 최근 12개월, 다른 날은 3개월. 해제가 늦게 붙는 것을 반영한다(SPEC 수집 요구사항 3)
2. 전월세 최근 3개월
3. 건축물대장: 받지 않은 필지(수집으로 새로 생긴 단지)만 받는다
4. etl.refresh: mv_* 재계산. 기준일과 data_completeness가 날마다 바뀌므로 매일 돈다

- 단계가 실패해도(종료 코드 ≠ 0, 시간 초과) 다음 단계로 넘어간다. 성공한 구간은 이미 커밋됐고,
  실패한 구간·필지는 다음 날 실행이 이어서 받는다
- 실행 시각을 놓치면(컨테이너가 꺼져 있었으면) 따라잡지 않고 다음 날 돈다. 바로 받으려면 --once
- 결과는 DB에 남는다: ingest_log(구간), bldg_parcel(필지), mv_refresh_log(갱신 기준일)
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from etl.collect import KST, RECENT_MONTHS
from etl.db import connect

RUN_HOUR, RUN_MINUTE = 0, 10  # 한국 시간
WEEKLY_SALE_MONTHS = 12
SUNDAY = 6  # date.weekday()
STEP_TIMEOUT = timedelta(hours=3)  # 멈춘 단계가 다음 날 실행까지 막지 않게 한다. 평소 단계는 수 분
SLEEP_CHUNK = 600  # 초. 긴 대기를 나눠 벽시계를 다시 본다(시계 보정·NAS 절전)
REQUIRED_ENV = ("DATABASE_URL", "SERVICE_KEY")


@dataclass(frozen=True)
class Step:
    name: str
    args: tuple[str, ...]  # python -m 뒤


def steps_for(day: date) -> list[Step]:
    """그날(한국 시간) 실행할 단계."""
    sale_months = WEEKLY_SALE_MONTHS if day.weekday() == SUNDAY else RECENT_MONTHS
    return [
        Step(f"매매 최근 {sale_months}개월",
             ("etl.collect", "--mode", "incremental", "--kind", "sale", "--recent", str(sale_months))),
        Step(f"전월세 최근 {RECENT_MONTHS}개월", ("etl.collect", "--mode", "incremental", "--kind", "rent")),
        Step("건축물대장 새 필지", ("etl.bldg",)),
        Step("파생 지표 갱신", ("etl.refresh",)),
    ]


def next_run(now: datetime) -> datetime:
    """now 뒤 첫 실행 시각(한국 시간). 그 시각이 지났으면 다음 날."""
    local = now.astimezone(KST)
    at = local.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    return at if at > local else at + timedelta(days=1)


def stamp() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def run_once(day: date, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
             log=print) -> list[str]:
    """단계를 차례로 실행하고 실패한 단계 이름을 돌려준다."""
    failed = []
    started = time.monotonic()
    log(f"\n##### {stamp()} 실행 시작 ({day} {'월화수목금토일'[day.weekday()]}요일)")
    for step in steps_for(day):
        log(f"\n=== {stamp()} {step.name}: python -m {' '.join(step.args)}")
        t = time.monotonic()
        try:
            code = run([sys.executable, "-m", *step.args], timeout=STEP_TIMEOUT.total_seconds()).returncode
            result = f"종료 코드 {code}"
        except subprocess.TimeoutExpired:
            code, result = None, f"{STEP_TIMEOUT} 시간 초과로 중단"
        log(f"=== {step.name} 끝 ({timedelta(seconds=round(time.monotonic() - t))}): {result}")
        if code != 0:
            failed.append(step.name)
    elapsed = timedelta(seconds=round(time.monotonic() - started))
    log(f"\n##### {stamp()} 실행 끝 ({elapsed}): "
        + (f"실패한 단계 {', '.join(failed)}. 다음 실행이 이어서 받는다" if failed else "모두 성공"))
    return failed


def sleep_until(when: datetime) -> None:
    while (left := (when - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(left, SLEEP_CHUNK))


def check_ready() -> None:
    """환경변수와 DB 접속을 확인한다. 안 되면 여기서 멈춘다(컨테이너가 다시 띄운다)."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        sys.exit(f"환경변수 없음: {', '.join(missing)}")
    with connect() as conn:
        row = conn.execute("SELECT as_of, refreshed_at FROM mv_refresh_log ORDER BY refreshed_at DESC LIMIT 1").fetchone()
    print("DB 접속 확인. 마지막 갱신: "
          + (f"{row[0]} 기준 ({row[1].astimezone(KST):%Y-%m-%d %H:%M} 한국 시간)" if row else "없음"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m etl.cron",
                                     description="매일 00:10(한국 시간)에 증분 수집·건축물대장·파생 지표 갱신을 돌린다.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="기다리지 않고 지금 한 번만 돌린다")
    group.add_argument("--plan", action="store_true", help="다음 실행 시각과 단계만 보여 준다")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # 단계 프로세스 출력과 순서가 섞이지 않게

    if args.plan:
        at = next_run(datetime.now(timezone.utc))
        print(f"다음 실행: {at:%Y-%m-%d %H:%M} (한국 시간)")
        for step in steps_for(at.date()):
            print(f"  {step.name}: python -m {' '.join(step.args)}")
        return

    # 컨테이너의 PID 1은 핸들러가 없으면 SIGTERM을 무시한다. docker stop에 바로 끝내고, 돌던 단계는
    # subprocess.run이 죽인다(구간·갱신은 트랜잭션이라 롤백된다)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    check_ready()
    if args.once:
        sys.exit(1 if run_once(datetime.now(KST).date()) else 0)

    while True:
        at = next_run(datetime.now(timezone.utc))
        print(f"\n다음 실행: {at:%Y-%m-%d %H:%M} (한국 시간) — "
              + ", ".join(step.name for step in steps_for(at.date())))
        sleep_until(at)
        run_once(at.date())


if __name__ == "__main__":
    main()
