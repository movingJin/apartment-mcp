import subprocess
from datetime import date, datetime, timezone

from etl.collect import KST
from etl.cron import next_run, run_once, steps_for


def kst(*args) -> datetime:
    return datetime(*args, tzinfo=KST)


def test_next_run_today_before_0010():
    assert next_run(kst(2026, 10, 28, 0, 5)) == kst(2026, 10, 28, 0, 10)


def test_next_run_tomorrow_after_0010():
    assert next_run(kst(2026, 10, 28, 0, 10)) == kst(2026, 10, 29, 0, 10)
    assert next_run(kst(2026, 10, 28, 8, 30)) == kst(2026, 10, 29, 0, 10)


def test_next_run_is_korean_time():
    # 00:10 KST는 UTC로 전날 15:10이다. UTC 날짜로 계산하면 하루 어긋난다
    assert next_run(datetime(2026, 10, 31, 15, 9, tzinfo=timezone.utc)) == kst(2026, 11, 1, 0, 10)
    assert next_run(datetime(2026, 10, 31, 15, 11, tzinfo=timezone.utc)) == kst(2026, 11, 2, 0, 10)


def test_sunday_run_takes_12_months_of_sales():
    sunday = next_run(datetime(2026, 10, 31, 15, 9, tzinfo=timezone.utc)).date()  # UTC로는 토요일
    assert sunday.weekday() == 6
    sale, rent, *_ = steps_for(sunday)
    assert sale.args == ("etl.collect", "--mode", "incremental", "--kind", "sale", "--recent", "12")
    assert rent.args == ("etl.collect", "--mode", "incremental", "--kind", "rent")


def test_weekday_steps():
    steps = steps_for(date(2026, 10, 28))  # 수요일
    assert [s.args for s in steps] == [
        ("etl.collect", "--mode", "incremental", "--kind", "sale", "--recent", "3"),
        ("etl.collect", "--mode", "incremental", "--kind", "rent"),
        ("etl.bldg",),
        ("etl.refresh",),
    ]


def test_failed_step_does_not_stop_refresh():
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd[1:])
        if "sale" in cmd:
            return subprocess.CompletedProcess(cmd, 1)
        if cmd[-1] == "etl.bldg":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0)

    failed = run_once(date(2026, 10, 28), run=fake_run, log=lambda *_: None)
    assert [c[1] for c in calls] == ["etl.collect", "etl.collect", "etl.bldg", "etl.refresh"]
    assert failed == ["매매 최근 3개월", "건축물대장 새 필지"]


def test_all_steps_ok():
    ok = lambda cmd, timeout: subprocess.CompletedProcess(cmd, 0)  # noqa: E731
    assert run_once(date(2026, 10, 28), run=ok, log=lambda *_: None) == []
