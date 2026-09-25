-- 구현 순서 4단계: 건축물대장(건축HUB 15134735) 부가정보.
-- 근거와 경위는 SPEC.md "변경 이력" 2026-09-25 4단계 참조.
--
-- 조회 키는 필지(10자리 법정동코드 + 대지구분 + 본번 + 부번)다. 단지(aptSeq)가 아니다.
-- 총괄표제부는 법정동 단위로(지번 없이 조회된다), 표제부는 필지 단위로 받아 원문을 그대로 둔다.
-- 원문 항목은 etl.datagokr.parse_page 형식({소문자 태그: 앞뒤 공백을 뗀 값})의 JSONB다.
-- 파생 규칙(etl.bldg.derive)을 바꾸면 API를 다시 부르지 않고 원문에서 다시 계산한다.

-- 대지구분(건축HUB platGbCd): 지번이 '산'으로 시작하면 1(산), 아니면 0(대지).
-- 매매 응답은 본번·부번 코드만 주고 '산'은 지번 문자열(jibun '산11-244')에만 남는다.
-- 산 지번을 대지로 조회하면 못 찾거나 같은 번호의 다른 대지 건물이 걸린다
CREATE FUNCTION plat_gb_of(jibun TEXT) RETURNS CHAR(1) AS $$
  SELECT CASE WHEN jibun LIKE '산%' THEN '1' ELSE '0' END
$$ LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE;

-- 총괄표제부 원문. 법정동 하나의 응답 전체
CREATE TABLE bldg_recap (
  lawd_cd  CHAR(10) NOT NULL,   -- 조회한 법정동 (sigunguCd + bjdongCd)
  mgm_pk   TEXT     NOT NULL,   -- mgmBldrgstPk
  plat_gb  TEXT     NOT NULL,   -- platGbCd: 0 대지, 1 산, 2 블록
  bonbun   TEXT     NOT NULL,   -- bun '0385'
  bubun    TEXT     NOT NULL,   -- ji '0000'
  item     JSONB    NOT NULL,
  PRIMARY KEY (lawd_cd, mgm_pk)
);
CREATE INDEX idx_bldg_recap_parcel ON bldg_recap (lawd_cd, bonbun, bubun);

-- 법정동별 총괄표제부 수집 이력 (재개용)
CREATE TABLE bldg_recap_log (
  lawd_cd    CHAR(10) PRIMARY KEY,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  row_count  INT,
  status     TEXT NOT NULL,     -- 'ok' | 'error'
  error_msg  TEXT
);

-- 표제부(동 단위) 원문. 필지 하나의 응답 전체. 조회한 키로 저장한다
CREATE TABLE bldg_title (
  lawd_cd  CHAR(10) NOT NULL,
  plat_gb  CHAR(1)  NOT NULL,
  bonbun   CHAR(4)  NOT NULL,
  bubun    CHAR(4)  NOT NULL,
  mgm_pk   TEXT     NOT NULL,
  item     JSONB    NOT NULL,
  PRIMARY KEY (lawd_cd, plat_gb, bonbun, bubun, mgm_pk)
);

-- 필지별 조회 결과와 파생 값. danji 부가정보의 출처다
CREATE TABLE bldg_parcel (
  lawd_cd      CHAR(10) NOT NULL,
  plat_gb      CHAR(1)  NOT NULL,
  bonbun       CHAR(4)  NOT NULL,
  bubun        CHAR(4)  NOT NULL,
  status       TEXT NOT NULL,   -- 'ok' 대장 있음 | 'not_found' 총괄표제부·표제부 모두 0건 | 'error' 마지막 조회 실패
  recap_mgm_pk TEXT,            -- 값을 가져온 총괄표제부. NULL이면 표제부에서 계산했다
  households   INT,
  buildings    INT,             -- 공동주택 주건축물 동 수
  floors_max   INT,
  far          NUMERIC(6,2),    -- 용적률 %
  bcr          NUMERIC(6,2),    -- 건폐율 %
  parking      INT,
  use_apr_date DATE,            -- 사용승인일. 실거래 건축년도와 대조해 엉뚱한 건물을 잡았는지 본다
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  error_msg    TEXT,
  PRIMARY KEY (lawd_cd, plat_gb, bonbun, bubun)
);

-- 건축물대장 값(households 등)을 함께 쓰는 단지 수. 1이면 그 단지 값, 2 이상이면 필지 합계다
-- (남가좌동 385 총괄표제부 4,300세대 = DMC파크뷰자이 1~5단지 + 2단지(임대)).
-- 값이 없으면 NULL. etl.bldg가 bldg_parcel에서 채운다
ALTER TABLE danji ADD COLUMN bldg_danji_cnt INT;

-- 건축물대장 부가정보가 없는 단지와 이유. 조회 실패 리포트 (SPEC 정제 규칙 3)
-- 조회 키 조건(본번 NULL·'0000' 제외)은 etl.bldg.plan_parcels와 같다
CREATE VIEW v_bldg_missing AS
SELECT d.danji_id, d.apt_seq, d.name, d.lawd_cd5, d.lawd_cd, d.bonbun, d.bubun, d.jibun, d.built_year,
       CASE
         WHEN d.lawd_cd IS NULL OR d.bonbun IS NULL OR d.bonbun = '0000' THEN 'no_key'
         WHEN p.status IS NULL THEN 'not_fetched'
         WHEN p.status <> 'ok' THEN p.status          -- not_found | error
         ELSE 'no_households'                         -- 대장은 있으나 세대가 있는 건물이 없음
       END AS reason,
       p.error_msg
FROM danji d
LEFT JOIN bldg_parcel p
  ON (p.lawd_cd, p.plat_gb, p.bonbun, p.bubun) = (d.lawd_cd, plat_gb_of(d.jibun), d.bonbun, d.bubun)
WHERE d.households IS NULL;
