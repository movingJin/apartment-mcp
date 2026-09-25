"""단지 예외 목록(danji_flag). "시세용 매매가 없는 단지" 규칙이 못 잡는 단지를 사람이 확인해 올린다.

    python -m etl.flags --candidates              # 검토할 후보를 db/danji_flag_candidates.csv로
    python -m etl.flags --load                    # db/danji_flag.csv를 danji_flag와 맞춘다
    python -m etl.flags --list                    # 지금 올라 있는 목록

판정 종류 (v_danji_status)
- rental          : 임대. 모든 통계와 후보에서 뺀다
- land_lease      : 토지임대부. 땅값이 빠진 가격이라 단지 자체 시세만 두고 비교·지역 통계·후보에서 뺀다
- sale_conversion : 분양전환. converted_on(비우면 첫 시세용 매매일) 이전 전월세는 규제 임대료라 쓰지 않는다

후보는 이름과 거래 패턴으로 찾지만 판정은 사람이 한다(단지명 매칭으로 판정하지 않는다).
- 이름 키워드: 임대·토지임대·뉴스테이·행복주택·NHF 등. `힐스테이트`, `리츠빌`, `신영구월`, `국민주택`처럼
  브랜드·지명·옛 이름에 걸리는 말(스테이, 리츠, 영구, 국민)은 쓰지 않는다
- 분양전환 패턴: 준공 뒤 한동안 매매 없이 전월세만 많다가 매매가 시작된 단지.
  매매 시작 전 전월세가 월 3건 이상이면 대부분 임대(NHF·부영·LH·공공임대)이고, 그보다 적으면 거래가 드문 일반 단지다

db/danji_flag.csv가 기준이다. --load는 파일에 없는 행을 지운다. 바꾼 뒤 `python -m etl.refresh`를 실행해야 뷰에 반영된다
"""

import argparse
import csv
import sys
from dataclasses import astuple, dataclass, fields
from datetime import date
from pathlib import Path

import psycopg

from etl.db import connect

DB_DIR = Path(__file__).resolve().parent.parent / "db"
FLAG_FILE = DB_DIR / "danji_flag.csv"
CANDIDATE_FILE = DB_DIR / "danji_flag_candidates.csv"
FLAGS = ("rental", "land_lease", "sale_conversion")

# 후보 찾기에만 쓴다. 판정은 사람이 한다
NAME_PATTERN = r"토지임대|임대|뉴스테이|행복주택|NHF|엔에이치에프|공공|브리즈힐"
MIN_RENTS_BEFORE_SALE = 30
MIN_RENTS_PER_MONTH_BEFORE_SALE = 3

CANDIDATE_SQL = """
WITH st AS (
  SELECT * FROM v_danji_status WHERE scope <> 'excluded' AND flag IS NULL
), r AS (
  SELECT danji_id, count(*) AS n, min(deal_date) AS first_rent FROM trade_rent GROUP BY 1
), rb AS (
  SELECT t.danji_id, count(*) AS n
  FROM trade_rent t JOIN st USING (danji_id)
  WHERE t.deal_date < st.first_market_sale
  GROUP BY 1
), direct AS (
  SELECT danji_id, count(*) AS n FROM trade_sale WHERE NOT is_canceled AND dealing_type = '직거래' GROUP BY 1
), x AS (
  SELECT d.apt_seq, d.name, l.sigungu, d.built_year, d.households,
         st.market_sale_count, COALESCE(direct.n, 0) AS direct_sale_count, st.first_market_sale,
         r.first_rent, COALESCE(r.n, 0) AS rent_count, COALESCE(rb.n, 0) AS rent_before_first_sale,
         round(COALESCE(rb.n, 0) / greatest((st.first_market_sale - r.first_rent) / 30.4, 1), 1) AS rents_per_month_before,
         (regexp_match(d.name, %(pattern)s))[1] AS keyword,
         d.built_year <= date_part('year', r.first_rent) - 2
           AND st.first_market_sale > r.first_rent + interval '12 months'
           AND COALESCE(rb.n, 0) >= %(min_before)s
           AND COALESCE(rb.n, 0) / greatest((st.first_market_sale - r.first_rent) / 30.4, 1) >= %(min_per_month)s
           AS conversion_pattern
  FROM danji d
  JOIN st USING (danji_id)
  LEFT JOIN r USING (danji_id)
  LEFT JOIN rb USING (danji_id)
  LEFT JOIN direct USING (danji_id)
  LEFT JOIN lawd l ON l.lawd_cd = d.lawd_cd5 || '00000'
)
SELECT apt_seq, name, sigungu, built_year, households, market_sale_count, direct_sale_count,
       first_market_sale, first_rent, rent_count, rent_before_first_sale, rents_per_month_before, keyword,
       CASE
         WHEN name ~ '토지임대' THEN 'land_lease'
         WHEN conversion_pattern THEN 'sale_conversion'
       END AS suggested_flag,
       concat_ws(' + ',
                 CASE WHEN keyword IS NOT NULL THEN '이름 ' || keyword END,
                 CASE WHEN conversion_pattern THEN '매매 전 전월세 ' || rent_before_first_sale || '건' END) AS reason
FROM x
WHERE keyword IS NOT NULL OR conversion_pattern
ORDER BY suggested_flag NULLS LAST, rent_before_first_sale DESC, apt_seq
"""


