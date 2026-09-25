from pathlib import Path

import pytest

from etl.rtms import parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "api"


@pytest.fixture
def sale_items() -> list[dict[str, str]]:
    """서대문구(11410) 2025-06 매매 상세 실제 응답의 항목 578건."""
    return parse_page((FIXTURES / "sale_11410_202506.xml").read_bytes()).items


@pytest.fixture
def rent_items() -> list[dict[str, str]]:
    """서대문구(11410) 2025-06 전월세 실제 응답의 항목 639건."""
    return parse_page((FIXTURES / "rent_11410_202506.xml").read_bytes()).items
