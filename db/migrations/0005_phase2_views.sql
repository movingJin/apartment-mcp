-- 구현 순서 5단계(Phase 2): 파생 지표 뷰. SPEC.md "Phase 2 — 머티리얼라이즈드 뷰".
--
-- 일반 뷰(v_*)가 계산 로직이고, mv_*는 그 스냅숏이다. MCP 서버는 mv_*만 읽는다.
-- mv_*는 WITH NO DATA로 만들고 `python -m etl.refresh`가 한 트랜잭션에서 모두 채운다.
-- 기간·신고 지연 판정(data_completeness)은 갱신 시점의 as_of_date() 기준이다.
--
-- 공통 규칙
-- - 매매는 v_sale_basis.is_basis(해제·직거래·이상치·분석 범위 밖 단지 제외), 전세는 v_rent_basis.is_jeonse_basis
-- - 중위값을 쓴다. 표본 3건 미만이면 low_confidence
-- - 평형그룹을 섞지 않는다. m2당가는 전용면적 기준(만원/m2)
-- - 최근 6개월 = 기준월 6개월 전 1일 ~ 기준일(기준일 2026-09-25면 2026-03-01 ~ 2026-09-25)

-- 단지 × 평형그룹 × 월 매매 시세
CREATE VIEW v_danji_price_monthly AS
SELECT danji_id, area_group, date_trunc('month', deal_date)::date AS month,
       count(*) AS sample_size,
       count(*) < 3 AS low_confidence,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_manwon))::int AS median_price_manwon,
       min(price_manwon) AS min_price_manwon,
       max(price_manwon) AS max_price_manwon,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_m2))::numeric, 1) AS median_price_per_m2,
       data_completeness_of(date_trunc('month', deal_date)::date) AS data_completeness
FROM v_sale_basis
WHERE is_basis AND deal_date <= as_of_date()
GROUP BY 1, 2, 3;

-- 단지 × 평형그룹 최근 6개월 매매·전세 시세와 전세가율.
-- sample_size·low_confidence는 매매 기준이고 전세는 jeonse_sample_size·jeonse_low_confidence로 따로 둔다.
-- 전세가율 = 전세 m2당 중위 ÷ 매매 m2당 중위 × 100. 같은 평형그룹 안에서도 매매와 전세의 면적 구성이 달라
-- 총액끼리 나누면 어떤 면적이 거래됐는지에 따라 흔들린다
CREATE VIEW v_danji_latest AS
WITH w AS (
  SELECT (date_trunc('month', as_of_date()) - interval '6 months')::date AS period_from, as_of_date() AS period_to
), sale AS (
  SELECT danji_id, area_group, count(*) AS n,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY price_manwon) AS med_price,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_m2) AS med_per_m2,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY area_excl) AS med_area,
         min(price_manwon) AS min_price, max(price_manwon) AS max_price, max(deal_date) AS last_date
  FROM v_sale_basis, w
  WHERE is_basis AND deal_date BETWEEN w.period_from AND w.period_to
  GROUP BY 1, 2
), jeonse AS (
  SELECT danji_id, area_group, count(*) AS n,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY deposit_manwon) AS med_deposit,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY deposit_per_m2) AS med_per_m2,
         max(deal_date) AS last_date
  FROM v_rent_basis, w
  WHERE is_jeonse_basis AND deal_date BETWEEN w.period_from AND w.period_to
  GROUP BY 1, 2
)
SELECT danji_id, area_group, st.lawd_cd5, st.scope,
       w.period_from, w.period_to,
       COALESCE(s.n, 0) AS sample_size,
       COALESCE(s.n, 0) < 3 AS low_confidence,
       round(s.med_price)::int AS median_price_manwon,
       s.min_price AS min_price_manwon,
       s.max_price AS max_price_manwon,
       round(s.med_per_m2::numeric, 1) AS median_price_per_m2,
       round(s.med_area::numeric, 2) AS median_area_excl,
       s.last_date AS last_sale_date,
       COALESCE(j.n, 0) AS jeonse_sample_size,
       COALESCE(j.n, 0) < 3 AS jeonse_low_confidence,
       round(j.med_deposit)::int AS median_jeonse_manwon,
       round(j.med_per_m2::numeric, 1) AS median_jeonse_per_m2,
       j.last_date AS last_jeonse_date,
       round((j.med_per_m2 / s.med_per_m2 * 100)::numeric, 1) AS jeonse_ratio,
       data_completeness_of(date_trunc('month', w.period_to)::date) AS data_completeness
