-- 구현 순서 6단계(Phase 3): MCP 서버가 읽는 뷰·함수와 읽기 전용 역할. SPEC.md "Phase 3".
--
-- 서버는 mv_*, 이 파일의 뷰·함수, 마스터 테이블(lawd, danji)만 읽는다. 원시 거래는 get_recent_trades만
-- v_sale_basis·v_rent_basis를 단지로 좁혀 읽는다. 여러 도구가 같은 지표를 내므로 조인과 환산을 여기 한 번만 둔다.
-- 아래 뷰는 mv_* 위에 있어 갱신이 필요 없다(mv_*가 갱신되면 따라간다).

-- 평당가(만원/평). 평 = m2 × 0.3025(정제 규칙 2)이므로 평당가 = m2당가 ÷ 0.3025. 출력용이다
CREATE FUNCTION per_pyeong(per_m2 NUMERIC) RETURNS NUMERIC AS $$
  SELECT round(per_m2 / 0.3025, 1)
$$ LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE;

-- 평형그룹 기준 최대 매수가(search_candidates·compute_budget). max_purchase_price와 같고 농어촌특별세만 평형그룹으로 판정한다.
-- L·XL은 전용 85m2 초과(area_group_of)이고 세금은 85m2 초과 여부만 보므로 L 상한 102를 대표 면적으로 넘긴다.
-- XS·S·M은 85m2 이하라 NULL(85m2 이하)이다. rural_tax_area_limit가 85가 아니게 되면 이 대응이 깨진다(테스트가 잡는다)
CREATE FUNCTION max_purchase_price_for_group(
  p_area_group              TEXT,
  p_equity_manwon           INT,
  p_annual_income_manwon    INT,
  p_is_regulated            BOOLEAN,
  p_homeless_household_head BOOLEAN,
  p_household_income_manwon INT,
  p_on                      DATE DEFAULT as_of_date()
) RETURNS TABLE (
  max_price INT, loan_amount INT, binding_factor TEXT, binding_detail TEXT, ltv NUMERIC, seomin BOOLEAN,
  ltv_loan_limit INT, dsr_loan_limit INT, cap_loan_limit INT, acquisition_tax INT, cash_needed INT
) AS $$
  SELECT * FROM max_purchase_price(
    p_equity_manwon, p_annual_income_manwon, p_is_regulated,
    loan_cap_manwon => NULL,
    homeless_household_head => p_homeless_household_head,
    household_income_manwon => p_household_income_manwon,
    area_excl => CASE WHEN p_area_group IN ('L', 'XL') THEN 102 END,
    on_date => p_on)
$$ LANGUAGE sql STABLE;

-- 기준일에 효력이 있는 규제지역 지정. is_regulated()와 같은 날짜 조건이다(토지거래허가구역 포함)
CREATE VIEW v_regulated_area_current AS
SELECT lawd_cd5, zone_type, effective_from, effective_to, source_note
FROM regulated_area
WHERE effective_from <= as_of_date() AND (effective_to IS NULL OR effective_to >= as_of_date());

-- 단지 × 평형그룹 요약: 최근 6개월 매매·전세(mv_danji_latest) + 분위(mv_danji_percentile) + 회복률(mv_danji_recovery)
-- + 단지 정보. search_candidates·get_danji·compare_danji가 같은 값을 내도록 이 뷰 하나에서 읽는다.
-- 최근 6개월 거래가 없어도 전고점이 있으면 행이 있다(sample_size 0, 시세 NULL).
-- households는 건축물대장 값이고 bldg_danji_cnt가 2 이상이면 필지를 함께 쓰는 단지들의 합계다
CREATE VIEW v_danji_summary AS
SELECT d.danji_id, d.apt_seq, d.name, d.lawd_cd5, lw.sido, lw.sigungu, lw.dong, d.jibun,
       d.built_year, d.households, d.bldg_danji_cnt,
       COALESCE(d.bldg_danji_cnt >= 2, false) AS households_is_parcel_total,
       rg.is_regulated,
       COALESCE(l.scope, r.scope) AS scope,
       area_group,
       COALESCE(l.period_from, r.current_from) AS period_from,
       COALESCE(l.period_to, r.current_to) AS period_to,
       COALESCE(l.sample_size, 0) AS sample_size,
       COALESCE(l.low_confidence, true) AS low_confidence,
       l.median_price_manwon, l.min_price_manwon, l.max_price_manwon,
       l.median_price_per_m2, per_pyeong(l.median_price_per_m2) AS median_price_per_pyeong,
       l.median_area_excl, l.last_sale_date,
       COALESCE(l.jeonse_sample_size, 0) AS jeonse_sample_size,
       COALESCE(l.jeonse_low_confidence, true) AS jeonse_low_confidence,
       l.median_jeonse_manwon, l.median_jeonse_per_m2, l.last_jeonse_date, l.jeonse_ratio,
       p.price_percentile, p.group_danji_count AS percentile_danji_count,
       to_char(r.peak_quarter, 'YYYY"Q"Q') AS peak_quarter, r.peak_sample_size, r.peak_price_per_m2,
       r.recovery_pct, r.region_median_recovery_pct, r.recovery_vs_region_pp, r.recovery_percentile,
       r.region_danji_count AS recovery_danji_count,
       to_char(r.trough_quarter, 'YYYY"Q"Q') AS trough_quarter, r.trough_sample_size, r.trough_pct,
       COALESCE(l.data_completeness, r.data_completeness) AS data_completeness
