-- 구현 순서 5단계(Phase 2): 대출 한도·취득세·최대 매수가 계산 함수. SPEC.md "결정론적 계산 함수".
-- 금액은 모두 만원. 정책 값은 policy_params에서 on_date 시점 값을 읽는다(인자로 주면 그 값).
-- 적용 범위는 수도권이다(대출 상한·스트레스 금리가 수도권·규제지역 기준).
-- 모형에 없는 것: 기존 부채(DSR), 정책대출(디딤돌·보금자리), 생애최초 우대, 고정금리 상품의 스트레스 금리 감면

-- 정책 값: on_date에 유효한 가장 최근 값. 없으면 오류다(빈 값으로 조용히 계산하지 않는다)
CREATE FUNCTION policy_num(p_key TEXT, p_on DATE DEFAULT as_of_date()) RETURNS NUMERIC AS $$
DECLARE
  v NUMERIC;
BEGIN
  SELECT value_num INTO v
  FROM policy_params
  WHERE param_key = p_key AND effective_from <= p_on
  ORDER BY effective_from DESC
  LIMIT 1;
  IF v IS NULL THEN
    RAISE EXCEPTION 'policy_params에 % 값이 없다 (% 기준)', p_key, p_on;
  END IF;
  RETURN v;
END
$$ LANGUAGE plpgsql STABLE;

-- 규제지역(투기과열지구 또는 조정대상지역) 여부. 토지거래허가구역은 LTV와 무관해 보지 않는다
CREATE FUNCTION is_regulated(p_lawd_cd5 CHAR(5), p_on DATE DEFAULT as_of_date()) RETURNS BOOLEAN AS $$
  SELECT EXISTS (
    SELECT 1 FROM regulated_area
    WHERE lawd_cd5 = p_lawd_cd5
      AND zone_type IN ('투기과열지구', '조정대상지역')
      AND effective_from <= p_on
      AND (effective_to IS NULL OR effective_to >= p_on)
  )
$$ LANGUAGE sql STABLE;

-- 스트레스 DSR 적용 최대 대출액. 원리금균등상환 역산:
--   r = (base_rate + stress_rate) / 12, n = years × 12, 월상환한도 = 연소득 × dsr_ratio / 12
--   대출액 = 월상환한도 × (1 − (1 + r)^−n) / r   (만원 미만 버림)
-- 스트레스 금리는 전부 더한다(변동금리 기준). 고정금리 기간이 있는 상품은 일부만 반영돼 실제 한도가 더 크다
CREATE FUNCTION max_loan_by_dsr(
  annual_income_manwon INT,
  dsr_ratio            NUMERIC DEFAULT NULL,
  base_rate            NUMERIC DEFAULT NULL,
  stress_rate          NUMERIC DEFAULT NULL,
  years                INT     DEFAULT NULL,
  on_date              DATE    DEFAULT as_of_date()
) RETURNS INT AS $$
DECLARE
  r NUMERIC;
  n INT;
  monthly NUMERIC;
BEGIN
  dsr_ratio   := COALESCE(dsr_ratio,   policy_num('dsr_ratio', on_date));
  base_rate   := COALESCE(base_rate,   policy_num('assumed_mortgage_rate', on_date));
  stress_rate := COALESCE(stress_rate, policy_num('stress_rate', on_date));
  years       := COALESCE(years,       policy_num('loan_years', on_date)::int);
  monthly := annual_income_manwon * dsr_ratio / 12;
  r := (base_rate + stress_rate) / 12;
  n := years * 12;
  IF r = 0 THEN
    RETURN floor(monthly * n);
  END IF;
  RETURN floor(monthly * (1 - power(1 + r, -n)) / r);
END
$$ LANGUAGE plpgsql STABLE;

-- 1주택(무주택자가 1채 취득) 유상취득 세금 합계: 취득세 + 지방교육세 + 농어촌특별세(만원, 반올림).
--   취득세율: 가격 ≤ 6억 1%, 6억 초과 ~ 9억 이하는 1%~3% 직선(지방세법 (가격(억) × 2/3 − 3)%와 같다.
--             소수 넷째 자리까지 반올림), 9억 초과 3%
--   지방교육세 = 취득세율 × 10%, 농어촌특별세 = 전용 85m2 초과면 가격 × 0.2%
-- area_excl이 NULL이면 85m2 이하로 본다
CREATE FUNCTION acquisition_tax(
  price_manwon INT,
  area_excl    NUMERIC DEFAULT NULL,
  on_date      DATE    DEFAULT as_of_date()
) RETURNS INT AS $$
DECLARE
  low_limit  NUMERIC := policy_num('acq_tax_low_price_limit', on_date);
  high_limit NUMERIC := policy_num('acq_tax_high_price_limit', on_date);
  low_rate   NUMERIC := policy_num('acq_tax_low_rate', on_date);
  high_rate  NUMERIC := policy_num('acq_tax_high_rate', on_date);
  rate       NUMERIC;
  rural      NUMERIC := 0;