FROM sale s
FULL JOIN jeonse j USING (danji_id, area_group)
CROSS JOIN w
JOIN v_danji_status st USING (danji_id);

-- 시군구 × 평형그룹 × 월. 시군구는 단지 위치 기준이고 토지임대부(danji_only)는 뺀다.
-- trade_count는 거래량(해제 제외, 직거래·이상치 포함), sample_size는 시세 계산에 쓴 거래 수
CREATE VIEW v_region_monthly AS
SELECT lawd_cd5, area_group, date_trunc('month', deal_date)::date AS month,
       count(*) FILTER (WHERE is_basis) AS sample_size,
       count(*) FILTER (WHERE is_basis) < 3 AS low_confidence,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_m2) FILTER (WHERE is_basis))::numeric, 1)
         AS median_price_per_m2,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_manwon) FILTER (WHERE is_basis))::int
         AS median_price_manwon,
       count(DISTINCT danji_id) FILTER (WHERE is_basis) AS danji_count,
       count(*) AS trade_count,
       data_completeness_of(date_trunc('month', deal_date)::date) AS data_completeness
FROM v_sale_basis
WHERE NOT is_canceled AND scope = 'full' AND deal_date <= as_of_date()
GROUP BY 1, 2, 3;

-- 같은 시군구·평형그룹 안에서 최근 6개월 m2당 중위가의 분위(0 = 가장 쌈, 100 = 가장 비쌈).
-- 모집단은 최근 6개월 매매가 있는 scope 'full' 단지다. 표본이 얇은 단지도 들어가고 low_confidence로 표시된다.
-- 모집단이 한 단지뿐이면 분위는 NULL
CREATE VIEW v_danji_percentile AS
SELECT danji_id, area_group, lawd_cd5, period_from, period_to,
       sample_size, low_confidence, median_price_per_m2,
       count(*) OVER g AS group_danji_count,
       CASE WHEN count(*) OVER g > 1
            THEN round((percent_rank() OVER (g ORDER BY median_price_per_m2) * 100)::numeric, 1)
       END AS price_percentile,
       data_completeness
FROM v_danji_latest
WHERE scope = 'full' AND sample_size > 0
WINDOW g AS (PARTITION BY lawd_cd5, area_group);

