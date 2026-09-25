"""법정동코드 전체자료를 lawd 테이블에 적재한다.

입력 형식은 세 가지다.
- 행정안전부 주소코드 공지의 KIKcd_B(말소코드포함) 고정폭 텍스트. 기본 입력이다.
  mois.go.kr "행정기관(행정동) 및 관할구역(법정동) 변경내역" 공지의 jscodeYYYYMMDD(말소코드포함).zip에 있다.
  컬럼은 법정동코드, 시도명, 시군구명, 읍면동명, 동리명, 생성일자, 말소일자이고 폭은 CP949 바이트 기준이다
- data.go.kr '국토교통부_법정동코드' CSV
- code.go.kr 전체자료 텍스트(탭 구분)
뒤의 둘은 컬럼이 법정동코드, 법정동명, 폐지여부 순이다. 인코딩은 UTF-8이 아니면 CP949로 읽는다.
data.go.kr CSV(20260813)에는 2026년 개편 코드(화성시 구, 인천 제물포구 등)가 없었다.

지역 범위와 무관하게 전국·폐지 코드까지 모두 적재한다(폐지 → is_active = FALSE).
lawd_cd 기준 UPSERT라 같은 파일을 여러 번 적재해도 행이 늘지 않는다.

    python -m etl.lawd KIKcd_B.20260701_말소코드포함.txt
"""

import argparse
import csv
import io
import re
from collections.abc import Iterator
from dataclasses import astuple, dataclass
from pathlib import Path

import psycopg

from etl.db import connect

STATUS_ACTIVE = "존재"
STATUS_ABOLISHED = "폐지"
MOIS_COLUMNS = ("법정동코드", "시도명", "시군구명", "읍면동명", "동리명", "생성일자", "말소일자")


@dataclass(frozen=True)
class LawdRow:
    lawd_cd: str
    lawd_cd5: str
    sido: str
    sigungu: str | None
    dong: str | None
    is_active: bool


def parse_row(code: str, name: str, status: str) -> LawdRow:
    """법정동명을 시도/시군구/동으로 나눈다.

    코드는 시도(2) + 시군구(3) + 읍면동(3) + 리(2) 자리이고, 끝자리가 0으로 채워진 정도로
    행의 레벨이 정해진다. 이름은 레벨마다 상위 이름을 모두 포함하므로 뒤에서부터 떼어낸다.
    - 시군구는 여러 토큰일 수 있다: '수원시 장안구'
    - 리 단위 행의 dong에는 '읍면 리'를 함께 둔다: '양평읍 양근리'
    - 세종특별자치시는 시군구가 없어 sigungu가 NULL이다
    """
    code = code.strip()
    status = status.strip()
    tokens = name.split()  # 원본에 후행 공백·중복 공백이 섞여 있다
    if len(code) != 10 or not code.isdigit():
        raise ValueError(f"법정동코드 형식 오류: {code!r}")
    if status not in (STATUS_ACTIVE, STATUS_ABOLISHED):
        raise ValueError(f"폐지여부 값 오류: {code} {status!r}")

    if code[2:] == "00000000":  # 시도
        min_tokens, sigungu, dong = 1, [], []
    elif code[5:] == "00000":  # 시군구
        min_tokens, sigungu, dong = 1, tokens[1:], []
    elif code[8:] == "00":  # 읍면동
        min_tokens, sigungu, dong = 2, tokens[1:-1], tokens[-1:]
    else:  # 리
        min_tokens, sigungu, dong = 3, tokens[1:-2], tokens[-2:]
    if len(tokens) < min_tokens:
        raise ValueError(f"법정동명이 코드 레벨과 맞지 않음: {code} {name!r}")

    return LawdRow(
        lawd_cd=code,
        lawd_cd5=code[:5],
        sido=tokens[0],
        sigungu=" ".join(sigungu) or None,
        dong=" ".join(dong) or None,
        is_active=status == STATUS_ACTIVE,
    )


def read_file(path: Path) -> list[LawdRow]:
    raw = path.read_bytes()
    first_line = raw.split(b"\n", 1)[0].decode("cp949", "replace")
    if first_line.split()[:2] == list(MOIS_COLUMNS[:2]):
        parse, records = parse_mois_fields, _mois_records(raw)
    else:
        parse, records = parse_row, _delimited_records(raw)

    rows: dict[str, LawdRow] = {}
    for line_no, fields in records:
        try:
            row = parse(*fields)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{path.name}:{line_no}: {e}") from e
        if row.lawd_cd in rows:
            raise ValueError(f"{path.name}:{line_no}: 법정동코드 중복 {row.lawd_cd}")
        rows[row.lawd_cd] = row
    return list(rows.values())