FROM mv_danji_latest l
FULL JOIN mv_danji_recovery r USING (danji_id, area_group)
JOIN danji d USING (danji_id)
-- 규제 여부는 시군구마다 한 번 계산한다(단지 행마다 부르면 2만 행에 0.5초)
JOIN (SELECT lawd_cd5, is_regulated(lawd_cd5) AS is_regulated FROM (SELECT DISTINCT lawd_cd5 FROM danji) x) rg
  ON rg.lawd_cd5 = d.lawd_cd5
LEFT JOIN lawd lw ON lw.lawd_cd = d.lawd_cd
LEFT JOIN mv_danji_percentile p USING (danji_id, area_group);

-- 시군구 × 평형그룹 단지 분포(get_region_stats). 단지 하나가 값 하나다(거래 수로 가중하지 않는다).
--   가격: mv_danji_percentile 모집단(최근 6개월 매매가 있는 scope 'full' 단지)의 m2당 중위가 분위수
--   회복률: scope 'full'이고 회복률이 있는 단지의 recovery_pct 분위수. p50은 mv_danji_recovery의 시군구 중위와 같다
CREATE VIEW v_region_distribution AS
WITH price AS (
  SELECT lawd_cd5, area_group, min(period_from) AS period_from, max(period_to) AS period_to,
         count(*) AS danji_count,
         count(*) FILTER (WHERE low_confidence) AS low_confidence_danji_count,
         percentile_cont(ARRAY[0.1, 0.25, 0.5, 0.75, 0.9]) WITHIN GROUP (ORDER BY median_price_per_m2) AS q,
         bool_or(data_completeness = 'partial') AS partial
  FROM mv_danji_percentile
  GROUP BY 1, 2
), recovery AS (
  SELECT lawd_cd5, area_group, min(current_from) AS period_from, max(current_to) AS period_to,
         count(*) AS danji_count,
         count(*) FILTER (WHERE low_confidence) AS low_confidence_danji_count,
         percentile_cont(ARRAY[0.1, 0.25, 0.5, 0.75, 0.9]) WITHIN GROUP (ORDER BY recovery_pct) AS q,
         bool_or(data_completeness = 'partial') AS partial
  FROM mv_danji_recovery
  WHERE scope = 'full' AND recovery_pct IS NOT NULL
  GROUP BY 1, 2
)
SELECT lawd_cd5, area_group,
       COALESCE(p.period_from, r.period_from) AS period_from,
       COALESCE(p.period_to, r.period_to) AS period_to,
       COALESCE(p.danji_count, 0) AS price_danji_count,
       COALESCE(p.low_confidence_danji_count, 0) AS price_low_confidence_danji_count,
       round(p.q[1]::numeric, 1) AS price_per_m2_p10,
       round(p.q[2]::numeric, 1) AS price_per_m2_p25,
       round(p.q[3]::numeric, 1) AS price_per_m2_p50,
       round(p.q[4]::numeric, 1) AS price_per_m2_p75,
       round(p.q[5]::numeric, 1) AS price_per_m2_p90,
       per_pyeong(round(p.q[3]::numeric, 1)) AS price_per_pyeong_p50,
       COALESCE(r.danji_count, 0) AS recovery_danji_count,
       COALESCE(r.low_confidence_danji_count, 0) AS recovery_low_confidence_danji_count,
       round(r.q[1]::numeric, 1) AS recovery_pct_p10,
       round(r.q[2]::numeric, 1) AS recovery_pct_p25,
       round(r.q[3]::numeric, 1) AS recovery_pct_p50,
       round(r.q[4]::numeric, 1) AS recovery_pct_p75,
       round(r.q[5]::numeric, 1) AS recovery_pct_p90,
       CASE WHEN COALESCE(p.partial, false) OR COALESCE(r.partial, false) THEN 'partial' ELSE 'complete' END
         AS data_completeness
FROM price p
FULL JOIN recovery r USING (lawd_cd5, area_group);

-- 시군구 × 월 거래량(평형그룹 합계). 평형마다 거래가 한 그룹에만 들어가므로 합이 시군구 거래량이다.
-- mv_region_monthly와 같이 scope 'full' 단지의 해제 제외 매매다(임대·토지임대부 단지 제외)
CREATE VIEW v_region_volume_monthly AS
SELECT lawd_cd5, month,
       sum(trade_count)::int AS trade_count,
       sum(sample_size)::int AS sample_size,
       data_completeness
FROM mv_region_monthly
GROUP BY lawd_cd5, month, data_completeness;

-- MCP 서버용 읽기 전용 역할. 로그인 계정(apartment_mcp 등)은 비밀번호가 있어 마이그레이션에 두지 않고
-- 이 역할의 멤버로 따로 만든다(PROGRESS.md "MCP 서버"). 원시 거래 테이블·수집 이력·건축물대장 원문은 주지 않는다.
-- v_*는 뷰 소유자 권한으로 원시 테이블을 읽는다. 함수는 호출자 권한이라 예산 함수가 읽는 정책 테이블도 준다
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'apartment_reader') THEN
    CREATE ROLE apartment_reader NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO apartment_reader;
GRANT SELECT ON
  lawd, danji, policy_params, regulated_area, analysis_profile, mv_refresh_log,
  mv_danji_price_monthly, mv_danji_latest, mv_region_monthly, mv_danji_percentile, mv_danji_recovery,
  v_danji_status, v_sale_basis, v_rent_basis, v_bldg_missing,
  v_regulated_area_current, v_danji_summary, v_region_distribution, v_region_volume_monthly
TO apartment_reader;
