import os

import pytest

from etl.db import connect
from etl.regions import RECODED, REGIONS

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")


def test_region_counts():
    assert len(REGIONS) == 83
    by_sido = {p: sum(code.startswith(p) for code in REGIONS) for p in ("11", "28", "41")}
    assert by_sido == {"11": 25, "28": 11, "41": 47}
    assert not RECODED.keys() & REGIONS.keys()
    assert all(code in REGIONS for codes in RECODED.values() for code in codes)


@needs_db
def test_regions_match_lawd():
    """lawd(행정안전부 KIKcd_B 20260701)의 수도권 활성 시군구에서 구가 있는 시와 출장소를 빼면 REGIONS와 같다.
    개편 전 코드는 폐지로 적재돼 있다."""
    with connect() as conn:
        from_lawd = dict(
            conn.execute(
                """
                WITH sgg AS (
                  SELECT lawd_cd5, sido, sigungu FROM lawd
                  WHERE is_active AND lawd_cd LIKE '_____00000' AND lawd_cd NOT LIKE '__00000000'
                    AND sido IN ('서울특별시', '인천광역시', '경기도') AND sigungu NOT LIKE '%출장')
                SELECT lawd_cd5, sido || ' ' || sigungu FROM sgg s
                WHERE NOT EXISTS (SELECT 1 FROM sgg o WHERE o.sido = s.sido AND o.sigungu LIKE s.sigungu || ' %')
                """
            ).fetchall()
        )
        abolished = {
            code for (code,) in conn.execute(
                "SELECT lawd_cd5 FROM lawd WHERE lawd_cd5 = ANY(%s) AND lawd_cd LIKE '_____00000' AND NOT is_active",
                (list(RECODED),),
            )
        }
    assert from_lawd == REGIONS
    assert abolished == RECODED.keys() - {"41590"}  # 화성시는 구가 생긴 뒤에도 시 코드가 활성이다