BEGIN
  IF price_manwon <= low_limit THEN
    rate := low_rate;
  ELSIF price_manwon <= high_limit THEN
    rate := round(low_rate + (price_manwon - low_limit) / (high_limit - low_limit) * (high_rate - low_rate), 4);
  ELSE
    rate := high_rate;
  END IF;
  IF area_excl > policy_num('rural_tax_area_limit', on_date) THEN
    rural := policy_num('rural_tax_rate', on_date);
  END IF;
  RETURN round(price_manwon * (rate * (1 + policy_num('edu_tax_ratio', on_date)) + rural));
END
$$ LANGUAGE plpgsql STABLE;

-- 주택가격 price_manwon에서 받을 수 있는 대출과 그 한도들.
--   LTV: 비규제 ltv_non_regulated. 규제지역은 ltv_regulated, 서민·실수요자이고 가격이 seomin_price_limit 이하면 ltv_regulated_seomin
--   상한: 주택가격 구간별 정책 상한(loan_cap_tier*)과 loan_cap_manwon(사용자가 따로 둔 상한) 중 작은 값
--   대출 = min(LTV 한도, DSR 한도, 상한). binding_factor는 그 최소가 된 한도, 대출이 0이면 'EQUITY'
CREATE FUNCTION purchase_loan_terms(
  price_manwon    INT,
  is_regulated    BOOLEAN,
  seomin_eligible BOOLEAN,   -- 서민·실수요자 소득·세대주 요건 충족(가격 요건은 여기서 본다)
  dsr_loan_limit  INT,
  loan_cap_manwon INT  DEFAULT NULL,
  on_date         DATE DEFAULT as_of_date()
) RETURNS TABLE (
  ltv            NUMERIC,
  seomin         BOOLEAN,
  ltv_loan_limit INT,
  cap_loan_limit INT,
  loan_amount    INT,
  binding_factor TEXT
) AS $$
BEGIN
  seomin := is_regulated AND seomin_eligible AND price_manwon <= policy_num('seomin_price_limit', on_date);
  ltv := CASE
           WHEN NOT is_regulated THEN policy_num('ltv_non_regulated', on_date)
           WHEN seomin THEN policy_num('ltv_regulated_seomin', on_date)
           ELSE policy_num('ltv_regulated', on_date)
         END;
  ltv_loan_limit := floor(price_manwon * ltv);
  cap_loan_limit := CASE
                      WHEN price_manwon <= policy_num('loan_cap_tier1_price_limit', on_date) THEN policy_num('loan_cap_tier1', on_date)
                      WHEN price_manwon <= policy_num('loan_cap_tier2_price_limit', on_date) THEN policy_num('loan_cap_tier2', on_date)
                      ELSE policy_num('loan_cap_tier3', on_date)
                    END;
  cap_loan_limit := LEAST(cap_loan_limit, loan_cap_manwon);
  loan_amount := GREATEST(LEAST(ltv_loan_limit, dsr_loan_limit, cap_loan_limit), 0);
  binding_factor := CASE
                      WHEN loan_amount = 0 THEN 'EQUITY'
                      WHEN loan_amount = ltv_loan_limit THEN 'LTV'
                      WHEN loan_amount = dsr_loan_limit THEN 'DSR'
                      ELSE 'CAP'
                    END;
  RETURN NEXT;
END
$$ LANGUAGE plpgsql STABLE;