-- 전고점 대비 회복률. SPEC Phase 2 "전고점 대비 회복률".
-- 분기 중위는 거래 3건 이상인 분기만 쓴다(m2당가).
--   전고점 = 2021Q1~2022Q2 분기 중위 중 최고, 저점 = 2022Q3~2023Q4 분기 중위 중 최저(보조 지표)
--   현재 = v_danji_latest의 최근 6개월 m2당 중위. sample_size·low_confidence는 현재 기준
--   recovery_pct = 현재 ÷ 전고점 × 100, trough_pct = 저점 ÷ 전고점 × 100
-- 시군구 비교(중위·차이·분위)는 scope 'full'이고 회복률이 있는 단지끼리 한다. 차이는 반올림한 두 값의 차다
CREATE VIEW v_danji_recovery AS
WITH q AS (
  SELECT danji_id, area_group, date_trunc('quarter', deal_date)::date AS quarter, count(*) AS n,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_m2) AS med
  FROM v_sale_basis
  WHERE is_basis AND deal_date >= '2021-01-01' AND deal_date < '2024-01-01' AND deal_date <= as_of_date()
  GROUP BY 1, 2, 3
  HAVING count(*) >= 3
), peak AS (
  SELECT DISTINCT ON (danji_id, area_group) danji_id, area_group, quarter, n, med
  FROM q
  WHERE quarter < '2022-07-01'
  ORDER BY danji_id, area_group, med DESC, quarter
), trough AS (
  SELECT DISTINCT ON (danji_id, area_group) danji_id, area_group, quarter, n, med
  FROM q
  WHERE quarter >= '2022-07-01'
  ORDER BY danji_id, area_group, med, quarter
), cur AS (
  SELECT danji_id, area_group, sample_size, median_price_per_m2
  FROM v_danji_latest
  WHERE sample_size > 0
), base AS (
  SELECT danji_id, area_group, st.lawd_cd5, st.scope,
         p.quarter AS peak_quarter, p.n AS peak_sample_size, round(p.med::numeric, 1) AS peak_price_per_m2,
         COALESCE(c.sample_size, 0) AS sample_size,
         COALESCE(c.sample_size, 0) < 3 AS low_confidence,
         c.median_price_per_m2 AS current_price_per_m2,
         t.quarter AS trough_quarter, t.n AS trough_sample_size, round(t.med::numeric, 1) AS trough_price_per_m2,
         round((c.median_price_per_m2 / p.med * 100)::numeric, 1) AS recovery_pct,
         round((t.med / p.med * 100)::numeric, 1) AS trough_pct
  FROM peak p
  FULL JOIN cur c USING (danji_id, area_group)
  LEFT JOIN trough t USING (danji_id, area_group)
  JOIN v_danji_status st USING (danji_id)
  WHERE st.scope <> 'excluded'
), compared AS (
  SELECT *, scope = 'full' AND recovery_pct IS NOT NULL AS in_region FROM base
), region AS (
  SELECT lawd_cd5, area_group, count(*) AS n,
         round((percentile_cont(0.5) WITHIN GROUP (ORDER BY recovery_pct))::numeric, 1) AS med
  FROM compared
  WHERE in_region
  GROUP BY 1, 2
)
SELECT c.danji_id, c.area_group, c.lawd_cd5, c.scope,
       c.peak_quarter, c.peak_sample_size, c.peak_price_per_m2,
       w.period_from AS current_from, w.period_to AS current_to,
       c.sample_size, c.low_confidence, c.current_price_per_m2,
       c.recovery_pct,
       c.trough_quarter, c.trough_sample_size, c.trough_price_per_m2, c.trough_pct,
       r.n AS region_danji_count,
       r.med AS region_median_recovery_pct,
       CASE WHEN c.in_region THEN c.recovery_pct - r.med END AS recovery_vs_region_pp,
       CASE WHEN c.in_region AND r.n > 1
            THEN round((percent_rank() OVER (PARTITION BY c.lawd_cd5, c.area_group, c.in_region
                                             ORDER BY c.recovery_pct) * 100)::numeric, 1)
       END AS recovery_percentile,
       data_completeness_of(date_trunc('month', w.period_to)::date) AS data_completeness
FROM compared c
CROSS JOIN (
  SELECT (date_trunc('month', as_of_date()) - interval '6 months')::date AS period_from, as_of_date() AS period_to
) w
LEFT JOIN region r USING (lawd_cd5, area_group);

-- 스냅숏. 키 인덱스는 UNIQUE로 둬 중복 행이 생기면 갱신이 실패하게 한다
CREATE MATERIALIZED VIEW mv_danji_price_monthly AS SELECT * FROM v_danji_price_monthly WITH NO DATA;
CREATE UNIQUE INDEX ON mv_danji_price_monthly (danji_id, area_group, month);

CREATE MATERIALIZED VIEW mv_danji_latest AS SELECT * FROM v_danji_latest WITH NO DATA;
CREATE UNIQUE INDEX ON mv_danji_latest (danji_id, area_group);
CREATE INDEX ON mv_danji_latest (lawd_cd5, area_group);

CREATE MATERIALIZED VIEW mv_region_monthly AS SELECT * FROM v_region_monthly WITH NO DATA;
CREATE UNIQUE INDEX ON mv_region_monthly (lawd_cd5, area_group, month);

CREATE MATERIALIZED VIEW mv_danji_percentile AS SELECT * FROM v_danji_percentile WITH NO DATA;
CREATE UNIQUE INDEX ON mv_danji_percentile (danji_id, area_group);
CREATE INDEX ON mv_danji_percentile (lawd_cd5, area_group);

CREATE MATERIALIZED VIEW mv_danji_recovery AS SELECT * FROM v_danji_recovery WITH NO DATA;
CREATE UNIQUE INDEX ON mv_danji_recovery (danji_id, area_group);
CREATE INDEX ON mv_danji_recovery (lawd_cd5, area_group);

-- 갱신 이력. MCP 응답의 data_as_of와 기간 기준일은 마지막 행에서 온다
CREATE TABLE mv_refresh_log (
  refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now() PRIMARY KEY,
  as_of        DATE NOT NULL,           -- as_of_date(): 기간과 data_completeness의 기준일
  duration_ms  INT NOT NULL
);
