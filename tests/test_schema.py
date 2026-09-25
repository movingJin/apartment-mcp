"""마이그레이션이 적용된 DB에 대한 읽기 전용 검사. DATABASE_URL이 없으면 건너뛴다."""

import os

import pytest

from etl.db import connect

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")


@pytest.fixture(scope="module")
def conn():
    with connect() as c:
        yield c


@pytest.mark.parametrize(
    ("area", "group"),
    [
        ("39.997", "XS"),
        ("40", "XS"),
        ("40.0001", "S"),
        ("60", "S"),  # 서대문구 2025-06에만 전용 60.00 거래가 9건
        ("60.06", "M"),
        ("84.9953", "M"),
        ("85", "M"),
        ("85.0001", "L"),
        ("102", "L"),
        ("102.0001", "XL"),
    ],
)
def test_area_group_includes_upper_bound(conn, area, group):
    assert conn.execute("SELECT area_group_of(%s::numeric)", (area,)).fetchone()[0] == group
