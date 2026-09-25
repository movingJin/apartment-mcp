-- 실제 API 응답 확인(2026-09-24, 서대문구 2025-06 매매·전월세) 결과를 스키마에 반영한다.
-- 근거와 경위는 SPEC.md "변경 이력" 참조.

-- 1. 평형 그룹: 상한 포함(이하/초과). 국토부·부동산원 규모별 통계와 같은 기준.
--    trade_* .area_group은 이 함수로 계산하는 생성 컬럼이라 ETL이 값을 넣지 않는다.
--    경계를 바꾸면 함수를 고친 뒤 area_group 컬럼을 다시 만들어야 기존 행에 반영된다.
CREATE FUNCTION area_group_of(area_excl NUMERIC) RETURNS TEXT AS $$
  SELECT CASE
    WHEN area_excl <= 40  THEN 'XS'
    WHEN area_excl <= 60  THEN 'S'
    WHEN area_excl <= 85  THEN 'M'
    WHEN area_excl <= 102 THEN 'L'
    ELSE 'XL'
  END
$$ LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE;

-- 2. 전용면적 소수 4자리 보존 (API가 84.9573처럼 4자리까지 준다)
DROP INDEX idx_sale_danji;
ALTER TABLE trade_sale DROP COLUMN area_group;
ALTER TABLE trade_sale ALTER COLUMN area_excl TYPE NUMERIC(8,4);
ALTER TABLE trade_sale
  ADD COLUMN area_group TEXT NOT NULL GENERATED ALWAYS AS (area_group_of(area_excl)) STORED;
CREATE INDEX idx_sale_danji ON trade_sale (danji_id, area_group, deal_date DESC);

ALTER TABLE trade_rent DROP COLUMN area_group;
ALTER TABLE trade_rent ALTER COLUMN area_excl TYPE NUMERIC(8,4);
ALTER TABLE trade_rent
  ADD COLUMN area_group TEXT NOT NULL GENERATED ALWAYS AS (area_group_of(area_excl)) STORED;

-- 3. 단지 식별은 국토부 단지 일련번호(aptSeq)로 한다.
--    지번만으로는 구분이 안 된다: 같은 구 안에서 동이 다르면 지번이 겹치고,
--    한 필지에 여러 단지가 있다(남가좌동 385 = DMC파크뷰자이 1~5단지).
--    지번 키(10자리 법정동코드 + 본번 + 부번)는 건축물대장 조회용으로 둔다.
ALTER TABLE danji DROP CONSTRAINT danji_lawd_cd5_jibun_name_norm_key;
ALTER TABLE danji
  ADD COLUMN apt_seq TEXT NOT NULL UNIQUE,   -- 예: '11410-4479'
  ADD COLUMN lawd_cd CHAR(10),               -- 시군구 5 + 읍면동 5
  ADD COLUMN bonbun  CHAR(4),
  ADD COLUMN bubun   CHAR(4);
CREATE INDEX idx_danji_jibun ON danji (lawd_cd, bonbun, bubun);

-- 4. 매매: 읍면동 코드·지번 코드·단지 일련번호·거래유형 원본 보존
ALTER TABLE trade_sale
  ADD COLUMN lawd_cd      CHAR(10),   -- sggCd + umdCd
  ADD COLUMN bonbun       CHAR(4),
  ADD COLUMN bubun        CHAR(4),
  ADD COLUMN apt_seq      TEXT,
  ADD COLUMN dealing_type TEXT;       -- dealingGbn: '중개거래' | '직거래'

-- 5. 전월세: 응답에 지번 코드·읍면동 코드·해제여부가 없다. 있는 것만 원본 그대로 보존한다.
ALTER TABLE trade_rent
  ADD COLUMN umd_nm             TEXT,     -- 읍면동 이름 (코드는 lawd에서 찾는다)
  ADD COLUMN jibun              TEXT,     -- '345-4' 형식
  ADD COLUMN apt_name           TEXT,
  ADD COLUMN apt_seq            TEXT,
  ADD COLUMN built_year         INT,
  ADD COLUMN contract_term      TEXT,     -- contractTerm 원문 'YY.MM~YY.MM'
  ADD COLUMN renewal_right      TEXT,     -- useRRRight 원문 '사용', 빈값이면 NULL
  ADD COLUMN pre_deposit_manwon INT,      -- 종전 계약 보증금
  ADD COLUMN pre_monthly_manwon INT,      -- 종전 계약 월세
  ADD COLUMN ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now();
