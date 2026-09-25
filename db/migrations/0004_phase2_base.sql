-- 구현 순서 5단계(Phase 2): 기준일, 단지 분석 범위, 정책 테이블, 거래 기준 뷰.
-- 결정과 근거는 SPEC.md "Phase 2"와 변경 이력 2026-09-25 "Phase 2 착수 전 결정" 참조.
--
-- 계산 로직은 일반 뷰(v_*)에 한 번만 두고, 머티리얼라이즈드 뷰(mv_*, 0005)는 그 스냅숏이다.
-- 테스트는 일반 뷰를 단지·시군구로 좁혀 조회하고 기준일을 apartment.as_of로 고정한다.

-- 분석 기준일: 한국 시간 오늘. DB 타임존이 UTC라 current_date는 자정~오전 9시에 하루 어긋난다.
-- 세션 설정 apartment.as_of('YYYY-MM-DD')가 있으면 그 날짜다(테스트·재현용)
CREATE FUNCTION as_of_date() RETURNS DATE AS $$
  SELECT COALESCE(NULLIF(current_setting('apartment.as_of', true), '')::date,
                  (now() AT TIME ZONE 'Asia/Seoul')::date)
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- 신고 지연(정제 규칙 4): 계약월이 기준월 포함 최근 3개월(기준일 2026-09-xx면 07~09월)이면 'partial'.
-- 수집 재수집 창(etl.collect RECENT_MONTHS)과 같은 범위다
CREATE FUNCTION data_completeness_of(month_start DATE) RETURNS TEXT AS $$
  SELECT CASE WHEN month_start >= (date_trunc('month', as_of_date()) - interval '2 months')::date
              THEN 'partial' ELSE 'complete' END
$$ LANGUAGE sql STABLE PARALLEL SAFE;

-- 단지 예외 목록. "시세용 매매가 없는 단지" 규칙(v_danji_status)이 못 잡는 단지를 사람이 확인해 넣는다.
-- 이름은 후보를 찾는 데만 쓰고 판정은 사람이 한다(etl.flags). danji_id는 재적재 때 바뀔 수 있어 aptSeq로 둔다
CREATE TABLE danji_flag (
  apt_seq      TEXT PRIMARY KEY REFERENCES danji (apt_seq),
  flag         TEXT NOT NULL CHECK (flag IN ('rental', 'land_lease', 'sale_conversion')),
  -- sale_conversion: 이 날짜 이전 전월세(분양전환 전 규제 임대료)는 쓰지 않는다. NULL이면 첫 시세용 매매일
  converted_on DATE CHECK (converted_on IS NULL OR flag = 'sale_conversion'),
  note         TEXT NOT NULL,       -- 판정 근거
  reviewed_on  DATE NOT NULL        -- 사람이 확인한 날
);

-- 단지 분석 범위.
--   scope 'full'       : 모든 통계와 후보에 들어간다
--   scope 'danji_only' : 토지임대부. 땅값이 빠진 가격이라 단지 자체 시세만 두고 분위·회복률 비교·지역 통계·후보에서 뺀다
--   scope 'excluded'   : 시세용 매매(해제·직거래 제외)가 한 건도 없거나 임대로 확인된 단지. 모든 통계에서 뺀다
-- 시세용 매매가 없으면 살 수 있는 단지가 아니다(공공·민간임대, 사업자 일괄 직거래 매각). 기간은 DB 전체(대상 범위 5년)
CREATE VIEW v_danji_status AS
WITH s AS (
  SELECT danji_id, count(*) AS market_sale_count, min(deal_date) AS first_market_sale
  FROM trade_sale
  WHERE NOT is_canceled AND dealing_type IS DISTINCT FROM '직거래'
  GROUP BY danji_id
)
SELECT d.danji_id, d.apt_seq, d.lawd_cd5,
       COALESCE(s.market_sale_count, 0) AS market_sale_count,
       s.first_market_sale,
       f.flag, f.note AS flag_note,
       CASE
         WHEN f.flag = 'rental' OR s.danji_id IS NULL THEN 'excluded'
         WHEN f.flag = 'land_lease' THEN 'danji_only'
         ELSE 'full'
       END AS scope,
       CASE
         WHEN f.flag = 'rental' THEN 'rental'
         WHEN s.danji_id IS NULL THEN 'no_market_sale'
         WHEN f.flag = 'land_lease' THEN 'land_lease'
       END AS scope_reason,
       -- 이 날짜 이전 전월세는 쓰지 않는다(분양전환 단지)
       CASE WHEN f.flag = 'sale_conversion' THEN COALESCE(f.converted_on, s.first_market_sale) END AS rent_basis_from
FROM danji d
LEFT JOIN s USING (danji_id)
LEFT JOIN danji_flag f USING (apt_seq);

