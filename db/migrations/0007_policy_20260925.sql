-- 정책 값·규제지역·분석 기준값 초기 적재(2026-09-25 확인).
-- 대책이 바뀌면 이 파일을 고치지 말고 새 시행일 행을 넣는 마이그레이션을 추가한다(과거 값은 남긴다).
-- 출처
--   10·15 대책: 2025-10-15 주택시장 안정화 대책(관계부처 합동), 정책브리핑 korea.kr newsId=148950973,
--               금융위원회 FAQ fsc.go.kr/po020201/85466
--   2026-06-30 발표: 화성 동탄구·용인 기흥구·구리시 추가 지정, 정책브리핑 korea.kr newsId=148967354

INSERT INTO policy_params (param_key, effective_from, value_num, note) VALUES
  ('dsr_ratio',              '2022-07-01', 0.40,  '은행권 차주단위 DSR 40%'),
  ('stress_rate',            '2025-10-16', 0.030, '10·15: 수도권·규제지역 주담대 스트레스 금리 하한 1.5%→3.0%. 변동금리 기준으로 전부 더한다(가정)'),
  ('assumed_mortgage_rate',  '2021-01-01', 0.045, '가정값(SPEC 기본값). 시장 금리 조회값이 아니다'),
  ('loan_years',             '2021-01-01', 30,    '가정값(SPEC 기본값). 원리금균등상환'),
  ('ltv_regulated',          '2025-10-16', 0.40,  '10·15: 규제지역 LTV 40%(무주택자 포함)'),
  ('ltv_regulated_seomin',   '2025-10-16', 0.60,  '10·15: 규제지역 서민·실수요자 LTV 60%(요건: 부부합산 연소득·주택가격·무주택 세대주)'),
  ('ltv_non_regulated',      '2025-10-16', 0.70,  '10·15: 비규제지역 LTV 70% 유지'),
  ('seomin_household_income_limit', '2025-10-16', 9000,  '서민·실수요자: 부부합산 연소득 9천만원 이하'),
  ('seomin_price_limit',     '2025-10-16', 80000, '서민·실수요자: 주택가격 8억원 이하'),
  ('loan_cap_tier1_price_limit', '2025-10-16', 150000, '수도권·규제지역 주담대 상한 구간: 주택가격 15억 이하'),
  ('loan_cap_tier1',         '2025-10-16', 60000, '15억 이하 6억(6억 상한은 6·27 대책부터, 10·15에서 유지)'),
  ('loan_cap_tier2_price_limit', '2025-10-16', 250000, '15억 초과 25억 이하 구간'),
  ('loan_cap_tier2',         '2025-10-16', 40000, '10·15: 15억 초과 25억 이하 4억'),
  ('loan_cap_tier3',         '2025-10-16', 20000, '10·15: 25억 초과 2억'),
  ('acq_tax_low_price_limit',  '2020-01-01', 60000, '1주택 취득세: 6억 이하 1%'),
  ('acq_tax_high_price_limit', '2020-01-01', 90000, '1주택 취득세: 9억 초과 3%, 그 사이는 (가격(억)×2/3−3)%'),
  ('acq_tax_low_rate',       '2020-01-01', 0.01,  '1주택 취득세 하단 세율'),
  ('acq_tax_high_rate',      '2020-01-01', 0.03,  '1주택 취득세 상단 세율'),
  ('edu_tax_ratio',          '2020-01-01', 0.1,   '지방교육세 = 취득세율 × 10%(0.1%~0.3%)'),
  ('rural_tax_rate',         '2020-01-01', 0.002, '농어촌특별세 0.2%'),
  ('rural_tax_area_limit',   '2020-01-01', 85,    '농어촌특별세는 전용 85m2 초과만');

-- 강남·서초·송파·용산: 2023-01-05 전국 해제 때 유지된 지정. 최초 지정일(투기과열 2017-08-03, 조정 2016-11-03)은
-- 이번에 원문으로 확인하지 못했다. 그 밖의 과거 지정·해제 이력(2020~2023)은 넣지 않았다
INSERT INTO regulated_area (lawd_cd5, zone_type, effective_from, effective_to, source_note)
SELECT code, zone, d::date, NULL, '2023-01-05 해제 때 유지(최초 지정일 원문 미확인)'
FROM unnest(ARRAY['11170', '11650', '11680', '11710']) AS code,
     (VALUES ('투기과열지구', '2017-08-03'), ('조정대상지역', '2016-11-03')) AS z (zone, d);

-- 10·15: 서울 나머지 21개 구 + 경기 12곳 투기과열지구·조정대상지역(2025-10-16 효력)
INSERT INTO regulated_area (lawd_cd5, zone_type, effective_from, effective_to, source_note)
SELECT code, zone, '2025-10-16', NULL, '10·15 대책'
FROM unnest(ARRAY[
       '11110', '11140', '11200', '11215', '11230', '11260', '11290', '11305', '11320', '11350', '11380',
       '11410', '11440', '11470', '11500', '11530', '11545', '11560', '11590', '11620', '11740',
       '41290',                    -- 과천시
       '41210',                    -- 광명시
       '41135', '41131', '41133',  -- 성남시 분당·수정·중원구
       '41117', '41111', '41115',  -- 수원시 영통·장안·팔달구
       '41173',                    -- 안양시 동안구
       '41465',                    -- 용인시 수지구
       '41430',                    -- 의왕시
       '41450'                     -- 하남시
     ]) AS code,
     unnest(ARRAY['투기과열지구', '조정대상지역']) AS zone;

-- 2026-06-30 발표: 화성시 동탄구·용인시 기흥구·구리시(2026-07-01 효력)
INSERT INTO regulated_area (lawd_cd5, zone_type, effective_from, effective_to, source_note)
SELECT code, zone, '2026-07-01', NULL, '2026-06-30 국토교통부 추가 지정'
FROM unnest(ARRAY['41597', '41463', '41310']) AS code,
     unnest(ARRAY['투기과열지구', '조정대상지역']) AS zone;

-- 토지거래허가구역(아파트, 실거주 의무). LTV와는 무관하고 갭투자가 막히는지 보는 정보다
INSERT INTO regulated_area (lawd_cd5, zone_type, effective_from, effective_to, source_note)
SELECT lawd_cd5, '토지거래허가구역', '2025-10-20', '2026-12-31',
       '10·15 대책. 강남·서초·송파·용산은 그 전부터 서울시 지정이 있었으나 넣지 않았다'
FROM regulated_area
WHERE zone_type = '투기과열지구' AND effective_from < '2026-01-01';

INSERT INTO regulated_area (lawd_cd5, zone_type, effective_from, effective_to, source_note)
SELECT code, '토지거래허가구역', '2026-07-05', '2027-12-31', '2026-06-30 발표, 경기도 지정'
FROM unnest(ARRAY['41597', '41463', '41310']) AS code;

-- 분석 기준값(SPEC "분석 기준값", 사용자 확인 2026-09-25): 미혼·무주택 세대주라 부부합산 소득 = 본인 소득.
-- 서민·실수요자 요건(부부합산 9천 이하·무주택 세대주)을 충족한다. 생애최초는 아니다
INSERT INTO analysis_profile (
  profile_key, equity_manwon, annual_income_manwon, household_income_manwon,
  homeless_household_head, first_time_buyer, has_car, commute_note,
  min_households, preferred_area_groups, updated_on
) VALUES (
  'default', 30000, 8000, 8000,
  true, false, false, '서울 각지(현장 변동), 정자역 간헐적',
  300, ARRAY['S', 'M'], '2026-09-25'
);
