# 부동산 입지 분석 DB & MCP 서버 구축 사양서

Sep 24, 2026 · @Dongjin Lee

> 이 사양은 API를 직접 호출해 보기 전에 세운 계획이다. 2026-09-24 실제 응답을 확인하고 고친 내용과 근거는 맨 끝 [변경 이력](#변경-이력)에 있다. 구현 진행 상황과 다음 작업은 `PROGRESS.md`를 본다.

## 목표와 범위

수도권 아파트를 입지·가격 기준으로 후보 압축하고, 그 근거를 원시 거래 단위까지 추적할 수 있는 시스템을 만든다. 최종 소비자는 Claude이며 사람이 보는 UI는 만들지 않는다.

**대원칙: 저평가·고가치 아파트를 자산으로 매수해 자산을 형성하는 것.** 임대가 아니라 매매가 목적이다. 매수할 수 없거나 자산 가치의 기준이 다른 대상(공공·민간임대 단지, 땅값이 빠진 토지임대부 아파트)이 "싸 보이는 단지"로 후보나 시세에 섞이지 않게 한다. 새 데이터와 기능은 이 목적의 어떤 판단에 쓰이는지로 필요 여부를 정한다.

**핵심 설계 원칙: 숫자는 DB에, 판단은 모델에.**

평당가·전세가율·분위·대출 한도 같은 산술은 전부 SQL 뷰와 함수로 고정한다. 모델은 계산하지 않고 조회한 값을 해석만 한다. 모델이 산술을 시작하면 조용히 틀리고, 틀렸는지 검증할 방법이 없어진다.

### 만드는 것

- 국토부 실거래가 + 단지 기본정보를 적재한 PostgreSQL DB
- 재실행 안전한 증분 ETL 스크립트
- 사전 계산된 파생 지표 뷰와 결정론적 계산 함수
- 타입 있는 도구를 노출하는 MCP 서버

### 만들지 않는 것

- 웹 UI, 대시보드, 차트 렌더링
- 가격 예측 모델 — 표본이 얇아 과적합되고 근거 추적이 불가능해진다
- 민간 플랫폼 크롤링 — 약관 위반
- `execute_sql` 같은 범용 SQL 실행 도구 (Phase 3 참조)

### 대상 범위

서울 전역 + 경기 + 인천, 아파트 매매·전월세, 최근 5년(2021년 상반기 \~ 현재). 범위는 설정 파일로 조정 가능하되 기본값은 이것으로 한다.

5년으로 잡은 이유는 하락장이 포함되기 때문이다. 최근 2년만 보면 전 구간이 상승장이라 단지 간 변별력이 없다. 전고점 대비 회복률은 입지 체력을 보는 가장 좋은 지표인데, 2021년 고점과 2022\~2023년 조정이 들어와야 계산이 된다. 10년까지 가면 2016년 시세가 지금 판단에 쓸모가 없고 표본만 무거워진다.

## 시스템 구성

NAS의 Docker 위에 세 컨테이너를 올린다. ETL은 cron으로 돌고, MCP 서버만 외부에 노출된다.

```mermaid
flowchart LR
  A[공공데이터포털<br/>국토부 API] --> B[ETL<br/>Python]
  B --> C[(PostgreSQL<br/>NAS)]
  C --> D[파생 지표 뷰<br/>계산 함수]
  D --> E[MCP 서버<br/>MCP SDK]
  E --> F[Claude]
```

ETL이 원시 데이터를 적재하고, 뷰가 지표를 계산하고, MCP 서버는 뷰만 읽는다. MCP 서버가 원시 테이블을 직접 집계하지 않게 한다 — 계산 로직이 두 군데로 갈라지면 값이 어긋난다.

### 기술 스택

| 구성요소 | 선택 | 비고 |
| --- | --- | --- |
| DB | PostgreSQL 16 | 파티셔닝, 윈도우 함수 필요 |
| ETL | Python 3.10.18, httpx, tenacity, psycopg3 | 재시도·백오프 필수 |
| MCP | Python, 공식 MCP SDK `mcp` 2.x의 `MCPServer`(1.x의 FastMCP가 이름만 바뀜) | stdio + HTTP 둘 다 지원 |
| 배포 | Docker Compose | NAS 표준 |

### 전송 방식 결정 필요

claude.ai나 모바일 앱에서 붙으려면 MCP 서버가 공개 HTTPS 엔드포인트여야 한다. 선택지는 둘이다.

1. **Cloudflare Tunnel** — NAS 포트 개방 없이 HTTPS 노출. 설정이 가장 간단하고 공유기 설정을 건드리지 않는다. 권장.
2. **DDNS + 리버스 프록시 + Let's Encrypt** — Synology 내장 기능으로 가능하나 포트 개방이 필요하다.

어느 쪽이든 인증을 반드시 건다. 인증 없는 공개 MCP 엔드포인트는 DB 전체를 공개하는 것과 같다.

Claude Code로 개발·검증하는 동안은 stdio 모드로 로컬에서 붙이는 게 빠르다. HTTP 노출은 마지막 단계로 미룬다.

**결정(2026-09-25, 7단계)**: 최종 사용처는 Claude(claude.ai 웹·데스크톱·모바일 커스텀 커넥터)다. MCP 서버는 NAS의 컨테이너(포트 28001)로 올리고 `https://apartment-mcp.movingjin.com`(공인 IP로 풀리는 도메인, 2번 방식)으로 노출한다. 인증은 커넥터의 "로그인 없음 + 요청 헤더"로 고정 토큰을 보낸다(`Authorization: Bearer <MCP_API_TOKEN>`). 커넥터는 사용자 기기가 아니라 Anthropic 클라우드(`160.79.104.0/21`)에서 접속하므로 서버가 공개 인터넷에서 닿아야 한다.

## Phase 1 — 데이터 수집과 적재

### 데이터 소스

모두 공공데이터포털(data.go.kr)에서 활용신청 후 인증키를 발급받는다. 개발단계는 자동승인이라 신청 후 1\~2시간이면 호출할 수 있다.

| 데이터셋 | 포털 ID | 내용 |
| --- | --- | --- |
| 국토교통부\_아파트 매매 실거래가 **상세** 자료 | `15126468` | 단지명, 단지 일련번호(`aptSeq`), 전용면적, 계약일, 거래금액, 층, 동, 건축년도, 해제여부·해제일, 등기일자, 거래유형(중개/직거래), 법정동 시군구·읍면동·본번·부번 코드 |
| 국토교통부\_아파트 전월세 실거래가 자료 | `15126474` | 단지명, 단지 일련번호(`aptSeq`), 보증금, 월세, 계약구분, 계약기간, 갱신요구권 사용여부, 종전 보증금·월세, 읍면동 **이름**, 지번 문자열. 해제여부·지번 코드·읍면동 코드는 **없다** |
| 국토교통부\_건축HUB\_건축물대장정보 서비스 | `15134735` | 세대수, 용적률, 건폐율, 지상·지하층수, 사용승인일, 총주차수 |
| 국토교통부\_공동주택 단지 목록제공 서비스 (K-APT) | `15057332` | 난방방식, 관리 정보. 선택 사항 |
| 행정안전부 법정동코드(행정표준코드) | — | mois.go.kr "행정기관(행정동) 및 관할구역(법정동) 변경내역" 공지의 `jscodeYYYYMMDD(말소코드포함).zip` 안 `KIKcd_B`. 폐지 코드에 말소일자가 있다. 지금 적재본은 2026-07-01 시행분 |

요청주소(호출 확인함, https도 동작)는 다음과 같고 응답은 XML이다.

- 매매 상세: `https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev`
- 전월세: `https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent`
- 건축HUB 총괄표제부: `https://apis.data.go.kr/1613000/BldRgstHubService/getBrRecapTitleInfo`
- 건축HUB 표제부: `https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo`

실거래가 응답 샘플(서대문구 2025-06)이 `tests/fixtures/api/`에, 건축HUB 응답 샘플이 `tests/fixtures/bldg/`에 있다. 건축HUB는 실거래가와 같은 인증키로 호출된다. 정상 `resultCode`가 `00`이고(실거래가는 `000`), 한 페이지 최대 100건이다. 개발계정 일일 한도는 10,000회다.

### API 사용 시 주의

1. **반드시 상세 자료를 쓴다.** 매매는 일반(`15126469`)과 상세(`15126468`)가 따로 있다. 해제여부·등기일자와 지번 코드는 상세에만 있다.
2. **구 엔드포인트 사용 금지.** 실거래가는 `openapi.molit.go.kr/OpenAPI_ToolInstallPackage/...`가 아니라 `apis.data.go.kr/1613000/`을 쓴다. 건축물대장은 `BldRgstService_v2`(`15044713`)가 아니라 건축HUB(`15134735`)로 이관됐다. 블로그 예제 상당수가 구 엔드포인트다.
3. **디코딩 키를 쓴다.** 마이페이지에 인코딩 키와 디코딩 키가 함께 발급된다. Python `requests`의 `params=`로 넘길 때는 디코딩 키여야 한다. 인코딩 키를 넣으면 라이브러리가 `%`를 다시 인코딩해 인증이 깨진다.
4. **건축물대장은 총괄표제부와 표제부를 구분한다.** 세대수·용적률·건폐율·총주차수는 단지 전체 합계인 총괄표제부에서 받는다. 표제부는 동 단위라 대단지를 조회하면 여러 건으로 쪼개진다(남가좌동 385는 102건). 층수는 표제부에서 받는다. 단, 총괄표제부는 동이 여럿인 필지에만 있다(표본 300필지 중 146). 동이 하나인 단지는 표제부 한 건이 전부이므로 표제부에서 계산한다(아래 "건축물대장 부가정보").
8. **건축HUB는 일시 오류가 잦다.** 쉬지 않고 호출하면 503과 **본문이 빈 HTTP 200**이 섞여 오고, 수십 초 동안 이것만 주는 구간도 있다. 빈 200은 0건 응답(`totalCount` 0)과 다르므로 재시도한다. 초당 5회 간격이면 대부분 사라진다.
5. **지번 코드는 4자리 zero-padding이다.** 본번 `0009`, 부번 `0000` 형태로 오고, 건축물대장 호출의 `bun`/`ji`에 같은 형식으로 넣는다. (매매 상세만 해당. 전월세는 `jibun` 문자열 `345-4`만 온다)
6. **응답 형식이 API마다 다르다.** 같은 뜻의 태그도 대소문자가 다르다(매매 `roadNm`, 전월세 `roadnm`). 빈 값은 빈 문자열이나 공백 한 칸(`<aptDong> </aptDong>`)으로 온다. 날짜 필드(`cdealDay`, `rgstDate`)는 `YY.MM.DD`, 계약기간(`contractTerm`)은 `YY.MM~YY.MM`, 전용면적은 소수 0\~4자리(`99`, `84.9573`)다.
7. **페이지를 끝까지 받는다.** `numOfRows=1000`으로 받아도 큰 구는 한 달에 1,000건을 넘는다(2025-06 전월세: 송파 1,523건, 강남 2,033건). `totalCount`를 보고 `pageNo`를 반복한다.

실거래가 API는 `LAWD_CD`(시군구 5자리)와 `DEAL_YMD`(YYYYMM) 조합으로 호출한다. 수집 대상은 API가 실제로 쓰는 코드 기준 수도권 시군구 **83개**(서울 25, 인천 11, 경기 47)이고 `etl/regions.py`에 명시한다. 법정동 마스터에서 뽑지 않는다. 2026년 개편 지역(인천 중구·동구·서구, 화성시)은 국토부가 과거 거래까지 새 코드(제물포구 `28125`, 영종구 `28155`, 서해구 `28275`, 검단구 `28290`, 화성시 만세·효행·병점·동탄구 `41591`\~`41597`)로 다시 매겼고 옛 코드는 0건이다. 법정동 파일이 개편보다 늦으면(data.go.kr CSV 20260813이 그랬다) lawd로 뽑은 목록에서 이 지역이 오류 없이 빠진다. 구가 있는 상위 시(수원시 `41110` 등)도 조회하면 0건이라 구 코드(`41111` 등)로 호출한다. 2021-01\~2026-09는 69개월이므로 83 × 69 = 5,727회에 1,000건을 넘는 구·월의 추가 페이지가 매매 초기 적재 1회분이고, 전월세도 비슷하다. 쿼터는 API별로 따로 잡히므로 각각 개발계정 일일 한도 1만 건 안에 들어간다. 초기 적재는 하루면 끝난다.

### 스키마

아래는 설계를 읽기 위한 요약이다. 실제 DDL은 `db/migrations/*.sql`이 기준이며 `python -m etl.migrate`로 적용한다. 매매·전월세 연도 파티션은 `ensure_trade_partitions(연도)`로 만든다(DEFAULT 파티션 없음).

```sql
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

-- 단지 마스터. 단지 = 국토부 단지 일련번호(aptSeq) 하나 (정제 규칙 3 참조)
CREATE TABLE danji (
  danji_id     BIGSERIAL PRIMARY KEY,
  apt_seq      TEXT NOT NULL UNIQUE,    -- 예: '11410-4479'
  lawd_cd5     CHAR(5) NOT NULL,
  lawd_cd      CHAR(10),                -- 시군구 5 + 읍면동 5. 건축물대장 조회 키
  bonbun       CHAR(4),
  bubun        CHAR(4),
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
  bldg_danji_cnt INT                    -- 건축물대장 값(households~parking)을 함께 쓰는 단지 수. 2 이상이면 필지 합계
);
CREATE INDEX idx_danji_jibun ON danji (lawd_cd, bonbun, bubun);

-- 건축물대장(건축HUB). 원문 JSONB와 필지별 결과. 자세한 컬럼은 0003_bldg_register.sql
-- plat_gb_of(jibun): 대지구분('산' 지번이면 '1', 아니면 '0'),
-- bldg_recap(법정동 단위 총괄표제부 원문), bldg_recap_log(법정동별 수집 이력),
-- bldg_title(필지 단위 표제부 원문), bldg_parcel(필지별 조회 결과와 파생 값),
-- v_bldg_missing(부가정보가 없는 단지와 이유)

-- 전용면적 → 평형 그룹. 상한 포함 (정제 규칙 2 참조)
CREATE FUNCTION area_group_of(area_excl NUMERIC) RETURNS TEXT
  LANGUAGE sql IMMUTABLE STRICT;

-- 매매 실거래 (deal_date 기준 연도별 RANGE 파티션)
CREATE TABLE trade_sale (
  id             BIGSERIAL,
  danji_id       BIGINT REFERENCES danji(danji_id),
  lawd_cd5       CHAR(5) NOT NULL,
  lawd_cd        CHAR(10),                -- sggCd + umdCd
  bonbun         CHAR(4),
  bubun          CHAR(4),
  jibun          TEXT,
  apt_name       TEXT,
  apt_seq        TEXT,
  area_excl      NUMERIC(8,4) NOT NULL,   -- 전용면적 m2 (API가 소수 4자리까지 준다)
  area_group     TEXT NOT NULL GENERATED ALWAYS AS (area_group_of(area_excl)) STORED,
  deal_date      DATE NOT NULL,
  price_manwon   INT NOT NULL,
  floor          INT,
  built_year     INT,
  dealing_type   TEXT,                    -- '중개거래' | '직거래'
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

-- 전월세 실거래 (동일 파티션 전략). 응답에 해제여부·지번 코드·읍면동 코드가 없다
CREATE TABLE trade_rent (
  id             BIGSERIAL,
  danji_id       BIGINT REFERENCES danji(danji_id),
  lawd_cd5       CHAR(5) NOT NULL,
  umd_nm         TEXT,                    -- 읍면동 이름. 코드는 lawd에서 찾는다
  jibun          TEXT,                    -- '345-4' 형식
  apt_name       TEXT,
  apt_seq        TEXT,
  built_year     INT,
  area_excl      NUMERIC(8,4) NOT NULL,
  area_group     TEXT NOT NULL GENERATED ALWAYS AS (area_group_of(area_excl)) STORED,
  deal_date      DATE NOT NULL,
  deposit_manwon INT NOT NULL,
  monthly_manwon INT NOT NULL DEFAULT 0,
  floor          INT,
  contract_type  TEXT,                    -- '신규' | '갱신' | NULL
  contract_term  TEXT,                    -- 원문 'YY.MM~YY.MM'
  renewal_right  TEXT,                    -- 갱신요구권 원문 '사용' | NULL
  pre_deposit_manwon INT,                 -- 종전 계약 보증금
  pre_monthly_manwon INT,                 -- 종전 계약 월세
  src_hash       TEXT NOT NULL,
  ingested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
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

-- Phase 2(0004~0007). 자세한 것은 "Phase 2", 정제 규칙 5~7
-- danji_flag(apt_seq, flag 'rental'|'land_lease'|'sale_conversion', converted_on, note, reviewed_on): 사람이 확인한 예외 목록
-- policy_params, regulated_area, analysis_profile: 정책 값·규제지역·분석 기준값
-- mv_refresh_log(refreshed_at, as_of, duration_ms): 파생 지표 뷰 갱신 이력
-- idx_rent_danji ON trade_rent (danji_id, area_group, deal_date DESC)

-- Phase 3(0008). 자세한 것은 "Phase 3 — 구현"
-- v_danji_summary, v_region_distribution, v_region_volume_monthly, v_regulated_area_current: MCP 도구가 읽는 뷰
-- per_pyeong(per_m2), max_purchase_price_for_group(area_group, …): 평당가 환산, 평형그룹 기준 최대 매수가
-- 역할 apartment_reader: MCP 서버용 읽기 전용 권한
```

### 수집 스크립트 요구사항

1. **멱등성** — (종류, 시군구, 계약월) 단위로 응답 전체와 DB를 동기화한다. 같은 구간을 몇 번 돌려도 중복이 생기지 않고, 응답이 같으면 아무 행도 바뀌지 않아야 한다.
   - `src_hash` = sha256(**저장하는 모든 값** + 같은 구간에서 값이 모두 같은 행 사이의 순번). 값이 모두 같은 행은 DB에서 서로 구분할 수 없어서 순번을 어느 행에 매기든 결과가 같다. 응답 순서가 바뀌어도 해시 집합은 그대로다
   - 구간 하나를 트랜잭션 하나로 처리한다. DB에 없는 해시만 넣고, 응답에서 사라진 해시는 지운다. 값이 바뀐 행(해제, 등기일 추가, 정정)은 옛 행을 지우고 새 행을 넣으므로 그 행만 `id`가 바뀐다. 안 바뀐 행은 `id`가 유지된다
   - 해제된 원 신고와 같은 조건의 재신고는 해제여부·등기일이 달라 둘 다 남는다
   - 지우는 동작이 있으므로 일부만 받은 응답으로 적재하면 안 된다. 받은 건수가 `totalCount`와 다르면 그 구간을 실패로 처리하고, 응답이 0건인데 DB 구간에 행이 있으면 지우지 않고 실패로 남긴다
   - `src_hash` 입력 필드를 바꾸면 모든 해시가 바뀌어 다음 수집 때 해당 구간 행이 전부 다시 들어간다(값은 같고 `id`만 바뀐다)
2. **재개 가능** — `ingest_log`에 성공 기록이 있는 (종류, 지역, 월) 조합은 건너뛴다. `--force` 플래그로 무시 가능.
3. **재수집 창** — 최근 3개월분(현재월 포함. 2026-09에 실행하면 07\~09월)은 성공 기록이 있어도 매번 다시 받는다. 지연 신고와 취소 거래를 반영하기 위함이다. 현재월은 한국 시간 기준이다(DB 타임존은 UTC). 창 길이는 `--recent N`으로 바꾼다. 해제는 계약월 +2개월 안에 77.3%, +11개월 안에 98.9%만 붙으므로, 매일 3개월 창에 더해 주 1회 최근 12개월 매매를 다시 받는다(`--mode incremental --kind sale --recent 12`, 약 1,000회).
4. **속도 제한** — 공공데이터포털은 일일 트래픽 한도가 있다. 초당 요청 수를 설정값으로 두고(기본 5), 429/5xx에 지수 백오프로 재시도한다.
5. **실행 모드** — `--mode full|incremental`, `--region`, `--from`, `--to` 인자를 받는다. full은 `--from`(기본 2021-01)\~`--to`(기본 현재월)를 재개 규칙대로 받고, incremental은 재수집 창만 받는다. `--dry-run`은 받을 구간 수만 보여 준다.
6. **XML 파싱 주의** — 응답이 XML이고 숫자 필드에 쉼표와 공백이 섞여 온다. `"  1,234"` → `1234`로 정규화하는 공용 파서를 둔다. 날짜·빈 값·태그 대소문자는 "API 사용 시 주의" 6번을 따른다.
7. **에러 격리** — 한 (지역, 월)의 실패가 전체를 중단시키지 않는다. 로그에 기록하고 계속 진행, 종료 시 실패 목록을 요약 출력한다. 단, 일일 한도 초과 응답이 오거나 같은 종류가 연속 5구간 실패하면 그 종류(API)의 남은 구간은 호출하지 않는다. 한도가 찬 뒤에도 계속 호출하면 남은 구간이 전부 실패하고 구간마다 재시도 대기만 쌓인다. 다음 실행이 이어서 받는다.

### 건축물대장 부가정보

`python -m etl.bldg`가 건축HUB에서 받아 `danji`의 세대수·동수·최고층·용적률·건폐율·주차수를 채운다(구현 순서 4단계).

1. **조회 키는 필지다.** `danji`의 (10자리 법정동코드, 대지구분, 본번, 부번). 대지구분은 `plat_gb_of(jibun)`로 정한다: 지번이 `산`으로 시작하면 1(산), 아니면 0(대지). 매매 응답은 본번·부번 코드만 주고 `산`은 지번 문자열에만 남아서, 대지로 조회하면 못 찾거나 같은 번호의 다른 대지 건물이 걸린다(매매가 있는 산 지번 단지 37개. 도봉구 쌍문동 `금호2` 산69-1은 대지 0건, 산 6건). 본번이 없거나(택지 블록 `가-240`·`BL-1`, 전월세에만 나온 산 지번) `0000`(지번 미부여)인 단지는 키가 없어 조회하지 않는다
2. **조회 단위** — 총괄표제부는 법정동 단위로 받는다(지번 없이 조회되고, 필지 단위 조회와 결과가 같다). 표제부는 필지 단위로 받는다. 법정동 단위 표제부는 단독주택까지 전부 와서 훨씬 많다. 수도권 전체는 법정동 약 1,080회(+추가 페이지) + 필지 약 20,300회라 개발계정 한도로 2\~3일에 나눠 받는다
3. **원문 보존** — 응답 항목을 JSONB로 그대로 저장한다. 파생 규칙을 바꾸면 API를 다시 부르지 않고 `--rederive`로 다시 계산한다
4. **재개** — 받은 법정동·필지(대장 없음 포함)는 다시 받지 않는다. 실패는 기록하고 다음 실행이 다시 받는다. 일일 한도 초과나 연속 5회 실패면 멈춘다. 실행이 끝날 때마다 모든 단지에 필지 값을 다시 반영하므로, 실거래 수집 뒤에 실행하면 새로 생긴 필지만 받는다
5. **파생 규칙** (`etl.bldg.derive`)
   - 총괄표제부가 있으면 세대수·용적률·건폐율·주차수는 총괄표제부 값. 한 필지에 여럿이면(집합/일반, 신대장/구대장이 함께 있다) 집합 > 일반, 신대장 > 구대장, 세대수 많은 쪽
   - 총괄표제부가 없으면 세대수는 표제부 주거동 세대수 합. 용적률·건폐율·주차수는 주건축물이 1동일 때만 그 표제부 값이다. 여러 동의 표제부 값은 동별 값인 대장과 필지 값을 동마다 반복한 대장이 섞여 있어 합하지 않는다
   - 동수·최고층은 표제부 주거동에서 센다. 주거동은 주용도가 공동주택이고 세대가 있는 주건축물이다(공동주택 용도로 등록된 상가·노유자시설동 제외). 없으면 세대가 있는 주건축물(주상복합·도시형생활주택은 주용도가 업무시설 등)
   - `0`은 값 없음이다. 옛 대장은 용적률·건폐율·주차수를 0으로 둔다
6. **한 필지 여러 단지** — 총괄표제부는 필지 합계 하나다(동아·삼익·풍림 1,620세대가 한 건). 단지별로 나누려면 표제부 건물명을 단지명과 맞춰야 하므로 나누지 않는다. 필지 값을 그 필지의 모든 단지에 넣고 `bldg_danji_cnt`에 단지 수를 적는다. 뷰와 MCP는 이 값이 2 이상이면 필지 합계라고 밝힌다
7. **검증** — 사용승인연도와 실거래 건축년도를 대조한다. 2년 이상 다르면 재건축 전 대장처럼 엉뚱한 건물을 잡았을 수 있어 리포트(`--report`)에 나온다

### cron 구성

- 매일 03:00 — 최근 3개월 증분 수집(`python -m etl.collect --mode incremental`), 이어서 파생 지표 뷰 갱신(`python -m etl.refresh`, 약 2분)
- 매주 일요일 03:30 — 최근 12개월 매매 재수집(`--mode incremental --kind sale --recent 12`), 이어서 `etl.refresh`
- 매주 일요일 04:00 — K-apt 단지 마스터 갱신

## 데이터 정제 규칙

분석 품질의 대부분이 여기서 갈린다. 아래 규칙을 놓치면 숫자가 그럴듯하게 틀린다. 5\~7은 Phase 2에서 실제 데이터로 정했다(변경 이력 "Phase 2 착수 전 결정").

### 1. 취소 거래

국토부 데이터에는 신고 후 해제된 거래가 섞여 있다. 호황기에는 신고가가 취소되는 경우가 잦아서, 이걸 포함하면 고점이 실제보다 높게 잡힌다.

- 응답의 해제여부 필드(`cdealType`)가 `O`면 `is_canceled = TRUE`, 해제일(`cdealDay`)은 `canceled_date`
- **모든 분석 뷰는 `WHERE is_canceled = FALSE`를 기본으로 건다**
- 원본 행은 지우지 않는다. 취소 패턴 자체가 정보다 (수집 동기화는 응답에서 사라졌거나 값이 바뀐 행만 지운다. 해제 행은 응답에 남아 있으므로 그대로 남는다)
- 규모가 작지 않다. 서대문구 2025-06 매매 578건 중 77건(13%)이 해제 거래였다
- 전월세 응답에는 해제여부가 없다. 전월세에는 이 필터를 적용할 수 없다

### 2. 면적 통일

전용면적 기준으로만 계산한다. 공급면적과 섞이면 평당가가 20% 넘게 틀어진다. 평 표기는 출력 시점에만 `m2 × 0.3025`로 환산한다.

평형 그룹은 전용면적 기준으로 고정한다.

| area\_group | 전용면적 범위 | 통칭 |
| --- | --- | --- |
| `XS` | 40m2 이하 | 소형 |
| `S` | 40 초과 \~ 60m2 이하 | 59타입 |
| `M` | 60 초과 \~ 85m2 이하 | 74\~84타입 |
| `L` | 85 초과 \~ 102m2 이하 | 84 초과 |
| `XL` | 102m2 초과 | 대형 |

경계값은 상한에 포함한다. 국토부·부동산원 규모별 통계와 같은 기준이다. 경계에 걸리는 거래가 실제로 있다(서대문구 2025-06에만 전용 60.00 거래 9건). `area_group`은 `area_group_of()`로 계산하는 생성 컬럼이라 ETL이 값을 넣지 않고, 규칙이 한 곳에만 있다. 전용면적은 API 값 그대로 소수 4자리까지 저장한다.

서로 다른 평형의 가격을 절대 섞어 평균내지 않는다. 극동아파트처럼 13평과 54평이 한 단지에 있으면 평균값이 무의미해진다.

### 3. 단지 매칭

단지명으로 매칭하지 않는다. 같은 단지가 `극동`, `극동아파트`, `홍은극동`으로 제각각 들어온다.

- **단지는 국토부 단지 일련번호 `aptSeq`로 식별한다.** 매매·전월세 응답 모두에 있고 값이 같다(서대문구 2025-06 양쪽에 나온 75개 단지 모두 지번·단지명 일치). 전월세는 지번 코드가 없어서 이 값으로만 단지에 연결할 수 있다
- 지번만으로는 단지가 구분되지 않는다. 지번은 읍면동마다 따로 매겨져 같은 구 안에서 겹치고(서대문구 본번 463 = 홍은동 동원베네스트, 홍제동 문화촌현대), 한 필지에 여러 단지가 있다(남가좌동 385 = DMC파크뷰자이 1\~5단지, 북아현동 1013 = e편한세상신촌 1\~4단지)
- 지번 키 `10자리 법정동코드(sggCd + umdCd) + 본번 + 부번`은 건축물대장(`15134735`) 조회에 쓴다. 한 필지에 단지가 여럿이면 총괄표제부는 여러 단지의 합계다(남가좌동 385 총괄표제부는 `DMC파크뷰자이` 하나, 세대수 4,300 = 1\~5단지 합계). 필지 값을 모든 단지에 넣고 `bldg_danji_cnt`로 표시한다("건축물대장 부가정보" 6)
- 단지 위치(`lawd_cd5`, `lawd_cd`, 본번, 부번, 지번)는 그 단지 매매 행의 최빈값으로 정한다. 매매가 없으면 전월세 행의 (읍면동 이름, 지번) 최빈값에서 만든다. 수집 실행이 끝날 때마다 모든 단지를 다시 계산해 적재 순서와 무관하게 같은 값이 나온다. 원본에는 시군구 코드가 잘못 붙은 행이 섞여 있고(관악구 `현대(관악)` 매매 338건 중 1건이 동작구 코드), 기간 중 법정동이 바뀐 단지도 있다(평택 고덕면 → 고덕동)
- 전월세에만 나오는 단지는 읍면동 이름을 `lawd`에서 코드로 바꾸고 지번 문자열을 본번·부번으로 나눠 만든다. 리 단위 지역도 `umdNm`이 `양평읍 양근리`처럼 와서 `lawd.dong`과 형식이 같으므로 (`lawd_cd5`, `dong`)으로 바로 찾는다. 법정동 파일이 행정구역 개편보다 늦으면 새 읍면동을 찾지 못해 `lawd_cd`가 NULL로 남으므로 `lawd`는 행정안전부 최신 코드 파일로 적재한다. 임대분은 별도 aptSeq로 나온다(`DMC파크뷰자이2단지(임대)`)- `name_norm`은 표시용으로만 둔다. 공백·괄호·`아파트`·`(주)` 제거, 전각→반각 변환
- 대단지가 여러 지번에 걸칠 때 aptSeq가 하나인지 여럿인지는 전체 데이터로 확인해야 한다. 여럿이면 좌표와 단지명으로 묶어 하나의 `danji_id`에 매핑하되, 원본 지번·aptSeq는 거래 행에 보존한다(현재 스키마는 단지 하나에 aptSeq 하나라 매핑 테이블이 추가로 필요하다)
  - 전체 수집 결과(2026-09-25): aptSeq 하나는 지번 하나다(원본 시군구 오류 5개 단지 제외). 반대로 같은 법정동·같은 이름·같은 준공연도인데 aptSeq가 여럿인 묶음이 63개(단지 127개) 있고, 일부는 한 단지가 쪼개진 것으로 보인다. 대표 aptSeq가 매매·전월세를 모두 갖고 나머지는 전월세만 있는 형태다(`약수하이츠` `11140-37` 매매 389·전월세 1,548 + `11140-1506` 전월세 89)
  - **매핑 테이블은 만들지 않는다(보류, Phase 2 착수 전 결정).** 영향은 표본이 나뉘는 것이지 값이 틀리는 것이 아니고, 얇아진 표본은 `sample_size`·`low_confidence`로 드러난다. 이름으로 묶으면 단지명 매칭 금지에 어긋난다. 특정 후보에서 문제가 되면 그때 필지 같은 키 근거로 개별 매핑한다
- 건축물대장 조회 실패 행은 버리지 말고 부가정보 `NULL`로 적재한 뒤 별도 리포트로 남긴다. 리포트는 뷰 `v_bldg_missing`(단지와 이유: `no_key`, `not_fetched`, `not_found`, `error`, `no_households`)과 `python -m etl.bldg --report`다

### 4. 신고 지연

실거래 신고는 계약 후 30일 이내이고, 지자체 전산 처리까지 더 걸린다. **최근 2개월 데이터는 미완성이다.**

- 모든 집계 뷰에 `data_completeness` 플래그를 둔다: 계약월이 현재월 기준 2개월 이내면 `partial`
- MCP 응답에 이 플래그를 반드시 실어 보낸다
- 최근 구간을 추세로 해석하면 거래량이 급감한 것처럼 보인다. 이걸 시장 위축으로 오독하지 않게 하는 게 플래그의 목적이다

### 5. 이상치

플래그는 컬럼으로 두지 않고 뷰 `v_sale_basis`에서 계산한다. 규칙이 한 곳에만 있고 원본 행은 바뀌지 않는다.

- 층 정보가 `0` 또는 음수 → `is_low_floor`(지하). 표시만 하고 제외하지 않는다(매매 57건, 전월세 181건, 모두 음수)
- **직거래(`is_direct`)는 시세에서 뺀다.** 가족 간 거래처럼 시세가 아닌 거래가 섞여 단지가 실제보다 싸 보인다. 같은 단지·평형·연도 중위의 70% 미만인 비율이 직거래 4.65%, 중개 0.25%(18배)다. 직거래는 해제 제외 매매의 4.4%(48,276건). `get_recent_trades`에서는 보여 주되 거래유형을 표시한다. `dealing_type`이 빈 값(214,019건, 2021년 초 필드 도입 전)이면 구분할 수 없어 포함한다
- 극단값 → `is_outlier`: 같은 단지·평형그룹·연도(해제 제외 거래 5건 이상)의 **m2당** 중위가 50% 미만 또는 200% 초과. 총액으로 보면 같은 평형그룹 안의 면적 차이(49와 59)가 이상치로 잡힌다. 기본 분석에서 제외하되 조회는 된다. 해제 제외 매매 중 직거래 0.068%, 중개 0.001%, 빈 값 0.024%가 걸린다
- 시세에 쓰는 거래 = `v_sale_basis.is_basis`: 해제·직거래·이상치가 아니고 단지가 분석 범위 안(아래 6)인 매매

### 6. 매수할 수 없는 단지

목적이 매수이므로 살 수 없거나 가치 기준이 다른 단지가 "싸 보이는 단지"로 섞이지 않게 한다. 판정은 뷰 `v_danji_status`의 `scope`다.

| scope | 대상 | 처리 |
| --- | --- | --- |
| `excluded` | 5년간(DB 전체 기간) **시세용 매매(해제·직거래 제외)가 한 건도 없는 단지**, 또는 예외 목록의 `rental` | 모든 통계와 후보에서 뺀다(전월세 포함) |
| `danji_only` | 예외 목록의 `land_lease`(토지임대부) | 단지 자체 시세만 두고 분위·회복률 비교·지역 통계·후보에서 뺀다. 땅값이 빠진 가격이다 |
| `full` | 나머지 | 모든 통계에 들어간다. 예외 목록의 `sale_conversion`은 전환일 이전 전월세만 뺀다 |

- 시세용 매매가 없는 단지는 2,919개(매매 행 자체가 없음 2,535, 직거래만 363, 해제만 21), 전월세 약 61만 행. 공공임대(재개발 임대분, 행복주택·국민임대·영구임대)와 민간임대(뉴스테이), 사업자가 일괄 직거래로 넘긴 청년안심주택 등이다. 이름 규칙은 두 방향으로 틀려서(`김포한강예미지뉴스테이`처럼 이름에 임대가 없는 임대, `LH동분당센트럴파크`처럼 LH가 들어간 분양) 쓰지 않는다
- 이 규칙이 못 잡는 단지는 예외 목록 `danji_flag`(`apt_seq`, 판정, 전환일, 근거, 확인일)에 사람이 확인해 넣는다. 기준 파일은 `db/danji_flag.csv`이고 `python -m etl.flags --load`로 적재한다. 후보는 `--candidates`가 이름 키워드와 거래 패턴으로 뽑지만 판정은 사람이 한다
  - 토지임대부: `강남브리즈힐`, `호반써밋서초파크뷰`(이름에 토지임대부아파트). 빼지 않으면 강남구 M 평형 가격 분위 8.2, 회복률 분위 0으로 "강남의 싼 단지"가 된다
  - 분양전환: 준공 뒤 매매 없이 전월세만 많다가 매매가 시작된 단지(부영 임대, NHF 뉴스테이, LH·공공임대). 전환 전 전월세는 규제된 임대료라 첫 시세용 매매일(또는 `converted_on`) 이전 전월세를 쓰지 않는다. 후보 조건: 준공연도 ≤ 첫 전월세 연도 − 2, 첫 매매가 첫 전월세보다 12개월 이상 늦음, 그 사이 전월세 30건 이상·월 3건 이상. 이보다 적으면 거래절벽기에 매매가 없던 소규모 일반 단지다. 2026-09-25 52곳 적재
- 한계: 같은 aptSeq 안에 섞인 임대 세대, 분양전환 뒤에도 남은 임대 계약은 API로 구분할 수 없다

### 7. 전세 시세

- **갱신 계약은 전세 시세에서 뺀다**(`contract_type`이 `갱신`이 아닌 행 = 신규 + 빈 값). 갱신 전세금은 같은 단지·평형·월 신규의 중위 91.7%(IQR 84.0\~99.8%, 2025\~2026)이고, 2025\~2026 전세의 39\~44%가 갱신이라 섞으면 시세와 전세가율이 낮게 나온다. 빈 값 비율은 2021년 49.2%에서 2026년 2.5%로 줄어서, 2021년 시계열에는 빈 값 속 갱신이 섞여 있다
- **전세는 월세 0인 계약만 쓴다**(반전세 제외)
- 저장 값이 모두 같은 행(완전 중복)은 지우지 않는다. 전월세 API에는 동이 없어 다른 동의 같은 층·같은 날·같은 금액 계약과 구분할 수 없다. 시세는 중위값이라 영향이 작다
- 판정은 뷰 `v_rent_basis`(`is_jeonse_basis`)에 있다

## Phase 2 — 파생 지표와 계산 함수

모델이 계산할 일이 없도록 지표를 미리 만들어둔다. MCP 도구는 이 뷰들만 읽는다. DDL은 `db/migrations/0004`\~`0007`이다.

### 머티리얼라이즈드 뷰

계산 로직은 일반 뷰(`v_*`)에 한 번만 두고, `mv_*`는 그 스냅숏(`SELECT * FROM v_*`)이다. `python -m etl.refresh`가 모든 `mv_*`를 한 트랜잭션에서 다시 계산하고(개발 DB 약 2분) `mv_refresh_log`에 기준일과 소요시간을 남긴다. 테스트는 `v_*`를 단지·시군구로 좁혀 조회한다.

| 뷰 | 키 | 내용 | 갱신 |
| --- | --- | --- | --- |
| `mv_danji_price_monthly` | 단지 × 평형그룹 × 월 | 매매 중위가, 거래건수, 최고가, 최저가, m2당 중위가 | 일 1회 |
| `mv_danji_latest` | 단지 × 평형그룹 | 최근 6개월 매매 중위가·m2당 중위·중위 면적·최근 거래일, 전세 중위·m2당 중위, 전세가율, 매매·전세 표본수, `scope` | 일 1회 |
| `mv_region_monthly` | 시군구 × 평형그룹 × 월 | m2당 중위가, 중위가, 시세 거래 수(`sample_size`), 거래 단지 수, 거래량(`trade_count`) | 일 1회 |
| `mv_danji_percentile` | 단지 × 평형그룹 | 같은 시군구·평형그룹 안 최근 6개월 m2당 중위가의 분위(0 = 가장 쌈 \~ 100), 모집단 단지 수 | 일 1회 |
| `mv_danji_recovery` | 단지 × 평형그룹 | 전고점·현재·회복률, 같은 시군구·평형그룹 회복률 중위와의 차이·분위, 보조로 저점·낙폭 | 일 1회 |

기반 뷰는 셋이다. `v_danji_status`(단지 분석 범위, 정제 규칙 6), `v_sale_basis`(매매 거래와 판정 플래그, 규칙 5), `v_rent_basis`(전월세 거래와 판정 플래그, 규칙 6·7).

공통 규칙:

- 중위값을 쓴다(`percentile_cont(0.5)`). 평균은 대형 평형 한 건에 끌려간다
- 매매는 `is_basis`(해제·직거래·이상치·분석 범위 밖 단지 제외), 전세는 `is_jeonse_basis`(월세 0, 갱신 제외, 분양전환 전 제외)만 쓴다
- 시군구는 거래 행 값이 아니라 단지 위치(`danji.lawd_cd5`)다. 원본에는 시군구 코드가 잘못 붙은 행이 있다
- **기준일** = `as_of_date()`, 한국 시간 오늘(세션 설정 `apartment.as_of`로 고정할 수 있다. 테스트용). **최근 6개월** = 기준월 6개월 전 1일 \~ 기준일(기준일 2026-09-25면 2026-03-01 \~ 2026-09-25)
- **`data_completeness`** = `data_completeness_of(월)`: 계약월이 기준월 포함 최근 3개월(2026-09면 07\~09월)이면 `partial`, 아니면 `complete`. 수집 재수집 창과 같은 범위다. 최근 6개월 창을 쓰는 뷰는 늘 `partial`이다. 값은 갱신 시점 기준이라 매일 갱신한다
- **전세가율** = 전세 m2당 중위 ÷ 매매 m2당 중위 × 100. 총액끼리 나누면 같은 평형그룹 안에서 매매와 전세의 면적 구성이 달라 흔들린다(개발 DB에서 칸의 7.7%가 5%p 이상 어긋나고, `삼정아트테라스정동` XS는 총액 기준 146.9%, m2당 기준 78.0%)
- 분위 모집단은 최근 6개월 매매가 있는 `scope = 'full'` 단지다. 표본이 얇은 단지도 들어가고 `low_confidence`로 표시된다. 모집단이 한 단지뿐이면 분위는 NULL
- 토지임대부(`scope = 'danji_only'`)는 단지 자체 뷰(`price_monthly`, `latest`, `recovery`의 자기 값)에는 있고, 분위·지역 통계·회복률 비교에는 없다

**전고점 대비 회복률** (`mv_danji_recovery`). 하락장을 지나 얼마나 회복했는지로 입지 체력을 본다(대상 범위를 5년으로 잡은 이유). 모델이 시계열을 받아 직접 나누지 않게 뷰에서 계산한다.

- 단위는 단지 × 평형그룹, 값은 **전용 m²당 중위가**다. 같은 평형그룹 안에서도 면적이 섞여(59와 49는 둘 다 `S`) 총액 중위는 어떤 면적이 거래됐는지에 따라 흔들린다
- 거래는 다른 시세 뷰와 같은 규칙으로 고른다(`v_sale_basis.is_basis`)
- **전고점** = 2021Q1\~2022Q2 분기별 중위 중 최고(`peak_quarter`, `peak_sample_size`). 거래 3건 이상인 분기만 쓴다. 단지·평형별 고점 분기의 85%가 이 구간에 있다(2021Q3이 31%로 가장 많다)
- **현재** = 최근 6개월 중위(`mv_danji_latest`와 같은 값·기간). `sample_size`·`low_confidence`는 현재 기준이다
- **회복률** `recovery_pct` = 현재 ÷ 전고점 × 100. 같은 시군구·평형그룹의 회복률 중위(`region_median_recovery_pct`)와의 차이(`recovery_vs_region_pp`, 반올림한 두 값의 차)와 그 안의 분위(`recovery_percentile`)를 같이 둔다. "구 평균보다 낮은 단지"를 모델이 계산하지 않고 고를 수 있게 하려는 것이다
- **저점**은 보조 지표다. 저점 = 2022Q3\~2023Q4 분기 중위 중 최저(3건 이상 분기), `trough_pct` = 저점 ÷ 전고점 × 100. 거래절벽기(2022년 하반기)는 거래가 적어 빠지는 분기가 많아 저점이 덜 정확하다
- 고점 구간에 3건 이상인 분기가 없으면 회복률은 NULL이다. 개발 DB(2026-09-25): 전고점이 있는 단지·평형 11,144개, 현재 시세까지 있어 회복률이 있는 것 10,154개. 회복률 중위 91.6%, `trough_pct` 중위 78.0%. 고점 분기 2021Q3이 35.6%

표본이 얇을 때가 문제다. 모든 집계 뷰는 `sample_size` 컬럼을 반드시 포함하고, \*\*3건 미만이면 `low_confidence = TRUE`\*\*로 표시한다. 구축 단지는 거래가 분기에 한두 건뿐인 경우가 흔하다. `mv_danji_latest`는 매매(`sample_size`)와 전세(`jeonse_sample_size`, `jeonse_low_confidence`)를 따로 둔다.

### 결정론적 계산 함수

대출 한도와 매수 가능가는 SQL 함수로 못 박는다. 이 숫자를 모델이 추정하면 안 된다. 금액은 모두 만원이고, 정책 값은 `policy_params`에서 `on_date`(기본 기준일) 시점 값을 읽는다. 값이 없으면 오류를 낸다(빈 값으로 조용히 계산하지 않는다). 적용 범위는 수도권이다.

```sql
-- 정책 값 조회(on_date에 유효한 가장 최근 값), 규제지역(투기과열지구·조정대상지역) 여부
CREATE FUNCTION policy_num(p_key TEXT, p_on DATE DEFAULT as_of_date()) RETURNS NUMERIC;
CREATE FUNCTION is_regulated(p_lawd_cd5 CHAR(5), p_on DATE DEFAULT as_of_date()) RETURNS BOOLEAN;

-- 스트레스 DSR 적용 최대 대출액. 인자를 비우면 policy_params 값
--   r = (base_rate + stress_rate) / 12, n = years × 12, 월상환한도 = 연소득 × dsr_ratio / 12
--   대출액 = 월상환한도 × (1 − (1+r)^−n) / r (만원 미만 버림)
CREATE FUNCTION max_loan_by_dsr(
  annual_income_manwon INT,
  dsr_ratio NUMERIC DEFAULT NULL, base_rate NUMERIC DEFAULT NULL,
  stress_rate NUMERIC DEFAULT NULL, years INT DEFAULT NULL,
  on_date DATE DEFAULT as_of_date()
) RETURNS INT;

-- 1주택 취득 세금 합계(취득세 + 지방교육세 + 농어촌특별세). area_excl이 NULL이면 85m2 이하로 본다
CREATE FUNCTION acquisition_tax(price_manwon INT, area_excl NUMERIC DEFAULT NULL,
                                on_date DATE DEFAULT as_of_date()) RETURNS INT;

-- 주택가격 하나에서 받을 수 있는 대출과 한도들(LTV·상한 구간 판정)
CREATE FUNCTION purchase_loan_terms(price_manwon INT, is_regulated BOOLEAN, seomin_eligible BOOLEAN,
                                    dsr_loan_limit INT, loan_cap_manwon INT DEFAULT NULL,
                                    on_date DATE DEFAULT as_of_date())
  RETURNS TABLE (ltv NUMERIC, seomin BOOLEAN, ltv_loan_limit INT, cap_loan_limit INT,
                 loan_amount INT, binding_factor TEXT);

-- 최대 매수 가능가: 가격 − 대출 + 취득세 ≤ 자기자본인 가장 큰 가격
CREATE FUNCTION max_purchase_price(
  equity_manwon           INT,
  annual_income_manwon    INT,
  is_regulated            BOOLEAN,
  loan_cap_manwon         INT     DEFAULT NULL,   -- 사용자가 따로 두는 상한. 정책 상한과 둘 중 작은 값
  homeless_household_head BOOLEAN DEFAULT FALSE,  -- 무주택 세대주 (서민·실수요자 요건)
  household_income_manwon INT     DEFAULT NULL,   -- 부부합산 연소득. NULL이면 annual_income_manwon
  area_excl               NUMERIC DEFAULT NULL,   -- 취득세 농어촌특별세 판정
  on_date                 DATE    DEFAULT as_of_date()
) RETURNS TABLE (
  max_price INT, loan_amount INT,
  binding_factor TEXT,   -- 'LTV' | 'DSR' | 'CAP' | 'EQUITY'
  binding_detail TEXT,   -- 구간 경계 때문이면 'seomin_price_limit' | 'loan_cap_tier'
  ltv NUMERIC, seomin BOOLEAN, ltv_loan_limit INT, dsr_loan_limit INT, cap_loan_limit INT,
  acquisition_tax INT, cash_needed INT   -- cash_needed = max_price − loan_amount + acquisition_tax
);
```

- LTV: 비규제 70%. 규제지역 40%, 단 **서민·실수요자**(무주택 세대주, 부부합산 연소득 9천만원 이하, 주택가격 8억원 이하)는 60%
- 대출 상한(수도권·규제지역 주담대): 주택가격 15억 이하 6억, 15억 초과 25억 이하 4억, 25억 초과 2억
- 대출 = min(LTV 한도, DSR 한도, 상한). 가격이 오르면 필요 현금은 줄지 않으므로(LTV·상한이 구간 경계에서 낮아져도 늘기만 한다) 이분 탐색으로 최대 가격을 찾는다
- 취득세율: 6억 이하 1%, 6억 초과 \~ 9억 이하 (가격(억) × 2/3 − 3)%(소수 넷째 자리까지 반올림한 비율), 9억 초과 3%. 지방교육세 = 취득세율 × 10%, 농어촌특별세 = 전용 85m2 초과면 0.2%
- 모형에 없는 것: 기존 부채, 정책대출(디딤돌·보금자리), 생애최초 우대, 고정금리 상품의 스트레스 금리 감면(스트레스 금리를 전부 더해 변동금리 기준으로 보수적이다)

`binding_factor`가 중요하다. 무엇이 예산을 묶고 있는지 알아야 대응이 달라진다. LTV가 묶고 있으면 비규제지역으로 가면 되고, DSR이 묶고 있으면 지역을 옮겨도 소용없다. `binding_factor`는 **`max_price`보다 1만원 비싼 집을 못 사게 막는 한도**다. 보통은 `max_price`에서 대출을 정한 한도와 같다. 그러나 구간 경계에서는 경계를 넘을 때 낮아지는 한도가 되고, `binding_detail`에 그 경계를 적는다. 예: 서민·실수요자가 자기자본 4억·소득 9천이면 8억에서 DSR 한도까지 빌리지만, 8억을 넘으면 LTV가 40%로 떨어진다. 이때 결과는 `LTV`/`seomin_price_limit`이다. 대출이 0이면 `EQUITY`다.

분석 기준값(자기자본 3억, 연소득 8천, 미혼 무주택 세대주, 2026-09-25 정책)으로 계산하면 다음과 같다. 검증 체크리스트의 손계산 대조다.

| 조건 | 최대 매수가 | 대출 | 묶는 것 | 취득세 |
| --- | --- | --- | --- | --- |
| 규제지역, 서민·실수요자 아님 | 49,100 | 19,640 | LTV 40% | 540 |
| 규제지역, 서민·실수요자(기준값) | 67,054 | 38,138 | DSR | 1,084 |
| 비규제지역 | 67,054 | 38,138 | DSR | 1,084 |

세율과 규제 파라미터는 하드코딩하지 말고 `policy_params` 테이블에 시행일과 함께 넣는다. 대책이 나올 때마다 코드를 고치지 않아도 되게 한다. 값이 바뀌면 기존 행을 고치지 않고 새 시행일 행을 넣는 마이그레이션을 추가한다. 지금 값은 `0007_policy_20260925.sql`에 있다. 출처는 10·15 대책(2025-10-15 관계부처 합동, 금융위 FAQ)과 2026-06-30 국토교통부 추가 지정 발표다.

```sql
CREATE TABLE policy_params (
  param_key    TEXT NOT NULL,
  effective_from DATE NOT NULL,
  value_num    NUMERIC,
  value_text   TEXT,
  note         TEXT,
  PRIMARY KEY (param_key, effective_from)
);
```

| param_key | 값 | 시행 | 근거 |
| --- | --- | --- | --- |
| `dsr_ratio` | 0.40 | 2022-07-01 | 은행권 차주단위 DSR |
| `stress_rate` | 0.030 | 2025-10-16 | 10·15: 수도권·규제지역 주담대 스트레스 금리 하한 1.5%→3.0% |
| `assumed_mortgage_rate`, `loan_years` | 0.045, 30 | — | 가정값(SPEC 기본값). 시장 금리 조회값이 아니다 |
| `ltv_regulated`, `ltv_regulated_seomin`, `ltv_non_regulated` | 0.40, 0.60, 0.70 | 2025-10-16 | 10·15 |
| `seomin_household_income_limit`, `seomin_price_limit` | 9,000, 80,000 | 2025-10-16 | 10·15 서민·실수요자 요건 |
| `loan_cap_tier*` | 15억 이하 60,000 / 25억 이하 40,000 / 초과 20,000 | 2025-10-16 | 6억은 6·27 대책부터, 구간은 10·15 |
| `acq_tax_*`, `edu_tax_ratio`, `rural_tax_*` | 6억·9억, 1%·3%, 10%, 0.2%·85m2 | 2020-01-01 | 지방세법 1주택 유상취득 |

### 규제지역 테이블

규제지역 지정은 수시로 바뀌고 예산에 직접 영향을 준다. 별도 테이블로 관리하고 수동 갱신한다. `effective_to`는 마지막 효력일(포함)이다.

```sql
CREATE TABLE regulated_area (
  lawd_cd5     CHAR(5) NOT NULL,
  zone_type    TEXT NOT NULL,   -- '투기과열지구' | '조정대상지역' | '토지거래허가구역'
  effective_from DATE NOT NULL,
  effective_to   DATE,
  source_note  TEXT,
  PRIMARY KEY (lawd_cd5, zone_type, effective_from)
);
```

2026-09-25 현재(서울 25개 구 + 경기 15곳, 모두 수집 대상 83개 안의 코드):

- 투기과열지구·조정대상지역: 강남·서초·송파·용산(2023-01-05 해제 때 유지. 최초 지정일은 원문을 확인하지 못했다), 서울 나머지 21개 구와 경기 12곳(과천, 광명, 성남 분당·수정·중원, 수원 영통·장안·팔달, 안양 동안, 용인 수지, 의왕, 하남. 2025-10-16), 화성 동탄구·용인 기흥구·구리시(2026-07-01)
- 토지거래허가구역(아파트, 실거주 의무): 서울 25개 구 + 경기 12곳 2025-10-20 \~ 2026-12-31, 동탄·기흥·구리 2026-07-05 \~ 2027-12-31. LTV와 무관하고 갭투자가 막히는지 보는 정보다
- 2020\~2023년의 지정·해제 이력은 넣지 않았다. 과거 날짜의 `is_regulated`는 믿지 않는다

## Phase 3 — MCP 서버

### 설계 원칙

**`execute_sql` 같은 범용 도구를 노출하지 않는다.** 문법은 틀리지 않지만 의미가 조용히 틀린다. 공급면적을 전용면적으로 쓰거나, 취소 거래를 포함하거나, 평형을 뭉개는 식이다. 그리고 틀렸는지 알 방법이 없다.

대신 타입 있는 도구로 의미를 고정한다. 도구 하나가 하나의 질문에 대응하고, 정제 규칙은 도구 내부에 박혀 있어야 한다.

### 도구 목록

| 도구 | 파라미터 | 반환 |
| --- | --- | --- |
| `list_regions` | `query` | 시군구(5자리 코드, 이름, 규제지역 여부, 효력 중인 지정 목록)와 이름이 맞는 법정동(10자리 코드와 소속 시군구) |
| `compute_budget` | `equity_manwon`, `annual_income_manwon`, `lawd_cd5`, `household_income_manwon`, `homeless_household_head`, `area_group` (모두 선택, 비우면 분석 기준값) | 최대 매수가, 대출액, `binding_factor`·`binding_detail`과 설명, LTV·DSR·상한 한도, 취득세, 필요 현금, 금리 가정. `lawd_cd5`가 없으면 규제·비규제 두 경우 |
| `search_candidates` | `lawd_cd5[]`, `area_group[]`, `min_households`, `built_after`, `max_price_manwon`(사용자가 더 낮게 두는 상한), 예산 인자(`compute_budget`과 같음), `min_sample_size`, `sort_by`, `limit` | 단지·평형 목록 + 최근 중위가, 평당가, 세대수, 준공년도, 전세가율, 분위, 전고점 대비 회복률과 구 중위 대비 차이, `sample_size`, 적용한 예산과 여유. `meta`에 예산(규제 여부·평형그룹별)과 제외 사유별 수 |
| `find_danji` | `query`, `lawd_cd5`, `limit` | 이름이 맞는 단지 목록 + 주소(시군구·동·지번), 준공, 세대수, `scope` |
| `get_danji` | `danji_id` | 단지 상세(건축물대장, `scope`, 규제지역) + 평형별 최근 시세 + 전세가율 + 분위 + 전고점·회복률 + 최근 매매 10건 |
| `get_price_history` | `danji_id`, `area_group`, `months` | 월별 중위가 시계열(평형그룹별 요약 포함) |
| `get_recent_trades` | `danji_id` 또는 `lawd_cd5`, `kind`(`sale`·`rent`), `area_group`, `since`, `include_canceled`, `limit` | 원시 거래 — 계약일, 층, 전용면적, 금액, 거래유형, 해제여부, 시세 포함 여부(`is_basis` 등 판정 플래그) |
| `get_region_stats` | `lawd_cd5`, `months`, `area_group[]` | m2당가·평당가 추이, 거래량, 분위 분포, 회복률 분포 |
| `compare_danji` | `danji_ids[]`(2\~10), `area_group[]` | 동일 지표 나란히(같은 평형그룹 행끼리) |

`search_candidates`는 예산을 모델에게서 받지 않는다. `compute_budget`과 같은 SQL 함수(`max_purchase_price_for_group`)로 시군구의 규제 여부와 평형그룹마다 최대 매수가를 계산하고, 최근 6개월 중위가가 그 이하인 단지·평형만 돌려준다. 예산을 모델이 어림잡아 넘기지 않게 하려는 것이다. `max_price_manwon`은 사용자가 예산보다 낮은 상한을 원할 때만 쓰고, 둘 중 낮은 값이 적용된다.

- 후보 판정은 가격(최근 6개월 시세용 매매가 `min_sample_size`건 이상) → 예산 → 세대수 순서다. `meta.counts`에 빠진 이유별 수(최근 거래 부족, 예산 초과, 세대수 모름, 세대수 미달)와 후보 수가 있고 합이 검토한 칸 수와 같다
- 세대수(건축물대장)를 모르는 단지는 `min_households`가 0보다 크면 빠지고 그 수를 `meta`에 적는다. `bldg_danji_cnt`가 2 이상이면 `households_is_parcel_total`로 필지 합계임을 표시한다
- 정렬 `sort_by`: `price_percentile`(기본, 같은 시군구·평형그룹 안 가격 분위가 낮은 순), `median_price_desc`(예산 안에서 비싼 순), `recovery_vs_region`(회복률이 구 중위보다 많이 뒤처진 순). 어느 정렬이든 `low_confidence`(표본 3건 미만) 행은 빼지 않고 뒤로 보낸다. 1\~2건짜리 중위가 분위 양 끝을 차지하기 쉽다
- `find_danji`는 사용자가 말한 단지 이름을 `danji_id`로 바꾸는 조회다. 공백·`아파트`·괄호를 빼고(`name_norm`) 부분 일치로 찾아 주소와 함께 모두 돌려주고, 사용자·모델이 주소로 고른다. 데이터를 단지에 연결하는 매칭에는 쓰지 않는다(정제 규칙 3)
- `get_recent_trades`는 해제 거래를 기본으로 빼고(`include_canceled`로 포함), 직거래·이상치는 판정 플래그와 함께 보여 준다. `get_danji`의 최근 6개월 중위가 근거는 `danji_id`, `area_group`, `since` = 기간 시작일로 불러 `is_basis`인 행이다

### 응답 규약

모든 도구의 반환값에 아래를 포함한다. 이게 신뢰성의 핵심이다.

```json
{
  "data": [ ... ],
  "meta": {
    "period": "2026-03-01 ~ 2026-09-24",
    "sample_size": 7,
    "low_confidence": false,
    "data_completeness": "partial",
    "excludes_canceled": true,
    "units": { "price": "만원", "area": "m2" },
    "data_as_of": "2026-09-24"
  }
}
```

- **단위를 값에 붙이지 말고 `meta.units`에 명시** — 억/만원 혼용이 가장 흔한 오독 원인이다
- **집계에는 항상 `sample_size`와 `period`** — 표본 2건짜리 중위가를 시세로 말하면 안 된다
- **`get_recent_trades`로 근거 추적이 가능해야 한다** — 모델이 "이 단지 5.2억"이라고 하면 사용자가 그 거래(계약일·층·금액)를 직접 확인할 수 있어야 한다
- 목록 응답(`search_candidates`, `compare_danji`, 시계열)은 `sample_size`·`low_confidence`·`data_completeness`를 행마다 싣는다. `meta.data_completeness`는 행 중 하나라도 `partial`이면 `partial`이다
- `meta.data_as_of`는 `mv_refresh_log` 마지막 행의 기준일이다. 오늘(한국 시간)보다 이르면 `meta.warning`으로 며칠 전 기준인지 알린다
- `meta.units`는 도구가 돌려주는 값의 단위만 싣는다. `meta.notes`에 해석 주의(표본, 신고 지연, 필지 합계, 분석 범위 밖 단지)를 싣는다
- 도구 결과는 구조화 결과(`structuredContent`)와 같은 내용의 JSON 텍스트다. 텍스트는 들여쓰기 없이, 한글은 이스케이프하지 않는다(들여쓴 JSON보다 30% 안팎 짧다)

### 도구 설명문 작성

각 도구의 description에 **언제 쓰는지와 쓰면 안 되는 경우**를 명시한다. 이게 모델의 도구 선택 정확도를 좌우한다.

예시: `get_danji` — "특정 단지의 상세 정보. 단지를 이미 특정했을 때 사용. 조건으로 단지를 찾을 때는 `search_candidates`를 먼저 호출할 것. 반환되는 시세는 최근 6개월 중위값이며 `sample_size`가 3 미만이면 신뢰하지 말 것."

### 보안

- DB 접속 계정은 **읽기 전용**으로 분리한다. 역할 `apartment_reader`(0008)는 MCP가 읽는 뷰·`mv_*`·마스터·정책 테이블만 SELECT할 수 있고 원시 거래·수집 이력·건축물대장 원문은 못 읽는다. 로그인 계정(`apartment_mcp`)은 이 역할의 멤버이고 비밀번호가 있어 마이그레이션 밖에서 만든다. 계정 기본값으로 `default_transaction_read_only = on`, `statement_timeout = 30s`
- 서버는 `MCP_DATABASE_URL`만 쓴다. 없으면 시작하지 않고 ETL용 `DATABASE_URL`(superuser)로 넘어가지 않는다. 연결마다 읽기 전용·REPEATABLE READ 트랜잭션이라 한 응답 안의 `mv_*`는 같은 스냅숏이다
- 모든 쿼리는 파라미터 바인딩. 문자열 조합 금지. 정렬처럼 SQL 조각을 고르는 곳은 서버에 고정된 목록에서만 고른다
- `limit` 상한을 서버에서 강제한다 (기본 50, 최대 200. 넘으면 200으로 줄인다)
- 인자 오류(없는 단지·시군구 코드 등)는 고칠 방법과 함께 모델에 알린다. 그 밖의 예외는 내용을 숨기고 "Error executing tool"만 돌려준다(SDK 기본 동작)
- HTTP 노출 시 인증 필수. `/mcp`의 모든 요청에 32자 이상 고정 토큰(`MCP_API_TOKEN`)을 요구하고 상수 시간으로 비교한다. 없거나 틀리면 401이다. `/healthz`만 인증 없이 `ok`를 돌려준다(DB를 읽지 않는다). OAuth는 쓰지 않는다: 사용자가 한 명이고 claude.ai 커넥터가 요청 헤더 인증을 지원한다
- HTTP 서버는 Host 헤더 검사(DNS rebinding 방지)를 끈다. 브라우저를 속여 로컬 서버를 부르게 하는 공격을 막는 장치인데, 토큰 없이는 어떤 요청도 통과하지 못하고, 켜 두면 리버스 프록시가 넘기는 Host 값에 따라 정상 요청이 421로 막힌다
- 컨테이너에는 읽기 전용 접속 문자열과 토큰만 환경변수로 넘긴다. `.env`(ETL superuser, 공공데이터 키)는 이미지에도 컨테이너에도 들어가지 않는다(`.dockerignore` 허용 목록, compose `environment`). 컨테이너는 root가 아닌 사용자로 돈다

### 구현

서버는 `mcp_server/`다. `server.py`가 도구 등록과 설명문, `tools.py`가 조회, `db.py`가 접속, `web.py`가 HTTP 앱(토큰 미들웨어, `/healthz`)이다.

- stdio: `python -m mcp_server`. Claude Code가 저장소 루트의 `.mcp.json`으로 띄운다(개발 확인용). 서버는 `.env`에서 `MCP_DATABASE_URL`을 직접 읽는다
- HTTP: `python -m mcp_server --http --host 0.0.0.0 --port 28001`. `mcp_server/Dockerfile`(python 3.10-slim, 의존성은 `mcp_server/requirements.txt`에 고정)로 이미지를 만들고 `docker compose up -d --build mcp`로 띄운다. 배포는 GitHub Actions(`.github/workflows/deploy-prod.yml`)가 `main` push마다 테스트·이미지 확인 → NAS 전송 → 컨테이너 재시작 순서로 한다. 경로는 `/mcp`, stateless + JSON 응답이다. 도구가 서버에서 먼저 보내는 알림이 없어 세션이 필요 없고, 요청마다 독립이라 컨테이너 재시작에도 끊긴 세션이 없으며 리버스 프록시가 스트림을 붙잡지 않는다

여러 도구가 같은 지표를 내므로 조인과 환산은 `db/migrations/0008_mcp_read.sql`에 한 번만 둔다. 모두 `mv_*` 위의 뷰라 따로 갱신하지 않는다.

| 뷰·함수 | 내용 | 쓰는 도구 |
| --- | --- | --- |
| `v_danji_summary` | 단지 × 평형그룹: `mv_danji_latest` + `mv_danji_percentile` + `mv_danji_recovery` + 단지 정보·법정동·규제 여부·평당가 | `search_candidates`, `get_danji`, `compare_danji` |
| `v_region_distribution` | 시군구 × 평형그룹: 단지별 최근 6개월 m2당 중위가와 회복률의 10/25/50/75/90 분위수(단지 하나가 값 하나) | `get_region_stats` |
| `v_region_volume_monthly` | 시군구 × 월 거래량(평형그룹 합계) | `get_region_stats` |
| `v_regulated_area_current` | 기준일에 효력이 있는 규제지역 지정(토지거래허가구역 포함) | 여러 도구 |
| `per_pyeong(per_m2)` | 평당가 = m2당가 ÷ 0.3025 | 여러 도구 |
| `max_purchase_price_for_group(area_group, …)` | `max_purchase_price`에서 농어촌특별세만 평형그룹으로 판정(L·XL은 85m2 초과) | `compute_budget`, `search_candidates` |

원시 거래는 `get_recent_trades`(와 `get_danji`의 최근 거래)만 `v_sale_basis`·`v_rent_basis`에서 읽는다. 단지 하나는 `danji_id =` 조건이라 뷰 안의 단지별 집계까지 조건이 내려가 수십 ms다. 시군구는 단지 id 배열로 걸고 약 1.5초다(뷰 안의 단지·연도 중위 계산은 전체를 돈다).

## 분석 기준값

`analysis_profile` 테이블(행 `default`)에 넣어두고 `compute_budget`의 기본값으로 쓴다. 대화 때마다 다시 입력하지 않기 위함이다.

| 항목 | 값 | 컬럼 |
| --- | --- | --- |
| 자기자본 | 약 30,000만원 | `equity_manwon` |
| 연소득 | 8,000만원 | `annual_income_manwon` |
| 부부합산 연소득 | 8,000만원 (미혼) | `household_income_manwon` |
| 주택 보유 | 무주택 세대주. 생애최초 아님 | `homeless_household_head`, `first_time_buyer` |
| 차량 | 없음 | `has_car` |
| 통근 | 서울 각지 (현장 변동), 정자역 간헐적 | `commute_note` |

무주택 세대주이고 부부합산 소득이 9천만원 이하라 규제지역 8억 이하 주택은 서민·실수요자 LTV 60%가 적용된다(2026-09-25 사용자 확인).

### 필터 기본값

- **역세권 가중** — 차량이 없으므로 도보 접근성이 중요하다. 좌표가 있으면 최근접 역까지 직선거리를 계산해 컬럼으로 둔다. **보류(2026-09-25, 사용자 결정)**: 단지 좌표와 역 좌표를 받을 데이터 출처가 아직 없고 `danji.lat/lon`은 비어 있다
- **최소 세대수** 300 이상 — 거래가 너무 드문 나홀로 단지는 시세 형성이 안 된다(`analysis_profile.min_households`)
- **평형그룹** `S` 또는 `M` 우선(`analysis_profile.preferred_area_groups`)

### 판단 기준 메모

도구가 반환할 값은 아니지만, 후보를 평가할 때 쓰는 기준이다. 문서에 남겨둔다.

싼 단지를 찾는 게 아니라 **싼 이유가 사라질 단지**를 찾는다. 가격이 낮은 이유(비역세권, 언덕, 노후, 교통 소외)가 무엇인지 확인하고, 그 이유를 지울 경로(신설 노선, 정비사업, 용적률 여유)가 있는지 본다. 이유가 구조적으로 남는 곳은 계속 싸다.

## 용량 산정과 NAS 운영

**1TB는 차고 넘친다.** 수도권 5년치는 10GB도 되지 않는다.

| 항목 | 행 수 (추정) | 인덱스 포함 |
| --- | --- | --- |
| 수도권 아파트 매매 5년 | 약 150만 | 약 1GB |
| 수도권 아파트 전월세 5년 | 약 350만 | 약 2.5GB |
| 단지 마스터 | 약 2만 | 50MB 미만 |
| 머티리얼라이즈드 뷰 | — | 약 1GB |
| WAL + 백업 여유 | — | 약 3GB |
| **합계** |  | **약 8GB** |

나중에 10년치로 늘리거나 전국으로 넓혀도 80GB를 넘지 않는다. 용량은 고민할 문제가 아니다.

### 진짜 병목은 메모리와 I/O

Synology 보급형은 RAM이 4\~8GB인 경우가 많다. PostgreSQL 튜닝을 해두면 충분하다.

- `shared_buffers` = RAM의 25%
- `work_mem` = 16\~32MB (동시 접속이 적으므로 넉넉히)
- `maintenance_work_mem` = 256MB (뷰 갱신용)
- `effective_cache_size` = RAM의 50\~75%
- 가능하면 DB 볼륨을 SSD 캐시가 걸린 쪽에 둔다

### 운영

- `pg_dump`로 주 1회 덤프, 별도 볼륨 또는 클라우드에 보관
- 파티션 단위로 오래된 연도를 분리 보관할 수 있게 해두면 나중에 편하다
- 컨테이너 재시작 시 데이터 유실이 없도록 볼륨 마운트를 확인한다

## 구현 순서와 검증

한 단계씩 검증하고 넘어간다. 데이터가 틀린 채로 MCP까지 올라가면 어디서 틀렸는지 못 찾는다.

### 순서

1. **스키마 + 법정동 마스터 적재** — 가장 작고 검증이 쉽다
2. **단일 지역·단일 월 수집** — 서대문구 1개월치로 파서와 정규화를 검증
3. **전체 수집** — 수도권 2021-01\~현재, 매매·전월세 각 약 5,450회 + 추가 페이지. 하루 정도 걸리니 재개 기능을 먼저 확인
4. **건축물대장 부가정보 매칭** — 지번 코드로 조인. 조회 실패율을 측정하고 5% 이하로 낮춘다
5. **파생 뷰 + 계산 함수**
6. **MCP 서버 (stdio)** — Claude Code에서 로컬로 검증
7. **HTTP 노출 + 인증**

### 검증 체크리스트

- [ ] 같은 수집 명령을 두 번 돌려도 행 수가 늘지 않는다 (멱등성)
- [ ] 수집 중단 후 재실행하면 중단 지점부터 이어진다
- [ ] 알고 있는 실거래 1건을 국토부 사이트와 DB에서 대조해 금액·면적·층이 일치한다
- [ ] `is_canceled = TRUE` 행이 실제로 존재하고, 분석 뷰에서 제외된다
- [ ] 평형이 섞인 단지(예: 전용 40m2와 130m2가 함께 있는 단지)의 중위가가 평형그룹별로 분리된다
- [ ] `max_purchase_price`를 손계산과 대조해 일치한다
- [ ] `binding_factor`가 규제지역/비규제지역에서 각각 다르게 나온다
- [ ] 최근 2개월 조회 시 `data_completeness: partial`이 반환된다
- [ ] 표본 2건짜리 단지 조회 시 `low_confidence: true`가 반환된다
- [ ] MCP 도구가 반환한 시세의 근거 거래를 `get_recent_trades`로 확인할 수 있다
- [ ] 해제된 원 신고와 같은 조건의 재신고가 둘 다 행으로 남는다 (`src_hash` 충돌 해결 확인)
- [ ] 한 필지의 여러 단지(남가좌동 385, DMC파크뷰자이 1\~5단지)가 서로 다른 `danji_id`로 들어간다
- [ ] 전월세 행이 `aptSeq`로 단지에 연결된다

## 변경 이력

### 2026-09-24 — Phase 1 착수 중 실측 반영

서대문구(`11410`) 2025-06 매매 상세 578건·전월세 639건을 실제로 받아 확인하고 아래를 고쳤다. 스키마 변경은 `db/migrations/0002_api_verified_fields.sql`.

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 평형 그룹 경계 | `40~60`, `60~85`처럼 경계가 양쪽에 걸림 | 상한 포함(이하/초과), `area_group_of()` 생성 컬럼 | 전용 60.00 거래가 한 달 한 구에 9건 |
| 전용면적 정밀도 | `NUMERIC(8,3)` | `NUMERIC(8,4)` | API가 `84.9573`처럼 소수 4자리까지 줌 |
| 단지 식별 | 시군구코드 + 본번 + 부번 | 국토부 `aptSeq`. 지번 키는 10자리 법정동코드 + 본번 + 부번으로 건축물대장용 | 같은 구에서 동이 다르면 지번이 겹치고, 한 필지에 여러 단지가 있음 |
| 전월세 컬럼 | 보증금·월세·계약구분만 | 지번·단지명·`aptSeq`·읍면동 이름·건축년도·계약기간·갱신요구권·종전 보증금/월세 추가. 해제여부는 API에 없어 두지 않음 | 전월세 응답 필드 확인 |
| 매매 컬럼 | — | 10자리 법정동코드·본번·부번·`aptSeq`·거래유형 추가 | 건축물대장 조회와 이상치 판정에 필요 |
| 호출 수 | 시군구 약 70개 × 60개월 | 79개(상위 시 7개 제외) × 69개월 + 추가 페이지 | 법정동 마스터 집계, 상위 시 코드는 0건, 큰 구는 월 1,000건 초과 |
| `src_hash` | 7개 필드 조합 | 미결 (수집 단계에서 결정) | 매매 578건 중 61쌍 충돌 |

### 2026-09-25 — 구현 순서 2단계(단일 지역·단일 월 수집) 착수 중 결정

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| `src_hash`와 재수집 | 7개 필드 해시로 UPSERT | 저장하는 모든 값 + 같은 값인 행 사이 순번으로 해시. (종류, 시군구, 월) 단위로 새 해시는 넣고 사라진 해시는 지우는 동기화 (수집 요구사항 1) | 7개 필드로는 매매 61그룹 125행이 겹친다. 모든 저장 값이 같은 행은 매매 1쌍(독립문삼호, 둘 다 해제)·전월세 30그룹 61행뿐이고, 이 행들은 DB에서 구분할 수 없어 순번 배정이 결과에 영향이 없다. 순번+UPSERT(원안 a)는 충돌 그룹 안에서 행끼리 속성이 뒤바뀔 수 있고 정정된 옛 행이 남는다. 구간 삭제 후 재삽입(원안 b)은 재수집 창의 `id`가 매일 전부 바뀐다. 사용자 승인 |
| 전월세 읍면동 이름 → 코드 | 리 단위 형식 미확인 | `umdNm`이 `양평읍 양근리` 형식이라 `lawd.dong`과 그대로 대응 | 양평군(`41830`) 2025-06 전월세 84건의 `umdNm` 8종 모두 `읍면 리` 형식, `lawd`에서 각각 활성 코드 1개. 같은 달 매매의 `sggCd`+`umdCd`와도 일치(양근리 `4183025021`) |

### 2026-09-25 — 구현 순서 3단계(전체 수집) 착수 중 결정

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 수집 대상 시군구 | 법정동 마스터 기준 79개(서울 25, 인천 10, 경기 44) | API가 쓰는 코드 83개(서울 25, 인천 11, 경기 47)를 `etl/regions.py`에 명시. 옛 코드 4개(인천 중구 `28110`·동구 `28140`·서구 `28260`, 화성시 `41590`) 대신 새 코드 8개. 사용자 승인 | 옛 코드 4개는 매매 2021-06·2025-06 모두 0건. 새 코드는 두 달 모두 있음(2021-06/2025-06): `28125` 202/89, `28155` 124/146, `28275` 469/420, `28290` 189/339, `41591` 174/147, `41593` 134/194, `41595` 266/262, `41597` 549/946. 개편 전 거래까지 새 코드로 다시 매겨져 있다. 부천도 같다(2021-06 `41192` 323건, `41190` 0건). 법정동코드 파일(20260813)에는 새 코드가 없어 lawd 기준이면 이 지역이 오류 없이 0건으로 빠진다. 나머지 75개는 두 달 모두 데이터가 있다(옹진군만 0건) |
| 재수집 창 | 최근 3개월 | 현재월 포함 3개월, 한국 시간 기준 (명확화) | `data_completeness`의 "현재월 기준 2개월 이내"와 같은 범위. DB 타임존이 UTC라 DB 날짜를 쓰면 자정\~오전 9시에 한 달 어긋난다 |
| 에러 격리 | 모든 실패를 격리하고 계속 | 일일 한도 초과 또는 같은 종류 연속 5구간 실패면 그 종류만 멈춤 | 한도가 차면 남은 구간이 전부 실패한다. 한도 초과 응답은 아직 못 봐서 공통 오류 코드 22를 찾고, 형식이 다르면 연속 실패로 멈춘다 |
| 법정동 마스터 원본 | code.go.kr 전체자료 (실제 적재는 data.go.kr CSV 20260813) | 행정안전부 주소코드 공지의 `KIKcd_B` 20260701(말소코드포함). `etl.lawd`가 이 고정폭 형식도 읽는다 | data.go.kr CSV에는 2026 개편 코드가 없었다. 행안부 파일 적재로 3,553행 추가, 3,591행 갱신(대부분 폐지 표시: 인천 중구·동구·서구, 전라남도·광주광역시). 새 코드 8개 지역의 2025-06 매매 읍면동 코드·이름과 전월세 읍면동 이름이 모두 lawd와 일치. 수도권 활성 시군구에서 구가 있는 시 8개와 하위 동 없는 출장소 3개(`28115`, `28116`, `28265`)를 빼면 `etl/regions.py` 83개와 코드·이름이 같다. `28275`는 서구가 아니라 서해구다(처음엔 API 응답만 보고 서구로 추정했다) |
| 단지 위치 | 명시 없음 (2단계 구현: 시군구는 처음 넣은 값, 지번 코드는 마지막으로 적재한 매매 값) | 매매 행 최빈값(매매가 없으면 전월세), 수집 실행 끝에 모든 단지 재계산. 사용자 승인 | 적재 순서에 따라 9개 단지 위치가 틀렸다. 원본 시군구 코드 오류 5개(`현대(관악)`이 동작구로, `한진해모로` 법정동코드가 없는 조합 `1120016200`으로 들어가는 식)와 기간 중 법정동이 바뀐 전월세 전용 단지 4개(강일동→상일동, 고덕면→고덕동, 목동동→동패동). 재계산 뒤 모든 단지의 시군구가 매매 다수 쪽과 같고 `lawd_cd`가 전부 lawd에 있다 |
| aptSeq와 지번 | 전체 데이터로 확인 | 확인 결과를 정제 규칙 3에 적음. 매핑 테이블은 Phase 2 전 결정 | 매매 aptSeq 18,189개 중 지번이 둘 이상인 것은 원본 오류 5개뿐. 이름이 둘 이상인 것은 0 |

### 2026-09-25 — 구현 순서 4단계(건축물대장 부가정보) 착수 중 결정

건축HUB를 실제로 호출해 확인했다. 무작위 300필지 표본은 총괄표제부·표제부를 모두 조회했고, 서대문구 전체(17개 법정동, 329필지)는 `etl.bldg`로 시험 실행했다. 스키마는 `db/migrations/0003_bldg_register.sql`. 사용자 승인.

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 부가정보 출처 | 세대수·용적률·건폐율·총주차수는 총괄표제부, 층수만 표제부 (주의 4) | 총괄표제부가 없으면 표제부에서 계산. 용적률·건폐율·주차수는 주건축물이 1동일 때만. `0`은 NULL ("건축물대장 부가정보" 5) | 표본 300필지 중 총괄표제부가 있는 곳은 146. 138은 동이 하나라 표제부 한 건뿐, 13은 주상복합(주용도 업무시설), 3은 대장 없음. 총괄표제부 없이 여러 동이면 표제부 용적률이 동별 값인 대장(현대 34.71, 18.72, …)과 필지 값을 반복한 대장(성창 122.45 ×3)이 섞여 있다. 옛 대장은 용적률·건폐율·주차가 0. 대장을 찾은 297필지에서 세대수·동수·최고층 100%, 용적률·건폐율 88%, 주차 84% |
| 총괄표제부가 여럿 | 명시 없음 | 집합 > 일반, 신대장 > 구대장, 세대수 많은 쪽 | 표본 6필지에 2건씩(예: DMC마포청구 일반 구대장 441세대 + 집합 신대장 420세대. 표제부 공동주택 합계는 420) |
| 한 필지 여러 단지 | 세대수 등을 붙일 때 확인 | 필지 값을 모든 단지에 넣고 `danji.bldg_danji_cnt`에 단지 수. 뷰·MCP는 2 이상이면 필지 합계라고 밝힌다 | 310필지·688단지(3.3%). 총괄표제부는 필지 합계 한 건이다(동아·삼익·풍림 1,620세대, 주공8·9단지 2,120세대). 나누려면 표제부 건물명과 단지명을 맞춰야 한다(단지명 매칭 금지). NULL로 두면 DMC파크뷰자이 같은 대단지가 세대수 필터에서 빠진다 |
| 조회 단위 | 명시 없음 | 총괄표제부는 법정동 단위, 표제부는 필지 단위. 수도권 약 1,080 + 20,300회 | 총괄표제부는 지번 없이 법정동으로 조회된다. 52개 법정동에서 필지 단위 조회와 `mgmBldrgstPk`가 모두 같았다(불일치 0). 법정동당 18~1,285건. 표제부는 법정동 단위면 단독주택까지 와서 더 많다 |
| 원문 보존 | 명시 없음 | 응답 항목 JSONB 저장, `--rederive`로 재계산 | 다시 받으려면 개발계정 한도(10,000회/일)로 2~3일 걸린다. 서대문구 표제부 1,526건 2.7MB, 전체 약 300MB 추정 |
| 조회 키 | 10자리 법정동코드 + 본번 + 부번 | 대지구분(`plat_gb_of(jibun)`) 추가. 본번 `0000`도 키 없음 | 매매가 있는 산 지번 단지 37개를 대지로 조회하고 있었다. 산으로 조회하면 4개 중 3개를 찾았고 사용승인연도가 실거래 건축년도와 같다(금호2 1991, 신동아 1994, 수봉아파트B동 1980). 본번 `0000`은 42단지(평택 화양 신축 5개 단지가 한 키 등) |
| 재시도 | 429/5xx 지수 백오프 6회 | 본문이 빈 HTTP 200도 재시도. 건축HUB는 8회(대기 합계 약 2분). 재시도마다 stderr에 사유 출력 | 쉬지 않고 40회 호출하면 503 6회·빈 200 4회. 초당 5회면 60회 모두 정상. 수십 초 동안 빈 200·503만 주는 구간이 있어 6회(약 30초)로는 넘기지 못했다 |
| 결과(서대문구) | 조회 실패율 5% 이하 | 342단지 중 336 채움, 없음 6(1.8%): 대장 없음 5, 세대 없음 1 | 대장 없음: 재건축·신축 뒤 대장이 다른 지번에 있는 경우 등(북한산두산위브2차 2021, 서대문센트럴아이파크 2025, 이랜드PEERDMC 2026, 서소문 1971, 세한숲속마을 산11-244). 세대 없음: 도시형생활주택이라 세대수 0·호수 89(연희웨스트팰리스). 호수는 총괄표제부에서 상가 호수로도 쓰여(DMC파크뷰자이 152) 세대수로 대신 쓰지 않았다. 표본 294필지와 서대문구 대부분에서 사용승인연도 = 실거래 건축년도 |

### 2026-09-25 — 목적 명시

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 목표 | 입지·가격 기준 후보 압축 | "목표와 범위"에 대원칙 추가: 저평가·고가치 아파트를 자산으로 매수해 자산 형성. 임대 등 매수 불가·가치 기준이 다른 대상은 후보·시세에서 뺀다 | 사용자 지시. 적용 방법(임대 단지·토지임대부 판별 등)은 Phase 2 전 결정 사항으로 `PROGRESS.md`에 권장안과 수치가 있다 |

### 2026-09-25 — 전고점 대비 회복률 추가, 역세권 보류

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 회복률 | "입지 체력을 보는 가장 좋은 지표"라고만 적고 뷰·도구에 없었다(모델이 시계열로 직접 계산해야 했다) | `mv_danji_recovery` 추가. `search_candidates`·`get_danji`·`get_region_stats`가 반환. 정의는 Phase 2 "전고점 대비 회복률". 사용자 승인 | 개발 DB(해제·직거래 제외, 분기 표본 3건 이상): 단지·평형별 2021~2023 고점 분기의 85%가 2021Q1~2022Q2(2021Q3 31%). 저점 분기는 2022Q4~2024Q2로 흩어지고 거래절벽기 분기가 표본 부족으로 빠져 보조 지표로 둔다. 낙폭 중위 0.795. 시도 전체 분기 중위는 거래 단지 구성에 따라 흔들려(서울 2022Q3 1,097 → 2023Q2 1,269만원/m²) 기준으로 쓰지 않았다 |
| 역세권 가중 | 좌표가 있으면 최근접 역 거리 컬럼 | 보류. 사용자 결정 | 좌표 데이터 출처가 계획에 없고 `danji.lat/lon`이 20,724개 모두 비어 있다 |

### 2026-09-25 — 구현 순서 5단계(Phase 2) 착수 전 결정과 구현

개발 DB 전체 수집분(매매 1,155,313건, 전월세 4,066,952건)으로 확인하고 정했다. 모두 사용자 승인. DDL은 `db/migrations/0004`\~`0007`.

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 한 단지가 aptSeq 여럿 | Phase 2 전에 매핑 테이블 여부 결정 | 만들지 않는다(보류, 정제 규칙 3) | 같은 법정동·이름·준공연도인데 aptSeq가 여럿인 묶음 68개(단지 137). 같은 필지 18, 매매가 한쪽에만 28, 양쪽 다 매매 없음 12(임대라 규칙 6으로 빠짐). 영향은 표본이 나뉘는 것이고 `sample_size`로 드러난다. 이름으로 묶으면 단지명 매칭 금지에 어긋난다 |
| 전세 시세 | 명시 없음 | 갱신 계약 제외, 완전 중복 유지, 월세 0 전세만(정제 규칙 7) | 갱신 전세금 = 같은 단지·평형·월 신규의 중위 91.7%(IQR 84.0\~99.8%, 2025\~2026, 7만 칸). 2025\~2026 전세의 39\~44%가 갱신. 완전 중복은 매매 있는 단지에서 초과분 153,066행(4.4%), 전월세 API에 동이 없어 구분 불가 |
| 재수집 창 | 최근 3개월 | 매일 3개월 + 주 1회 최근 12개월 매매(`--recent`) | 해제일이 계약월+2개월 안에 붙는 비율 77.3%, 5개월 95.0%, 11개월 98.9%(해제 47,480건, 계약 2025-08 이전). 3개월 창으로는 해제의 22.7%를 놓쳐 신고가 취소가 고점에 남는다. 12개월 매매 = 83 × 12 ≈ 1,000회 |
| 이상치·거래유형 | 컬럼 또는 뷰, `dealing_type` 활용은 Phase 2에서 | 뷰에서 계산. 직거래는 시세에서 제외, 빈 값은 포함. `is_outlier`는 m2당, 연도 칸 5건 이상(정제 규칙 5) | 중위 70% 미만 비율 직거래 4.65%, 중개 0.25%. SPEC 50%/200% 규칙은 총액 기준으로도 중개 0.02%, 직거래 0.19%만 걸린다. m2당으로 바꾸면 같은 평형그룹 안의 면적 차이가 이상치로 잡히지 않는다(중개 0.001%, 직거래 0.068%) |
| 임대 단지 | 대원칙만(판별 방법 미정) | 시세용 매매(해제·직거래 제외)가 5년간 0건인 단지는 모든 통계에서 제외 + 사람이 확인한 예외 목록 `danji_flag`(정제 규칙 6) | 매매 행이 없는 단지 2,535개에 더해 직거래만 있는 363개(대부분 2024\~2026 준공 청년안심주택·민간임대, `베르디움STAY1` 직거래 252건), 해제만 있는 21개. 이름 규칙은 놓침(`김포한강예미지뉴스테이`)과 오탐(`LH동분당센트럴파크`, `THESHARP판교퍼스트파크`)이 모두 있다 |
| 토지임대부 | 대원칙만 | `land_lease`: 단지 자체 시세만 두고 비교·지역 통계·후보에서 제외. 2곳 | 빼지 않으면 `강남브리즈힐`이 강남구 M 가격 분위 8.2·회복률 분위 0. LH `브리즈힐` 3곳은 2021-01부터 매매 247\~295건으로 공공분양이라 넣지 않았다 |
| 분양전환 | 명시 없음 | `sale_conversion`: 첫 시세용 매매일(또는 `converted_on`) 이전 전월세 제외. 52곳 | 후보 조건(오래된 단지, 매매 전 전월세 30건·월 3건 이상)에 63곳이 걸린다. 매매 전 전월세가 월 5건 이상이면 대부분 부영·NHF·LH·공공임대(`서봉마을사랑으로부영6단지` 2,503건). 모든 단지에 일반 규칙으로 걸면 신축 입주장 전세(349단지)까지 빠진다. 이름 표시가 있는 25곳 + 월 5건·100건 이상 27곳을 올렸다. 일반 단지가 섞였을 수 있지만(`서울역센트럴자이`는 2021-11까지 매매가 비어 있다) 첫 매매가 최근 6개월보다 앞서면 현재 시세는 바뀌지 않는다 |
| 전세가율 | "전세가율"(식 미정) | m2당 중위끼리 나눈다 | 총액으로 나누면 칸의 7.7%(7,405칸 중 568)가 5%p 이상 어긋나고, XS 그룹에서 100%를 넘는 경우가 생긴다(`삼정아트테라스정동` 146.9% → m2당 78.0%) |
| 뷰 구조 | 머티리얼라이즈드 뷰 5개 | 로직은 일반 뷰 `v_*`, `mv_*`는 스냅숏. `etl.refresh`가 한 트랜잭션에서 갱신하고 `mv_refresh_log`에 기록 | 규칙을 한 곳에 두고, 테스트가 가상 단지로 `v_*`를 좁혀 조회할 수 있다(단지 단위 수십 ms, 시군구 비교 뷰 20\~30초). 전체 갱신 약 110초 |
| 뷰 갱신 주기 | 표는 일 1회, cron은 매월 1일 | 매일 수집 뒤 | `data_completeness`와 최근 6개월 창이 갱신 시점 기준이라 매일 바뀐다 |
| 기준일·`data_completeness` | 현재월 기준 2개월 이내면 partial | `as_of_date()`(한국 시간). 기준월 포함 3개월이 partial | 재수집 창과 같은 범위(3단계 결정). DB 타임존이 UTC다 |
| LTV | 규제 0.40 / 비규제 0.70 | 규제지역 서민·실수요자(무주택 세대주, 부부합산 9천 이하, 8억 이하) 0.60 추가. 인자 `homeless_household_head`, `household_income_manwon` | 10·15 대책 금융위 FAQ. 사용자는 미혼 무주택 세대주, 연소득 8천이라 해당. 자기자본 3억 기준 규제지역 최대 매수가가 49,100 → 67,054 |
| 대출 상한 | `loan_cap_manwon DEFAULT 60000` | 주택가격 구간별 정책 상한(15억 이하 6억, 25억 이하 4억, 초과 2억)을 `policy_params`에서 읽고, `loan_cap_manwon`은 사용자가 더 낮게 둘 때만 쓰는 선택 인자 | 10·15 대책 |
| 취득세 | `acquisition_tax(price)` | 전용면적 선택 인자(85m2 초과 농어촌특별세 0.2%). 지방교육세 포함 | 지방세법. 목표 평형 S·M은 85m2 이하라 기본값으로 충분하다 |
| `max_purchase_price` 반환 | 최대가, 대출, `binding_factor` | LTV, 서민 적용 여부, LTV·DSR·상한 한도, 취득세, 필요 현금, `binding_detail` 추가 | 예산을 무엇이 묶는지 근거를 함수 결과에서 바로 보게 한다 |
| `binding_factor` 정의 | 명시 없음 | `max_price`보다 1만원 비싼 집을 못 사게 막는 한도. 구간 경계면 `binding_detail` | 구현 중 발견: 서민·실수요자가 8억 경계에 걸리면 8억에서의 대출은 DSR 한도지만 막는 것은 8억 초과 LTV 40%다. 대출이 정해진 한도로 답하면 `DSR`이라고 잘못 말한다 |
| 정책 값 기본 인자 | SQL 함수에 숫자 기본값 | 인자를 비우면 `policy_params`에서 시행일 기준으로 읽고, 값이 없으면 오류 | SPEC "세율과 규제 파라미터는 하드코딩하지 말고"를 함수 기본값에도 적용 |
| 분석 기준값 | 자기자본·연소득·무주택·차량·통근 | 부부합산 소득, 무주택 세대주, 생애최초 여부, 최소 세대수, 선호 평형 추가 | 서민·실수요자 요건과 후보 필터 기본값에 필요. 생애최초는 아님(사용자 확인) |

### 2026-09-25 — 구현 순서 6단계(Phase 3: MCP 서버 stdio) 착수 전 결정과 구현

개발 DB(`mv_*` 기준일 2026-09-25)로 확인했다. 예산 적용 방식, `find_danji` 추가, 기본 정렬, 읽기 전용 계정 생성은 사용자 승인. DDL은 `db/migrations/0008_mcp_read.sql`, 서버는 `mcp_server/`.

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| `search_candidates` 예산 | `max_price_manwon` 인자로 `compute_budget` 결과를 받는다(설명문으로 강제) | 서버가 같은 함수로 시군구 규제 여부·평형그룹마다 예산을 계산해 적용. `max_price_manwon`은 사용자가 더 낮게 두는 상한 | 모델이 숫자를 옮기거나 어림잡을 여지가 없어진다. 여러 시군구를 섞으면 규제 여부마다 예산이 다르다(서민·실수요자가 아니면 규제 49,100 / 비규제 67,054). L·XL은 농어촌특별세로 조금 낮다(비규제 66,929 / M 67,054) |
| 단지명 검색 | 도구 없음 | `find_danji` 추가. 공백·`아파트`·괄호를 뺀 부분 일치로 찾고 주소와 `scope`를 함께 준다 | 사용자가 이름으로 물으면 `danji_id`를 얻을 길이 없었다. 이름이 같은 단지가 2개 이상인 이름이 1,317개(단지 5,125개)라 주소로 골라야 한다. 조회용이고 데이터 매칭에는 쓰지 않는다 |
| 후보 정렬 | 명시 없음 | `sort_by` 셋(가격 분위 낮은 순 기본, 중위가 높은 순, 회복률 차이 낮은 순). 어느 정렬이든 표본 3건 미만은 뒤로 | 서울 S·M, 예산 이하, 300세대 이상이 284칸으로 기본 `limit` 50을 넘는다. 표본 1\~2건 칸이 분위 양 끝에 몰린다(성북구 `대광` 1971년 S, 1건, 분위 0). 뒤로 보내기는 구현 중 결정 |
| 세대수 모름 | 명시 없음 | `min_households` > 0이면 빼고 수를 `meta.counts`에 적는다. `0`이면 포함 | 인천·경기 건축물대장 조회가 진행 중이다(`full` 단지 세대수: 서울 93.2%, 인천·경기 0%). 기본 조건 전체 검색에서 7,314칸이 이 이유로 빠진다 |
| 제외 사유 | 명시 없음 | 가격 → 예산 → 세대수 순서로 한 번만 센다(`meta.counts`, 합 = 검토한 칸) | 처음 구현은 사유를 겹쳐 세서, 세대수를 모르면서 예산도 넘는 분당 214칸이 어디에도 안 잡혔다 |
| `get_recent_trades` 인자 | `danji_id` 또는 `lawd_cd5`, `limit` | `kind`(매매·전월세), `area_group`, `since`, `include_canceled`(기본 false) 추가. 판정 플래그(`is_basis` 등) 포함 | 검증 체크리스트 "시세의 근거 거래를 확인": DMC파크뷰자이1단지 M은 `since` = 기간 시작일로 부르면 `is_basis` 14건 = `sample_size` 14, 중위가 일치(S·XL도 같음, 테스트). 해제 제외 기본값은 정제 규칙 1 그대로 |
| 계산 위치 | MCP 서버는 뷰만 읽는다 | `0008`에 요약·분포·거래량·규제지역 뷰와 평당가·평형그룹 예산 함수. 서버에는 조회·필터·정렬만 | 여러 도구가 같은 지표를 내므로 조인·환산을 한 곳에 둔다. 분포의 회복률 p50이 `mv_danji_recovery` 시군구 중위와 모든 칸에서 같다(테스트) |
| 읽기 전용 계정 | "분리한다" | 역할 `apartment_reader`(마이그레이션) + 로그인 `apartment_mcp`(마이그레이션 밖), `MCP_DATABASE_URL`만 사용 | 원시 거래·수집 이력 SELECT, 쓰기, `REFRESH`, `CREATE`가 모두 거부된다(테스트) |
| MCP SDK | FastMCP | 공식 `mcp` 2.2의 `MCPServer` | 2.x에서 FastMCP가 `MCPServer`로 이름이 바뀌었다(`mcp.server.fastmcp`는 안내만 남은 import 오류). 독립 패키지 `fastmcp` 4.x는 의존성이 훨씬 많다 |
| 응답 텍스트 | JSON | 구조화 결과 + 같은 내용의 들여쓰기 없는 JSON 텍스트 | SDK 기본(들여쓰기 2칸)보다 `search_candidates` 50행 40,491 → 29,648바이트, `get_danji` 9,036 → 6,191 |
| 조회 속도 | — | 규제 여부를 시군구마다 한 번 계산, 원시 거래는 단지 하나면 `=` 조건 | `is_regulated()`를 단지 행마다 부르면 2만 행에 0.54초. `v_sale_basis`를 단지 id 배열로 걸면 뷰 안 단지·연도 중위가 전체를 돌아 단지 하나도 1.9초, `=` 조건이면 8ms. 결과: 83개 전체 후보 검색 0.9초, 2개 구 0.2초, `get_danji` 30ms, 시군구 원시 거래 1.6초 |

### 2026-09-25 — 구현 순서 7단계(HTTP 노출 + 인증)

사용자가 정한 것: 최종 사용처는 claude.ai 커스텀 커넥터(Pro 요금제), 서버는 NAS 컨테이너(포트 28001), 도메인 `https://apartment-mcp.movingjin.com`, 커넥터 화면에 요청 헤더 칸이 있음을 확인. 커넥터 조건은 Claude 문서(claude.com/docs/connectors/building/authentication, support.claude.com 11175166)로 확인했다.

| 항목 | 원래 계획 | 바뀐 내용 | 근거 |
| --- | --- | --- | --- |
| 전송 방식 | Cloudflare Tunnel(권장) 또는 DDNS + 리버스 프록시 중 결정 | 도메인이 공인 IP(211.222.168.70)로 풀리는 2번 방식. 서버는 NAS 컨테이너 28001 | 사용자 결정. 커넥터는 Anthropic 클라우드에서 접속하므로 공개 인터넷에서 닿아야 한다 |
| 인증 | "HTTP 노출 시 인증 필수"(방식 미정) | 고정 토큰(`Authorization: Bearer`, `X-API-Key`도 받음), 32자 이상, 상수 시간 비교. OAuth는 쓰지 않는다 | 커넥터는 OAuth(DCR·CIMD) 또는 요청 헤더(`static_headers`)를 지원한다. 사용자 한 명이고 커넥터 화면에 "로그인 없음 + 요청 헤더"가 있다(Pro). OAuth는 인증 서버·동의 화면·토큰 저장을 새로 만들어야 한다 |
| HTTP 방식 | 명시 없음 | Streamable HTTP, 경로 `/mcp`, stateless + JSON 응답 | 서버가 먼저 보내는 알림이 없다. 요청마다 독립이라 재시작·프록시에 강하다 |
| Host 헤더 검사 | 명시 없음(SDK는 host가 127.0.0.1이면 자동으로 켠다) | 끈다 | 켜진 채로 공개 도메인 Host가 오면 421로 막힌다. 막아 주는 공격(DNS rebinding)은 토큰으로 이미 막힌다 |
| 컨테이너 | "세 컨테이너"(db, etl, mcp) | `mcp` 서비스 추가, 읽기 전용 접속 문자열과 토큰만 넘김. `db` 서비스는 프로필 `local-db`로 기본에서 뺐다 | 개발 DB는 NAS의 다른 PostgreSQL(호스트 25432)이다. 그대로 두면 `docker compose up`이 빈 DB를 하나 더 띄운다 |
| 배포 | 명시 없음 | GitHub Actions: `test`(DB 없이 pytest, 이미지 빌드·스모크) → `build`(secrets로 `.env` 작성, NAS로 scp) → `deploy`(ssh로 compose build·up, `/healthz` 확인). 사용자가 쓰는 reco-act 워크플로와 같은 방식 | 사용자 지시. 트리거는 PR이 아니라 `main` push(병합 전 코드가 운영에 나가지 않게). 배포 경로가 git 작업 폴더면 멈춘다(`/home`이 NAS 볼륨이라 개발 폴더와 겹치면 개발용 `.env`를 덮어쓴다) |
| 확인 | — | 로컬: 토큰 없음·틀림 401, 공개 도메인 Host로 200, MCP 클라이언트로 HTTP 경유 도구 호출. 이미지 내용 재현(빈 가상환경 + 고정 의존성 + 환경변수만)으로 동작. 테스트 14개 | 이 PC(NAS 안의 kasm)에서는 공인 IP로 나가는 연결(80·443)이 시간 초과라 공개 주소는 외부망에서 확인해야 한다 |
