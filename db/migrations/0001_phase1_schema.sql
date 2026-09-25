-- Phase 1 스키마: 법정동·단지 마스터, 실거래(매매·전월세), 수집 이력.
-- SPEC.md "Phase 1 — 스키마"와 동일하게 유지한다. 바꿀 때는 새 마이그레이션 파일을 추가한다.

-- 법정동 마스터
CREATE TABLE lawd (
  lawd_cd      CHAR(10) PRIMARY KEY,
  lawd_cd5     CHAR(5)  NOT NULL,
  sido         TEXT NOT NULL,
  sigungu      TEXT,
  dong         TEXT,
  is_active    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX idx_lawd_cd5 ON lawd (lawd_cd5);

-- 단지 마스터
CREATE TABLE danji (
  danji_id     BIGSERIAL PRIMARY KEY,
  lawd_cd5     CHAR(5) NOT NULL,
  jibun        TEXT NOT NULL,
  name         TEXT NOT NULL,
  name_norm    TEXT NOT NULL,
  kapt_code    TEXT,
  built_year   INT,
  households   INT,
  buildings    INT,
  floors_max   INT,
  far          NUMERIC(6,2),
  bcr          NUMERIC(6,2),
  parking      INT,
  heating      TEXT,
  lat          NUMERIC(10,7),
  lon          NUMERIC(10,7),
  UNIQUE (lawd_cd5, jibun, name_norm)
);

-- 매매 실거래 (deal_date 기준 연도별 RANGE 파티션)
CREATE TABLE trade_sale (
  id             BIGSERIAL,
  danji_id       BIGINT REFERENCES danji(danji_id),
  lawd_cd5       CHAR(5) NOT NULL,
  jibun          TEXT,
  apt_name       TEXT,
  area_excl      NUMERIC(8,3) NOT NULL,   -- 전용면적 m2
  area_group     TEXT NOT NULL,           -- 정제 규칙 "면적 통일" 참조
  deal_date      DATE NOT NULL,
  price_manwon   INT NOT NULL,
  floor          INT,
  built_year     INT,
  is_canceled    BOOLEAN NOT NULL DEFAULT FALSE,
  canceled_date  DATE,
  registered_date DATE,
  src_hash       TEXT NOT NULL,
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (id, deal_date),
  UNIQUE (src_hash, deal_date)
) PARTITION BY RANGE (deal_date);

CREATE INDEX idx_sale_danji ON trade_sale (danji_id, area_group, deal_date DESC);
CREATE INDEX idx_sale_region ON trade_sale (lawd_cd5, deal_date DESC);

-- 전월세 실거래 (동일 파티션 전략)
CREATE TABLE trade_rent (
  id             BIGSERIAL,
  danji_id       BIGINT REFERENCES danji(danji_id),
  lawd_cd5       CHAR(5) NOT NULL,
  area_excl      NUMERIC(8,3) NOT NULL,
  area_group     TEXT NOT NULL,
  deal_date      DATE NOT NULL,
  deposit_manwon INT NOT NULL,
  monthly_manwon INT NOT NULL DEFAULT 0,
  floor          INT,
  contract_type  TEXT,
  src_hash       TEXT NOT NULL,
  PRIMARY KEY (id, deal_date),
  UNIQUE (src_hash, deal_date)
) PARTITION BY RANGE (deal_date);

-- 수집 이력 (재개용)
CREATE TABLE ingest_log (
  kind        TEXT NOT NULL,
  lawd_cd5    CHAR(5) NOT NULL,
  deal_ymd    CHAR(6) NOT NULL,
  fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  row_count   INT,
  status      TEXT NOT NULL,
  error_msg   TEXT,
  PRIMARY KEY (kind, lawd_cd5, deal_ymd)
);

-- 매매·전월세의 연도 파티션을 만든다. 이미 있으면 건너뛴다.
-- DEFAULT 파티션은 두지 않는다: 범위 밖 행은 조용히 쌓이지 말고 적재 시점에 실패해야 한다.
-- 수집 스크립트는 대상 기간의 연도마다 이 함수를 먼저 호출한다.
CREATE FUNCTION ensure_trade_partitions(p_year INT) RETURNS VOID AS $$
DECLARE
  t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['trade_sale', 'trade_rent'] LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
      t || '_' || p_year, t, make_date(p_year, 1, 1), make_date(p_year + 1, 1, 1)
    );
  END LOOP;
END;
$$ LANGUAGE plpgsql;

-- 기본 대상 기간(2021년 ~ 현재) + 내년분
SELECT ensure_trade_partitions(y)
FROM generate_series(2021, EXTRACT(YEAR FROM current_date)::INT + 1) AS y;