-- 최대 매수 가능가: 가격 − 대출 + 취득세 ≤ 자기자본을 만족하는 가장 큰 가격(만원).
-- 가격이 오르면 필요 현금(가격 − 대출 + 취득세)은 줄지 않는다(LTV·상한이 구간 경계에서 낮아져도 늘기만 한다)
-- 그래서 이분 탐색으로 찾는다.
-- binding_factor는 max_price보다 1만원 비싼 집을 못 사게 막는 한도다. 보통은 max_price에서 대출을 정한 한도와 같지만,
-- 구간 경계(서민·실수요자 가격 8억, 대출 상한 15억·25억)에서는 경계를 넘을 때 낮아지는 한도이고
-- binding_detail에 그 경계를 적는다('seomin_price_limit' | 'loan_cap_tier'). 대출이 0이면 'EQUITY'
CREATE FUNCTION max_purchase_price(
  equity_manwon           INT,
  annual_income_manwon    INT,
  is_regulated            BOOLEAN,
  loan_cap_manwon         INT     DEFAULT NULL,   -- 사용자가 따로 두는 대출 상한. NULL이면 정책 상한만
  homeless_household_head BOOLEAN DEFAULT FALSE,  -- 무주택 세대주 (서민·실수요자 요건)
  household_income_manwon INT     DEFAULT NULL,   -- 부부합산 연소득 (서민·실수요자 요건). NULL이면 annual_income_manwon
  area_excl               NUMERIC DEFAULT NULL,   -- 취득세의 농어촌특별세 판정. NULL이면 85m2 이하
  on_date                 DATE    DEFAULT as_of_date()
) RETURNS TABLE (
  max_price       INT,
  loan_amount     INT,
  binding_factor  TEXT,     -- 'LTV' | 'DSR' | 'CAP' | 'EQUITY'
  binding_detail  TEXT,     -- 구간 경계 때문이면 'seomin_price_limit' | 'loan_cap_tier', 아니면 NULL
  ltv             NUMERIC,  -- max_price에 적용한 LTV
  seomin          BOOLEAN,  -- 서민·실수요자 LTV를 적용했는가
  ltv_loan_limit  INT,
  dsr_loan_limit  INT,
  cap_loan_limit  INT,
  acquisition_tax INT,
  cash_needed     INT       -- max_price − loan_amount + acquisition_tax (≤ equity_manwon)
) AS $$
DECLARE
  seomin_ok BOOLEAN;
  lo INT := 0;
  hi INT;
  mid INT;
  t RECORD;
  nxt RECORD;
BEGIN
  IF equity_manwon < 0 OR annual_income_manwon < 0 THEN
    RAISE EXCEPTION '자기자본과 소득은 0 이상이어야 한다';
  END IF;
  seomin_ok := homeless_household_head
               AND COALESCE(household_income_manwon, annual_income_manwon)
                   <= policy_num('seomin_household_income_limit', on_date);
  dsr_loan_limit := public.max_loan_by_dsr(annual_income_manwon, on_date => on_date);

  -- 대출은 DSR 한도와 가장 큰 정책 상한을 넘지 못하므로 hi에서는 필요 현금이 자기자본보다 크다
  hi := equity_manwon + GREATEST(LEAST(dsr_loan_limit, policy_num('loan_cap_tier1', on_date)::int), 0) + 1;
  WHILE hi - lo > 1 LOOP
    mid := (lo + hi) / 2;
    SELECT * INTO t FROM public.purchase_loan_terms(mid, is_regulated, seomin_ok, dsr_loan_limit, loan_cap_manwon, on_date);
    IF mid - t.loan_amount + public.acquisition_tax(mid, area_excl, on_date) <= equity_manwon THEN
      lo := mid;
    ELSE
      hi := mid;
    END IF;
  END LOOP;

  SELECT * INTO t FROM public.purchase_loan_terms(lo, is_regulated, seomin_ok, dsr_loan_limit, loan_cap_manwon, on_date);
  SELECT * INTO nxt FROM public.purchase_loan_terms(lo + 1, is_regulated, seomin_ok, dsr_loan_limit, loan_cap_manwon, on_date);
  max_price := lo;
  loan_amount := t.loan_amount;
  binding_factor := nxt.binding_factor;
  binding_detail := CASE
                      WHEN t.seomin AND NOT nxt.seomin THEN 'seomin_price_limit'
                      WHEN nxt.cap_loan_limit < t.cap_loan_limit THEN 'loan_cap_tier'
                    END;
  ltv := t.ltv;
  seomin := t.seomin;
  ltv_loan_limit := t.ltv_loan_limit;
  cap_loan_limit := t.cap_loan_limit;
  acquisition_tax := public.acquisition_tax(lo, area_excl, on_date);
  cash_needed := max_price - loan_amount + acquisition_tax;
  RETURN NEXT;
END
$$ LANGUAGE plpgsql STABLE;