def write_candidates(conn: psycopg.Connection, path: Path) -> int:
    cur = conn.execute(
        CANDIDATE_SQL,
        {"pattern": NAME_PATTERN, "min_before": MIN_RENTS_BEFORE_SALE, "min_per_month": MIN_RENTS_PER_MONTH_BEFORE_SALE},
    )
    rows = cur.fetchall()
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([c.name for c in cur.description])
        w.writerows(rows)
    return len(rows)


@dataclass(frozen=True)
class Flag:
    apt_seq: str
    flag: str
    converted_on: date | None
    note: str
    reviewed_on: date


def read_flag_file(path: Path) -> list[Flag]:
    """db/danji_flag.csv를 읽는다. name 등 다른 열은 사람이 보라고 둔 것이라 무시한다."""
    flags = []
    with path.open(encoding="utf-8", newline="") as f:
        for lineno, row in enumerate(csv.DictReader(f), start=2):
            try:
                flag = Flag(
                    apt_seq=row["apt_seq"].strip(),
                    flag=row["flag"].strip(),
                    converted_on=date.fromisoformat(row["converted_on"]) if row.get("converted_on", "").strip() else None,
                    note=row["note"].strip(),
                    reviewed_on=date.fromisoformat(row["reviewed_on"].strip()),
                )
            except (KeyError, ValueError) as e:
                raise ValueError(f"{path.name}:{lineno}: {e}") from e
            if flag.flag not in FLAGS:
                raise ValueError(f"{path.name}:{lineno}: flag는 {', '.join(FLAGS)} 중 하나: {flag.flag!r}")
            if flag.converted_on and flag.flag != "sale_conversion":
                raise ValueError(f"{path.name}:{lineno}: converted_on은 sale_conversion에만 쓴다")
            if not flag.note:
                raise ValueError(f"{path.name}:{lineno}: note(판정 근거)가 비었다")
            flags.append(flag)
    seqs = [f.apt_seq for f in flags]
    if len(seqs) != len(set(seqs)):
        raise ValueError(f"{path.name}: apt_seq 중복: {sorted({s for s in seqs if seqs.count(s) > 1})}")
    return flags


def load_flags(conn: psycopg.Connection, flags: list[Flag]) -> tuple[int, int, int]:
    """danji_flag를 flags와 똑같이 맞춘다. (추가, 바뀜, 지움) 수를 돌려준다."""
    columns = [f.name for f in fields(Flag)]
    with conn.transaction():
        existing = {
            row[0]: Flag(*row)
            for row in conn.execute(f"SELECT {', '.join(columns)} FROM danji_flag")
        }
        wanted = {f.apt_seq: f for f in flags}
        unknown = sorted(
            set(wanted) - {s for (s,) in conn.execute("SELECT apt_seq FROM danji WHERE apt_seq = ANY(%s)", (list(wanted),))}
        )
        if unknown:
            raise ValueError(f"danji에 없는 apt_seq: {unknown}")
        stale = sorted(set(existing) - set(wanted))
        if stale:
            conn.execute("DELETE FROM danji_flag WHERE apt_seq = ANY(%s)", (stale,))
        changed = [f for f in flags if f.apt_seq in existing and existing[f.apt_seq] != f]
        added = [f for f in flags if f.apt_seq not in existing]
        for f in changed + added:
            conn.execute(
                f"""
                INSERT INTO danji_flag ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})
                ON CONFLICT (apt_seq) DO UPDATE SET
                  {', '.join(f'{c} = EXCLUDED.{c}' for c in columns[1:])}
                """,
                astuple(f),
            )
    return len(added), len(changed), len(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description="단지 예외 목록(danji_flag) 후보 추출과 적재")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--candidates", action="store_true", help=f"검토할 후보를 {CANDIDATE_FILE.name}로 쓴다")
    group.add_argument("--load", action="store_true", help=f"{FLAG_FILE.name}을 danji_flag와 맞춘다")
    group.add_argument("--list", action="store_true", help="지금 올라 있는 목록")
    parser.add_argument("--file", type=Path, help="--candidates의 출력 또는 --load의 입력 파일")
    args = parser.parse_args()

    with connect(autocommit=True) as conn:
        if args.candidates:
            path = args.file or CANDIDATE_FILE
            n = write_candidates(conn, path)
            print(f"후보 {n}개 → {path}")
            print("확인한 단지를 apt_seq,flag,converted_on,note,reviewed_on 열로 db/danji_flag.csv에 옮긴 뒤 --load")
        elif args.load:
            path = args.file or FLAG_FILE
            try:
                added, changed, removed = load_flags(conn, read_flag_file(path))
            except ValueError as e:
                sys.exit(f"적재하지 않음: {e}")
            print(f"{path.name}: 추가 {added}, 바뀜 {changed}, 지움 {removed}")
            if added or changed or removed:
                print("뷰에 반영하려면 python -m etl.refresh")
        else:
            for row in conn.execute(
                "SELECT f.flag, f.apt_seq, d.name, f.converted_on, f.note FROM danji_flag f JOIN danji d USING (apt_seq)"
                " ORDER BY 1, 2"
            ):
                print(" | ".join("" if v is None else str(v) for v in row))


if __name__ == "__main__":
    main()
