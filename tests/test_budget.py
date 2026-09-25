"""대출 한도·취득세·최대 매수가 함수(0006)와 2026-09-25 정책 값(0007). DATABASE_URL이 없으면 건너뛴다.

손계산 값과, SQL과 따로 쓴 Python 참조 구현(아래 reference_*)을 함께 대조한다.
"""

import itertools
import math
import os
from decimal import Decimal

import psycopg
import pytest

from etl.db import connect

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL 필요")

ON = "2026-09-25"

# 2026-09-25 정책 값(SPEC Phase 2 "결정론적 계산 함수" 표)
DSR, RATE, STRESS, YEARS = 0.40, 0.045, 0.030, 30
LTV_REG, LTV_REG_SEOMIN, LTV_FREE = 0.40, 0.60, 0.70
SEOMIN_INCOME, SEOMIN_PRICE = 9000, 80000
CAP_TIERS = ((150000, 60000), (250000, 40000), (math.inf, 20000))


@pytest.fixture(scope="module")
def conn():
    with connect(autocommit=True) as c:
        yield c


def budget(conn, *args, **kwargs):
    params = ", ".join(["%s"] * len(args) + [f"{k} => %s" for k in kwargs])
    cur = conn.execute(
        f"SELECT * FROM max_purchase_price({params}, on_date => %s)", (*args, *kwargs.values(), ON)
    )
    return dict(zip([c.name for c in cur.description], cur.fetchone()))


def reference_dsr_loan(income):
    r, n = (RATE + STRESS) / 12, YEARS * 12
    return math.floor(income * DSR / 12 * (1 - (1 + r) ** -n) / r)


def reference_tax(price, area=None):
    if price <= 60000:
        rate = Decimal("0.01")
    elif price <= 90000:
        # 지방세법 식 그대로. float로 계산하면 0.01215 같은 반올림 경계에서 틀린다
        rate = ((Decimal(price) / 10000 * 2 / 3 - 3) / 100).quantize(Decimal("0.0001"), rounding="ROUND_HALF_UP")
    else:
        rate = Decimal("0.03")
    rural = Decimal("0.002") if area is not None and area > 85 else 0
    return int((price * (rate * Decimal("1.1") + rural)).quantize(Decimal(1), rounding="ROUND_HALF_UP"))


def reference_loan(price, regulated, seomin_ok, dsr_loan, user_cap=None):
    seomin = regulated and seomin_ok and price <= SEOMIN_PRICE
    ltv = LTV_FREE if not regulated else LTV_REG_SEOMIN if seomin else LTV_REG
    cap = next(c for limit, c in CAP_TIERS if price <= limit)
    if user_cap is not None:
        cap = min(cap, user_cap)
    return max(min(math.floor(price * ltv), dsr_loan, cap), 0)


def reference_max_price(equity, income, regulated, head=False, household=None, area=None, user_cap=None):
    seomin_ok = head and (household if household is not None else income) <= SEOMIN_INCOME
    dsr_loan = reference_dsr_loan(income)

    def fits(p):
        return p - reference_loan(p, regulated, seomin_ok, dsr_loan, user_cap) + reference_tax(p, area) <= equity

    # 필요 현금이 가격에 대해 줄지 않는다는 SQL의 가정에 기대지 않도록, 상한에서 1만원씩 내려오며 찾는다
    p = equity + max(min(dsr_loan, 60000, user_cap if user_cap is not None else 60000), 0) + 1
    step = 1000
    while not fits(p):  # 성긴 간격으로 내려와서
        p -= step
    while fits(p + 1):  # 1만원 단위로 맞춘다
        p += 1
    return p


def test_dsr_loan_matches_annuity_formula(conn):
    assert conn.execute("SELECT max_loan_by_dsr(8000, on_date => %s)", (ON,)).fetchone()[0] == 38138
    assert reference_dsr_loan(8000) == 38138
    # 인자로 준 값이 정책 값보다 우선한다
    assert conn.execute("SELECT max_loan_by_dsr(8000, 0.4, 0.04, 0.0, 30)").fetchone()[0] == math.floor(
        8000 * 0.4 / 12 * (1 - (1 + 0.04 / 12) ** -360) / (0.04 / 12)
    )


@pytest.mark.parametrize(
    ("price", "area", "expected"),
    [
        (50000, None, 550),      # 1% + 지방교육세 0.1%
        (60000, None, 660),
        (60001, None, 660),      # 6억 초과 직후도 1.00%
        (70000, None, 1286),     # 1.67%(소수 넷째 자리 반올림) × 1.1
        (70000, 100, 1426),      # 85m2 초과면 농어촌특별세 0.2% 추가
        (70000, 85, 1286),       # 85m2는 초과가 아니다
        (90000, None, 2970),
        (100000, None, 3300),
    ],
)
def test_acquisition_tax(conn, price, area, expected):
    assert conn.execute("SELECT acquisition_tax(%s, %s, %s)", (price, area, ON)).fetchone()[0] == expected
    assert reference_tax(price, area) == expected


