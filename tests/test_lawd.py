import pytest

from etl.lawd import LawdRow, parse_row, read_file


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    [
        ("1100000000", "서울특별시", ("11000", "서울특별시", None, None)),
        ("1141000000", "서울특별시 서대문구", ("11410", "서울특별시", "서대문구", None)),
        ("1141011800", "서울특별시 서대문구 홍은동", ("11410", "서울특별시", "서대문구", "홍은동")),
        # 시 아래 구가 있는 경우 시군구가 두 토큰이다
        ("4111100000", "경기도 수원시 장안구", ("41111", "경기도", "수원시 장안구", None)),
        ("4111112900", "경기도 수원시 장안구 파장동", ("41111", "경기도", "수원시 장안구", "파장동")),
        # 리 단위는 읍면과 리를 dong에 함께 둔다
        ("4183025021", "경기도 양평군 양평읍 양근리", ("41830", "경기도", "양평군", "양평읍 양근리")),
        (
            "4146125021",
            "경기도 용인시 처인구 포곡읍 삼계리",
            ("41461", "경기도", "용인시 처인구", "포곡읍 삼계리"),
        ),
        # 세종특별자치시는 시군구가 없다
        ("3611000000", "세종특별자치시", ("36110", "세종특별자치시", None, None)),
        ("3611010100", "세종특별자치시 반곡동", ("36110", "세종특별자치시", None, "반곡동")),
        ("3611025021", "세종특별자치시 조치원읍 원리", ("36110", "세종특별자치시", None, "조치원읍 원리")),
        # 원본의 후행 공백
        ("4119200000", "경기도 부천시 원미구 ", ("41192", "경기도", "부천시 원미구", None)),
    ],
)
def test_parse_row_splits_name_by_code_level(code, name, expected):
    row = parse_row(code, name, "존재")
    assert (row.lawd_cd5, row.sido, row.sigungu, row.dong) == expected
    assert row.lawd_cd == code
    assert row.is_active


def test_parse_row_abolished_is_inactive():
    assert parse_row("2300000000", "인천직할시", "폐지").is_active is False


@pytest.mark.parametrize(
    ("code", "name", "status"),
    [
        ("111101010", "서울특별시 종로구 청운동", "존재"),  # 9자리
        ("11110101AB", "서울특별시 종로구 청운동", "존재"),
        ("1111010100", "서울특별시 종로구 청운동", "모름"),
        ("1111010100", "서울특별시", "존재"),  # 읍면동 코드인데 이름이 시도뿐
        ("1111010100", "   ", "존재"),
    ],
)
def test_parse_row_rejects_malformed(code, name, status):
    with pytest.raises(ValueError):
        parse_row(code, name, status)


def test_read_file_cp949_csv(tmp_path):
    path = tmp_path / "lawd.csv"
    path.write_bytes(
        "법정동코드,법정동명,폐지여부\r\n"
        "1100000000,서울특별시,존재\r\n"
        "1141011800,서울특별시 서대문구 홍은동,존재\r\n"
        "2300000000,인천직할시,폐지\r\n".encode("cp949")
    )
    assert read_file(path) == [
        LawdRow("1100000000", "11000", "서울특별시", None, None, True),
        LawdRow("1141011800", "11410", "서울특별시", "서대문구", "홍은동", True),
        LawdRow("2300000000", "23000", "인천직할시", None, None, False),
    ]


def test_read_file_code_go_kr_tab_format(tmp_path):
    path = tmp_path / "법정동코드 전체자료.txt"
    path.write_bytes("법정동코드\t법정동명\t폐지여부\n1141011800\t서울특별시 서대문구 홍은동\t존재\n".encode("cp949"))
    assert read_file(path) == [LawdRow("1141011800", "11410", "서울특별시", "서대문구", "홍은동", True)]


def test_read_file_rejects_duplicate_code(tmp_path):
    path = tmp_path / "lawd.csv"
    path.write_text(
        "법정동코드,법정동명,폐지여부\n1100000000,서울특별시,존재\n1100000000,서울특별시,존재\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="중복"):
        read_file(path)


def test_read_file_rejects_unknown_header(tmp_path):
    path = tmp_path / "other.csv"
    path.write_text("code,name\n1100000000,서울특별시\n", encoding="utf-8")
    with pytest.raises(ValueError, match="헤더"):
        read_file(path)


def mois_line(code, sido="", sigungu="", umd="", ri="", created="19880423", abolished="") -> bytes:
    """KIKcd_B 고정폭 한 줄 (CP949 바이트 폭: 11, 31, 31, 31, 31, 9, 이후 말소일자)."""
    def pad(text: str, width: int) -> bytes:
        b = text.encode("cp949")
        return b + b" " * (width - len(b))
    return (
        pad(code, 11) + pad(sido, 31) + pad(sigungu, 31) + pad(umd, 31) + pad(ri, 31)
        + pad(created, 9) + pad(abolished, 20) + b"\n"
    )


MOIS_HEADER = mois_line("법정동코드", "시도명", "시군구명", "읍면동명", "동리명", "생성일자", "말소일자")


def test_read_file_mois_fixed_width(tmp_path):
    path = tmp_path / "KIKcd_B.20260701(말소코드포함)"
    path.write_bytes(
        MOIS_HEADER
        + mois_line("1100000000", "서울특별시")
        + mois_line("2811000000", "인천광역시", "중구", created="19950101", abolished="20260701")
        + mois_line("2812500000", "인천광역시", "제물포구", created="20260701")
        + mois_line("4159100000", "경기도", "화성시 만세구", created="20260201")
        + mois_line("4183025021", "경기도", "양평군", "양평읍", "양근리")
        + mois_line("4778036031", "경상북도", "영일군", "대송면", "장  리", abolished="19950101")
        + mois_line("3611010100", "세종특별자치시", "", "반곡동")
    )
    assert read_file(path) == [
        LawdRow("1100000000", "11000", "서울특별시", None, None, True),
        LawdRow("2811000000", "28110", "인천광역시", "중구", None, False),
        LawdRow("2812500000", "28125", "인천광역시", "제물포구", None, True),
        LawdRow("4159100000", "41591", "경기도", "화성시 만세구", None, True),
        LawdRow("4183025021", "41830", "경기도", "양평군", "양평읍 양근리", True),
        LawdRow("4778036031", "47780", "경상북도", "영일군", "대송면 장 리", False),
        LawdRow("3611010100", "36110", "세종특별자치시", None, "반곡동", True),
    ]


@pytest.mark.parametrize(
    "line",
    [
        mois_line("281250000", "인천광역시", "제물포구"),  # 9자리
        mois_line("2812500000"),  # 시도명 없음
        mois_line("2811000000", "인천광역시", "중구", abolished="2026.7.1"),
    ],
)
def test_read_file_mois_rejects_malformed(tmp_path, line):
    path = tmp_path / "KIKcd_B"
    path.write_bytes(MOIS_HEADER + line)
    with pytest.raises(ValueError, match="KIKcd_B:2"):
        read_file(path)