def _delimited_records(raw: bytes) -> Iterator[tuple[int, list[str]]]:
    """data.go.kr CSV 또는 code.go.kr 탭 구분 텍스트: 법정동코드, 법정동명, 폐지여부."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")

    first_line = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter="\t" if "\t" in first_line else ",")
    header = next(reader)
    if [h.strip() for h in header[:3]] != ["법정동코드", "법정동명", "폐지여부"]:
        raise ValueError(f"법정동코드 파일 헤더가 아님: {header!r}")
    for line_no, fields in enumerate(reader, start=2):
        if fields:
            yield line_no, fields[:3]


def _mois_records(raw: bytes) -> Iterator[tuple[int, list[str]]]:
    """행정안전부 KIKcd_B 고정폭 텍스트. 열 위치는 헤더에서 각 컬럼명이 시작하는 바이트로 정한다."""
    lines = raw.split(b"\n")
    header = lines[0]
    offsets: list[int] = []
    for name in MOIS_COLUMNS:
        at = header.find(name.encode("cp949"))
        if at < 0 or (offsets and at <= offsets[-1]):
            raise ValueError(f"행정안전부 법정동코드 헤더가 아님: {header.decode('cp949', 'replace').strip()!r}")
        offsets.append(at)
    spans = list(zip(offsets, offsets[1:] + [None]))
    for line_no, line in enumerate(lines[1:], start=2):
        if line.strip():
            yield line_no, [line[a:b].decode("cp949") for a, b in spans]


def parse_mois_fields(
    code: str, sido: str, sigungu: str, umd: str, ri: str, created: str, abolished: str
) -> LawdRow:
    """KIKcd_B 한 줄. 이름 안의 연속 공백('장  리')은 한 칸으로 줄이고,
    리 단위 행의 dong은 parse_row와 같이 '읍면 리'로 둔다."""
    code, sido, sigungu, umd, ri, abolished = (" ".join(f.split()) for f in (code, sido, sigungu, umd, ri, abolished))
    if len(code) != 10 or not code.isdigit():
        raise ValueError(f"법정동코드 형식 오류: {code!r}")
    if not sido:
        raise ValueError(f"시도명이 비어 있음: {code}")
    if abolished and not re.fullmatch(r"\d{8}", abolished):
        raise ValueError(f"말소일자 형식 오류: {code} {abolished!r}")
    return LawdRow(
        lawd_cd=code,
        lawd_cd5=code[:5],
        sido=sido,
        sigungu=sigungu or None,
        dong=" ".join(x for x in (umd, ri) if x) or None,
        is_active=not abolished,
    )


def load(conn: psycopg.Connection, rows: list[LawdRow]) -> dict[str, int]:
    """rows를 UPSERT하고 inserted/updated/unchanged 건수를 돌려준다."""
    with conn.transaction():
        conn.execute("CREATE TEMP TABLE lawd_stage (LIKE lawd) ON COMMIT DROP")
        with conn.cursor().copy(
            "COPY lawd_stage (lawd_cd, lawd_cd5, sido, sigungu, dong, is_active) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(astuple(row))

        # 값이 바뀐 행만 갱신한다. xmax = 0이면 이번에 새로 삽입된 행이다.
        written = conn.execute(
            """
            INSERT INTO lawd AS l (lawd_cd, lawd_cd5, sido, sigungu, dong, is_active)
            SELECT lawd_cd, lawd_cd5, sido, sigungu, dong, is_active FROM lawd_stage
            ON CONFLICT (lawd_cd) DO UPDATE SET
              lawd_cd5  = EXCLUDED.lawd_cd5,
              sido      = EXCLUDED.sido,
              sigungu   = EXCLUDED.sigungu,
              dong      = EXCLUDED.dong,
              is_active = EXCLUDED.is_active
            WHERE (l.lawd_cd5, l.sido, l.sigungu, l.dong, l.is_active)
                  IS DISTINCT FROM
                  (EXCLUDED.lawd_cd5, EXCLUDED.sido, EXCLUDED.sigungu, EXCLUDED.dong, EXCLUDED.is_active)
            RETURNING (xmax = 0)
            """
        ).fetchall()

    inserted = sum(1 for (is_insert,) in written if is_insert)
    updated = len(written) - inserted
    return {"inserted": inserted, "updated": updated, "unchanged": len(rows) - len(written)}


def main() -> None:
    parser = argparse.ArgumentParser(description="법정동코드 파일을 lawd 테이블에 적재한다.")
    parser.add_argument("path", type=Path, help="행정안전부 KIKcd_B(말소코드포함), data.go.kr CSV 또는 code.go.kr 텍스트")
    args = parser.parse_args()

    rows = read_file(args.path)
    with connect(autocommit=True) as conn:
        counts = load(conn, rows)
    active = sum(row.is_active for row in rows)
    print(
        f"read {len(rows)} rows (active {active}, abolished {len(rows) - active}) | "
        f"inserted {counts['inserted']}, updated {counts['updated']}, unchanged {counts['unchanged']}"
    )


if __name__ == "__main__":
    main()