def test_default_profile_by_hand(conn):
    """자기자본 3억, 연소득 8천. 손계산은 SPEC 변경 이력 Phase 2 착수 전 결정."""
    # 규제지역 일반: 0.6P + 1.1%P = 3억 → P = 49,100, 대출 19,640(LTV 40%)
    reg = budget(conn, 30000, 8000, True)
    assert (reg["max_price"], reg["loan_amount"], reg["binding_factor"]) == (49100, 19640, "LTV")
    assert (reg["acquisition_tax"], reg["cash_needed"], reg["dsr_loan_limit"]) == (540, 30000, 38138)
    # 비규제: LTV 70%면 대출이 DSR 한도를 넘어 DSR이 묶는다. P = 3억 + 38,138 − 취득세 1,084
    free = budget(conn, 30000, 8000, False)
    assert (free["max_price"], free["loan_amount"], free["binding_factor"]) == (67054, 38138, "DSR")
    assert free["acquisition_tax"] == 1084 and free["cash_needed"] == 30000
    # 규제지역이어도 서민·실수요자(무주택 세대주, 부부합산 9천 이하)면 LTV 60%라 DSR이 묶는다
    seomin = budget(conn, 30000, 8000, True, homeless_household_head=True)
    assert (seomin["max_price"], seomin["binding_factor"], seomin["ltv"], seomin["seomin"]) == (
        67054, "DSR", Decimal("0.60"), True,
    )


def test_binding_factor_differs_by_regulation(conn):
    assert budget(conn, 30000, 8000, True)["binding_factor"] == "LTV"
    assert budget(conn, 30000, 8000, False)["binding_factor"] == "DSR"


def test_seomin_needs_household_income_and_head(conn):
    assert budget(conn, 30000, 8000, True, homeless_household_head=True, household_income_manwon=9500)["ltv"] == Decimal("0.40")
    assert budget(conn, 30000, 8000, True, homeless_household_head=False)["ltv"] == Decimal("0.40")


def test_seomin_price_limit_is_the_binding_edge(conn):
    # 8억에서는 DSR 한도(42,905)까지 빌리지만, 8억을 넘으면 LTV가 40%로 떨어져 살 수 없다
    r = budget(conn, 40000, 9000, True, homeless_household_head=True)
    assert (r["max_price"], r["loan_amount"], r["dsr_loan_limit"]) == (80000, 42905, 42905)
    assert (r["binding_factor"], r["binding_detail"], r["seomin"]) == ("LTV", "seomin_price_limit", True)
    assert r["cash_needed"] == 80000 - 42905 + 2050 < 40000


def test_loan_cap_tiers(conn):
    # 15억 초과 구간: 상한 4억이 묶는다. 1.033P − 4억 = 12억 → P = 154,889
    r = budget(conn, 120000, 30000, False)
    assert (r["max_price"], r["loan_amount"], r["binding_factor"], r["cap_loan_limit"]) == (154889, 40000, "CAP", 40000)
    # 사용자가 따로 둔 상한
    r = budget(conn, 30000, 8000, False, loan_cap_manwon=10000)
    assert (r["loan_amount"], r["binding_factor"]) == (10000, "CAP")


def test_no_income_means_equity_only(conn):
    r = budget(conn, 30000, 0, False)
    assert (r["loan_amount"], r["binding_factor"]) == (0, "EQUITY")
    assert r["max_price"] + r["acquisition_tax"] <= 30000 < r["max_price"] + 1 + r["acquisition_tax"]


@pytest.mark.parametrize(
    ("equity", "income", "regulated", "head"),
    list(itertools.product((5000, 20000, 30000, 45000, 90000, 150000), (0, 4000, 8000, 15000), (True, False), (True, False))),
)
def test_matches_reference(conn, equity, income, regulated, head):
    r = budget(conn, equity, income, regulated, homeless_household_head=head)
    assert r["max_price"] == reference_max_price(equity, income, regulated, head=head)
    assert r["cash_needed"] <= equity


def test_is_regulated(conn):
    q = "SELECT is_regulated(%s, %s)"
    assert conn.execute(q, ("11410", ON)).fetchone()[0]           # 서대문구: 10·15
    assert conn.execute(q, ("11680", ON)).fetchone()[0]           # 강남구: 2023년 해제 때 유지
    assert not conn.execute(q, ("41390", ON)).fetchone()[0]       # 시흥시
    assert not conn.execute(q, ("41597", "2026-06-30")).fetchone()[0]  # 동탄구: 2026-07-01부터
    assert conn.execute(q, ("41597", "2026-07-01")).fetchone()[0]
    assert not conn.execute(q, ("11410", "2025-10-15")).fetchone()[0]


def test_missing_policy_value_is_an_error(conn):
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("SELECT policy_num('no_such_param')")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("SELECT ltv_regulated FROM (SELECT policy_num('ltv_regulated', '2025-10-15') AS ltv_regulated) t")
