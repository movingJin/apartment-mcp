import os
from datetime import date

import pytest

from etl.db import connect
from etl.flags import FLAG_FILE, Flag, load_flags, read_flag_file

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")

HEADER = "apt_seq,name,flag,converted_on,note,reviewed_on\n"


def write(tmp_path, body: str):
    path = tmp_path / "danji_flag.csv"
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def test_reads_flags_and_ignores_name_column(tmp_path):
    path = write(tmp_path, "11680-4341,강남브리즈힐,land_lease,,토지임대부,2026-09-25\n"
                           "41590-1761,부영6,sale_conversion,2025-04-01,전환,2026-09-25\n")
    assert read_flag_file(path) == [
        Flag("11680-4341", "land_lease", None, "토지임대부", date(2026, 9, 25)),
        Flag("41590-1761", "sale_conversion", date(2025, 4, 1), "전환", date(2026, 9, 25)),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("A,x,public_rental,,근거,2026-09-25\n", "flag는"),
        ("A,x,land_lease,2025-01-01,근거,2026-09-25\n", "converted_on은 sale_conversion에만"),
        ("A,x,rental,,,2026-09-25\n", "note"),
        ("A,x,rental,,근거,\n", ":2:"),
        ("A,x,rental,,근거,2026-09-25\nA,y,rental,,근거,2026-09-25\n", "중복"),
    ],
)
def test_rejects_bad_rows(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        read_flag_file(write(tmp_path, body))


def test_checked_in_file_is_valid():
    flags = read_flag_file(FLAG_FILE)
    assert {f.flag for f in flags} <= {"rental", "land_lease", "sale_conversion"}
    assert any(f.apt_seq == "11680-4341" and f.flag == "land_lease" for f in flags)  # 강남브리즈힐(토지임대부)


@needs_db
def test_load_syncs_table_to_file():
    with connect(autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("DELETE FROM danji_flag")
            seqs = [s for (s,) in conn.execute("SELECT apt_seq FROM danji ORDER BY danji_id LIMIT 3")]
            first = [Flag(seqs[0], "rental", None, "a", date(2026, 9, 25)),
                     Flag(seqs[1], "land_lease", None, "b", date(2026, 9, 25))]
            assert load_flags(conn, first) == (2, 0, 0)
            assert load_flags(conn, first) == (0, 0, 0)  # 같은 파일이면 바뀌는 것이 없다

            second = [Flag(seqs[0], "rental", None, "근거 수정", date(2026, 9, 26)),
                      Flag(seqs[2], "sale_conversion", date(2025, 1, 1), "c", date(2026, 9, 26))]
            assert load_flags(conn, second) == (1, 1, 1)  # 추가 seqs[2], 바뀜 seqs[0], 지움 seqs[1]
            assert conn.execute("SELECT apt_seq, note FROM danji_flag ORDER BY apt_seq").fetchall() == sorted(
                [(seqs[0], "근거 수정"), (seqs[2], "c")]
            )

            with pytest.raises(ValueError, match="danji에 없는"):
                load_flags(conn, [Flag("NO-SUCH", "rental", None, "x", date(2026, 9, 25))])