-- 매매 거래와 판정 플래그. 시세는 is_basis인 거래로만 계산한다.
--   is_direct    : 직거래. 가족 간 거래 등 시세가 아닌 거래가 섞여 시세에서 뺀다(중위 70% 미만 비율이 중개의 18배).
--                  dealing_type이 빈 값(2021년 초 필드 도입 전)이면 구분할 수 없어 포함한다
--   is_low_floor : 층 0 이하(지하). 표시만 하고 빼지 않는다
--   is_outlier   : 같은 단지·평형그룹·연도(해제 제외 거래 5건 이상)의 m2당 중위가 50% 미만 또는 200% 초과
-- lawd_cd5는 거래 행 값이 아니라 단지 위치다(원본에 시군구 코드가 잘못 붙은 행이 있다)
CREATE VIEW v_sale_basis AS
WITH cell AS (
  SELECT danji_id, area_group, date_part('year', deal_date)::int AS deal_year,
         count(*) AS n, percentile_cont(0.5) WITHIN GROUP (ORDER BY price_manwon / area_excl) AS median_per_m2
  FROM trade_sale
  WHERE NOT is_canceled
  GROUP BY 1, 2, 3
), flagged AS (
  SELECT t.id, t.danji_id, st.lawd_cd5, st.scope,
         t.area_excl, t.area_group, t.deal_date, t.price_manwon,
         t.price_manwon / t.area_excl AS price_per_m2,
         t.floor, t.dealing_type, t.is_canceled, t.canceled_date, t.registered_date,
         t.dealing_type IS NOT DISTINCT FROM '직거래' AS is_direct,
         COALESCE(t.floor <= 0, false) AS is_low_floor,
         COALESCE(c.n >= 5 AND (t.price_manwon / t.area_excl < 0.5 * c.median_per_m2
                                OR t.price_manwon / t.area_excl > 2.0 * c.median_per_m2), false) AS is_outlier
  FROM trade_sale t
  JOIN v_danji_status st USING (danji_id)
  LEFT JOIN cell c
    ON c.danji_id = t.danji_id AND c.area_group = t.area_group AND c.deal_year = date_part('year', t.deal_date)::int
)
SELECT *, NOT is_canceled AND NOT is_direct AND NOT is_outlier AND scope <> 'excluded' AS is_basis
FROM flagged;

-- 전월세 거래와 판정 플래그. 전월세 응답에는 해제여부가 없다.
--   is_basis        : 분석 범위 밖 단지(excluded)와 분양전환 전 계약을 뺀 거래
--   is_jeonse_basis : 전세 시세·전세가율용. 월세 0인 전세 중 갱신 계약을 뺀 것(신규 + 빈 값).
--                     갱신 전세금은 같은 단지·평형·월 신규의 중위 91.7%라 섞으면 시세가 낮게 나온다.
--                     2021년은 빈 값이 49%라 그 속의 갱신을 걸러내지 못한다
-- 값이 모두 같은 행(완전 중복)은 동 정보가 없어 다른 세대와 구분할 수 없으므로 그대로 둔다
CREATE VIEW v_rent_basis AS
WITH flagged AS (
  SELECT t.id, t.danji_id, st.lawd_cd5, st.scope,
         t.area_excl, t.area_group, t.deal_date, t.deposit_manwon, t.monthly_manwon,
         t.deposit_manwon / t.area_excl AS deposit_per_m2,
         t.floor, t.contract_type, t.contract_term, t.renewal_right,
         t.monthly_manwon = 0 AS is_jeonse,
         t.contract_type IS NOT DISTINCT FROM '갱신' AS is_renewal,
         COALESCE(t.floor <= 0, false) AS is_low_floor,
         COALESCE(t.deal_date < st.rent_basis_from, false) AS is_before_conversion
  FROM trade_rent t
  JOIN v_danji_status st USING (danji_id)
), based AS (
  SELECT *, scope <> 'excluded' AND NOT is_before_conversion AS is_basis FROM flagged
)
SELECT *, is_basis AND is_jeonse AND NOT is_renewal AS is_jeonse_basis
FROM based;

-- 단지별 조회(get_danji, 회복률 등)가 전월세 전체를 훑지 않게 한다
CREATE INDEX idx_rent_danji ON trade_rent (danji_id, area_group, deal_date DESC);

-- 정책 파라미터. 세율·규제 값은 함수에 하드코딩하지 않고 시행일과 함께 여기 둔다(값은 0007 이후 마이그레이션)
CREATE TABLE policy_params (
  param_key      TEXT NOT NULL,
  effective_from DATE NOT NULL,
  value_num      NUMERIC,
  value_text     TEXT,
  note           TEXT,           -- 근거(발표·시행 문서)
  PRIMARY KEY (param_key, effective_from)
);

-- 규제지역. 수시로 바뀌어 수동으로 갱신한다. effective_to는 마지막 효력일(포함), NULL이면 계속
CREATE TABLE regulated_area (
  lawd_cd5       CHAR(5) NOT NULL,
  zone_type      TEXT NOT NULL CHECK (zone_type IN ('투기과열지구', '조정대상지역', '토지거래허가구역')),
  effective_from DATE NOT NULL,
  effective_to   DATE,
  source_note    TEXT,
  PRIMARY KEY (lawd_cd5, zone_type, effective_from)
);

-- 분석 기준값(SPEC "분석 기준값"). compute_budget의 기본값이다. 대화 때마다 다시 입력하지 않는다
CREATE TABLE analysis_profile (
  profile_key              TEXT PRIMARY KEY,
  equity_manwon            INT NOT NULL,       -- 자기자본
  annual_income_manwon     INT NOT NULL,       -- 차주 연소득 (DSR)
  household_income_manwon  INT NOT NULL,       -- 부부합산 연소득 (서민·실수요자 요건)
  homeless_household_head  BOOLEAN NOT NULL,   -- 무주택 세대주
  first_time_buyer         BOOLEAN NOT NULL,   -- 생애최초 주택구입
  has_car                  BOOLEAN NOT NULL,
  commute_note             TEXT,
  min_households           INT,                -- 후보 필터 기본값
  preferred_area_groups    TEXT[],
  updated_on               DATE NOT NULL
);
