# 진행 상황

마지막 갱신: 2026-09-25

새 세션은 이 파일과 `SPEC.md`를 먼저 읽는다. SPEC은 API를 호출해 보기 전에 쓴 계획이라, 실측과 다른 부분은 SPEC 끝 "변경 이력"에 근거와 함께 고쳐 두었다.

## 현재 위치

- SPEC "구현 순서" 1단계(스키마 + 법정동 마스터), 2단계(단일 지역·단일 월 수집), **3단계(전체 수집) 완료**(2026-09-25)
  - 사용자가 2026-09-25 00:22~08:01(한국 시간, 6시간 38분) `--mode full`로 실행. 11,446구간 성공, 실패 0, 한도 초과 없음. 개발 DB에 83개 시군구 × 2021-01~2026-09 매매 1,155,313건·전월세 4,066,952건·단지 20,724개
  - 수집 대상은 법정동 마스터 79개가 아니라 API 실측 83개다(`etl/regions.py`, 사용자 승인)
  - 수집 후 점검과 단지 위치 보정까지 끝냈다(아래 "전체 수집 점검 결과", "단지 위치 보정")
- **4단계(건축물대장 부가정보) 구현 완료, 서대문구 시험 실행 완료**(2026-09-25). 설계 결정 3가지는 사용자 승인(SPEC 변경 이력 4단계): 총괄표제부가 없으면 표제부로 계산, 한 필지 여러 단지는 필지 합계 + `bldg_danji_cnt`, 원문 JSONB 저장
  - 서대문구 342단지 중 336 채움, 없음 6(1.8%). 목표 5% 이하
- 건축물대장 수도권 전체 조회는 사용자가 실행 중이다. 첫날(2026-09-25) 한도 초과로 멈춤: 필지 ok 8,944 / not_found 108 / error 3(한도 초과 1, 용적률 원문 값이 `NUMERIC(6,2)` 범위 밖 2: `1123010600-0329-0002` 249,024.56, `1135010300-0715-0000` 201,802.78). 다음 날 같은 명령을 다시 실행한다(아래 "건축물대장 전체 실행"). 끝나면 "건축물대장 전체 실행 뒤 점검"을 한다
- **5단계(Phase 2: 파생 뷰 + 계산 함수) 완료**(2026-09-25). 착수 전 결정 5가지와 구현 중 결정은 모두 사용자 승인, SPEC 본문(정제 규칙 5~7, Phase 2, 분석 기준값)과 변경 이력 "Phase 2 착수 전 결정과 구현"에 반영
  - 마이그레이션 `0004`~`0007`, `etl/refresh.py`, `etl/flags.py`, `etl/collect.py --recent`, 예외 목록 `db/danji_flag.csv`(54곳), 테스트 134개 추가(전체 277개 통과)
  - 개발 DB의 `mv_*`는 2026-09-25 기준으로 갱신해 두었다
- **6단계(Phase 3: MCP 서버 stdio) 완료**(2026-09-25). 착수 전 결정(예산은 서버가 계산, `find_danji` 추가, 기본 정렬 가격 분위, 읽기 전용 계정)은 사용자 승인. SPEC Phase 3 본문과 변경 이력 "6단계"에 반영
  - 마이그레이션 `0008_mcp_read.sql`, 서버 `mcp_server/`(도구 9개), `.mcp.json`, 읽기 전용 계정 `apartment_mcp`(개발 DB에 만들었고 `.env`에 `MCP_DATABASE_URL`), 테스트 65개 추가(전체 342개 통과)
- **7단계(HTTP 노출 + 인증) 구현 완료, 배포 대기**(2026-09-25). 사용자는 Claude(claude.ai 커스텀 커넥터, Pro)에 연결한다. NAS 컨테이너 28001 + `https://apartment-mcp.movingjin.com`, 인증은 커넥터 "로그인 없음 + 요청 헤더"의 고정 토큰. SPEC "전송 방식"·Phase 3 보안·구현과 변경 이력 "7단계"에 반영
  - `mcp_server/web.py`(토큰 미들웨어, `/healthz`), `mcp_server/Dockerfile`, `mcp_server/requirements.txt`, `.dockerignore`, `docker-compose.yml`의 `mcp` 서비스, `.env`에 `MCP_API_TOKEN`, 테스트 14개 추가(전체 356개 통과)
  - 배포는 GitHub Actions(`.github/workflows/deploy-prod.yml`, `main` push). 저장소 `github.com/movingJin/apartment-mcp`. `.gitignore` 작성, 형상관리 대상 첫 커밋(2026-09-25)
- **다음**: 사용자가 GitHub secrets를 등록하고 `main`에 push하면 Actions가 배포한다(아래 "MCP 서버 배포"). 외부망에서 `/healthz`를 확인한 뒤 claude.ai 커넥터를 저장하고, 실제 질문으로 써 보며 설명문·응답을 다듬는다
- 사용자가 실행할 것(오래 걸리지 않지만 API 호출이라 안내만 한다): 최근 12개월 재수집 1회. 매매는 주 1회 cron으로 돌릴 것을 처음 한 번 해 보는 것이고, 전월세는 지연 신고 규모를 재기 위한 것이다(아래 "12개월 재수집 1회")
- "구현 순서" 번호와 SPEC의 Phase 번호는 다르다. 1~4단계는 Phase 1(수집·적재), Phase 2(파생 뷰·계산 함수)는 5단계, Phase 3(MCP 서버)는 6~7단계다

## 개발 환경

| 항목 | 상태 |
| --- | --- |
| 개발 DB | `.env`의 `DATABASE_URL`. PostgreSQL 16.11, `host.docker.internal:25432`, DB `apartment`, 스키마 `public`. 타임존 `Etc/UTC` |
| DB 계정 | ETL은 `DATABASE_URL`(superuser). MCP 서버는 `MCP_DATABASE_URL`(`apartment_mcp`, 역할 `apartment_reader` 멤버, 읽기 전용·`statement_timeout` 30s). 둘 다 `.env` |
| MCP | 공식 SDK `mcp` 2.2(`MCPServer`). 운영: NAS 컨테이너(HTTP 28001, `https://apartment-mcp.movingjin.com/mcp`, 토큰 `MCP_API_TOKEN`). 개발 확인: Claude Code가 `.mcp.json`(절대 경로)으로 stdio 실행 |
| Python | 프로젝트 `.venv`(시스템 Python 3.10.18, SPEC 기술 스택과 같음). `pyproject.toml`의 `requires-python >=3.10` |
| 로컬 PC | Docker 없음, PostgreSQL 서버 없음(psql 12 클라이언트만) |
| `.env` | `SERVICE_KEY`(디코딩 키), `POSTGRES_PASSWORD`, `DATABASE_URL` |
| `docker-compose.yml` | `db` 서비스만 있음. 로컬에 Docker가 없어 실행해 본 적 없음 |

저장소는 `github.com/movingJin/apartment-mcp`다(2026-09-25에 새로 clone한 폴더로 옮겼고 옛 폴더는 `~/Projects/apartment-mcp_2`). git에 없는 것: `.env`(비밀값, `.env.example` 참고), `.mcp.json`(이 PC 절대 경로), `.venv`, `logs/`, 법정동코드 원천 파일(`KIKcd_B…txt`는 행안부 공지 `jscode20260701(말소코드포함).zip`, 국토부 CSV는 data.go.kr), `db/danji_flag_candidates.csv`(`etl.flags --candidates` 출력). 새 PC에서는 `.env`와 원천 파일을 따로 가져온다.

시스템 3.10에는 `ensurepip`이 없어서 `python3.10 -m venv`로 만들면 pip이 없다. `~/.local/bin/virtualenv`로 만든다.

```bash
~/.local/bin/virtualenv -p /usr/bin/python3.10 .venv
.venv/bin/pip install "psycopg[binary]>=3.1" "httpx>=0.27" "tenacity>=8" "mcp>=2.2" "pytest>=8"

set -a && . ./.env && set +a                               # 모든 명령 전에 한 번
.venv/bin/python -m etl.migrate
.venv/bin/python -m etl.lawd KIKcd_B.20260701_말소코드포함.txt   # 행안부 법정동코드 (아래 참고)
.venv/bin/python -m etl.collect --mode full --dry-run      # 받을 구간 수만 (API 호출 없음)
.venv/bin/python -m etl.collect --mode full                # 83개 × 2021-01~현재월. 'ok' 구간은 건너뜀
.venv/bin/python -m etl.collect --mode incremental         # 최근 3개월만 (cron용)
.venv/bin/python -m etl.collect --mode full --region 11410 --from 2025-06 --kind sale --force
.venv/bin/python -m etl.bldg --dry-run                     # 건축물대장: 받을 법정동·필지 수만
.venv/bin/python -m etl.bldg                               # 건축물대장: 안 받은 것만 받고 단지에 반영(재개)
.venv/bin/python -m etl.bldg --region 11410                # 시군구 한정
.venv/bin/python -m etl.bldg --report                      # 부가정보 없는 단지 리포트 (API 호출 없음)
.venv/bin/python -m etl.bldg --rederive                    # 저장된 원문에서 파생 값 재계산 (API 호출 없음)
.venv/bin/python -m etl.collect --mode incremental --kind sale --recent 12   # 최근 12개월 매매 (주 1회 cron용)
.venv/bin/python -m etl.refresh                            # 파생 지표 mv_* 전부 다시 계산 (약 2분). 수집·예외 목록 변경 뒤
.venv/bin/python -m etl.flags --candidates                 # 예외 목록 후보 → db/danji_flag_candidates.csv
.venv/bin/python -m etl.flags --load                       # db/danji_flag.csv → danji_flag (파일이 기준, 없는 행은 지움)
.venv/bin/python -m mcp_server                             # MCP 서버(stdio). 보통은 Claude Code가 .mcp.json으로 띄운다
.venv/bin/python -m mcp_server --http --port 28011         # MCP 서버(HTTP, 로컬 확인용). 운영은 docker compose up -d --build mcp
.venv/bin/python -m pytest                                 # DATABASE_URL·MCP_DATABASE_URL이 없으면 DB 테스트는 건너뛴다
```

### MCP 서버 배포 (NAS 컨테이너, claude.ai 커넥터)

`main`에 push하면 GitHub Actions(`.github/workflows/deploy-prod.yml`, reco-act와 같은 방식)가 배포한다. Actions 탭에서 수동 실행도 된다.

1. `test`: DB 없이 pytest(DB 테스트는 건너뜀, 133개), 이미지 빌드, 이미지 스모크 테스트(이미지 안 파일이 `etl`·`mcp_server`뿐인지, `/healthz`, 토큰 없음 401, 토큰으로 `tools/list` 200)
2. `build`: 배포 경로가 git 작업 폴더면 멈춤 → secrets로 `.env`(MCP 두 값만) 작성 → `docker-compose.yml`·`.dockerignore`·`.env`·`mcp_server/*`·`etl` 세 파일을 `${SERVICE_ROOT}/apartment-mcp`로 전송
3. `deploy`: NAS에서 `docker-compose build mcp` → `up -d mcp` → `image prune` → `/healthz`를 60초까지 기다린다(실패하면 로그 50줄)

GitHub secrets(저장소 Settings → Secrets and variables → Actions):

| 이름 | 값 |
| --- | --- |
| `PROD_HOST`, `PROD_PORT`, `PROD_USERNAME`, `PROD_PRIVATE_KEY` | NAS SSH 접속(reco-act와 같은 값) |
| `SERVICE_ROOT` | NAS 배포 루트(reco-act와 같은 값). `${SERVICE_ROOT}/apartment-mcp`가 이 개발 폴더(`~/Projects/apartment-mcp`)와 같으면 안 된다. 같으면 워크플로가 멈춘다 |
| `MCP_DATABASE_URL`, `MCP_API_TOKEN` | 개발 `.env`의 같은 이름 값(`grep '^MCP_' .env`) |

수동으로 띄울 때(배포 경로에서, `.env`에 MCP 두 값이 있어야 한다):

```bash
PATH="/var/packages/ContainerManager/target/usr/bin/:$PATH"
docker-compose up -d --build mcp
docker-compose logs -f mcp               # "Uvicorn running on http://0.0.0.0:28001"이 나오면 된다
curl -s http://127.0.0.1:28001/healthz   # NAS 안에서: ok
```

- 외부망(휴대폰 LTE 등)에서 `https://apartment-mcp.movingjin.com/healthz`가 `ok`면 도메인·인증서·리버스 프록시가 맞다. NAS 안(kasm 포함)에서는 공인 IP로 나가는 연결이 시간 초과라 확인할 수 없다(2026-09-25)
- 리버스 프록시는 `https://apartment-mcp.movingjin.com` → `http://localhost:28001`(또는 NAS IP:28001). 경로는 그대로 넘긴다
- claude.ai 커넥터: URL `https://apartment-mcp.movingjin.com/mcp`(**`/mcp`까지**), 인증 "로그인 없음", 요청 헤더 이름 `Authorization`, 값 `Bearer <토큰>`(필수). 토큰은 `grep '^MCP_API_TOKEN=' .env | cut -d= -f2-`
- 토큰을 바꾸면 개발 `.env`와 GitHub secret `MCP_API_TOKEN`을 함께 고치고 다시 배포한 뒤 커넥터 헤더 값도 바꾼다
- 문제별 증상: 401 = 헤더 이름·값(`Bearer ` 접두어) 확인. 도구 호출이 "Error executing tool"만 돌려주면 서버 로그(`docker compose logs mcp`)를 본다. 컨테이너에서 DB 접속이 안 되면 `host.docker.internal`(= `extra_hosts`의 host-gateway)로 NAS 호스트 25432에 닿는지, PostgreSQL `pg_hba`가 도커 네트워크 대역을 받는지 본다
- 로컬에서 HTTP로 띄워 보기: `.venv/bin/python -m mcp_server --http --port 28011`(토큰은 `.env`에서 읽는다)

### MCP 서버 (Claude Code, stdio)

- Claude Code(VS Code 확장 포함)를 이 저장소에서 다시 열면 `.mcp.json`의 `apartment` 서버를 쓸지 묻는다. 승인하면 도구 9개(`list_regions`, `compute_budget`, `search_candidates`, `find_danji`, `get_danji`, `get_price_history`, `get_recent_trades`, `get_region_stats`, `compare_danji`)가 보인다. `/mcp`로 상태를 본다
- 서버는 `.env`의 `MCP_DATABASE_URL`을 직접 읽는다(셸에서 `.env`를 불러오지 않아도 된다). 없으면 시작하지 않는다
- `.mcp.json`은 이 PC의 절대 경로다. 저장소를 옮기면 고친다
- 응답의 `data_as_of`는 마지막 `etl.refresh` 기준일이다. 수집 뒤 `etl.refresh`를 빼먹으면 `meta.warning`이 붙고 `get_recent_trades`(원시 거래, 실시간)와 시세(스냅숏)가 어긋날 수 있다
- 다른 DB(NAS 등)에 로그인 계정을 만드는 법. 역할 `apartment_reader`와 권한은 `0008`이 만든다:

```sql
CREATE ROLE apartment_mcp LOGIN PASSWORD '…' IN ROLE apartment_reader;
ALTER ROLE apartment_mcp SET default_transaction_read_only = on;
ALTER ROLE apartment_mcp SET statement_timeout = '30s';
```

- 나중에 뷰를 새로 만들어 MCP가 읽게 하면 그 마이그레이션에서 `GRANT SELECT … TO apartment_reader`를 함께 한다

### 건축물대장 전체 실행

```bash
cd ~/Projects/apartment-mcp
set -a && . ./.env && set +a
nohup .venv/bin/python -m etl.bldg > logs/bldg_full.log 2>&1 &
tail -f logs/bldg_full.log
```

- 진행 줄 `[i/N] recap 법정동: n건` / `[i/N] title 필지: n건 ok 세대 …`. 재시도는 `  재시도 n회째 실패 (…)` 줄로 보인다. 끝에 `끝 (소요시간): 성공 …` 줄과 리포트가 나온다
- 호출당 약 0.45초라 한도(10,000회)까지 약 1시간 15분. 한도 초과(`멈춤: 일일 한도 초과`)나 연속 5회 실패로 멈추면 **다음 날 같은 명령을 다시 실행한다**. 받은 법정동·필지는 건너뛴다. 3일째에 끝날 것으로 예상
- 한도 초과 응답 형식은 아직 못 봤다. 오류 코드 22가 아니면 연속 5회 실패로 멈춘다(건축HUB 재시도가 8회라 멈추기까지 10분쯤 걸릴 수 있다)
- 진행 확인: `psql "$DATABASE_URL" -c "select status, count(*) from bldg_parcel group by 1"` (필지 약 20,300개)
- 끝나면(`받을 것: … 0`) `--report`로 결과를 본다

### 12개월 재수집 1회

```bash
cd ~/Projects/apartment-mcp
set -a && . ./.env && set +a
psql "$DATABASE_URL" -c "select kind, sum(row_count) from ingest_log group by 1"   # 실행 전 행 수
date -u +%FT%TZ                                                                    # 시작 시각 (새 행 판별용)
.venv/bin/python -m etl.collect --mode incremental --recent 12 > logs/recent12.log 2>&1
.venv/bin/python -m etl.refresh
```

- 매매·전월세 각 996구간(+추가 페이지), 호출당 0.1초 안팎이라 수 분. 로그 끝의 구간별 `inserted`/`deleted` 합계가 지연 신고 규모다
- 끝나면 결과를 알려 준다. 점검: 시작 시각 뒤 `ingested_at`인 행 수(새로 들어온 행)와 `ingest_log` 합계 변화(순증)로 지운 행 수를 구한다. 전월세도 주 1회 12개월을 받을지 이 수치로 정한다

### 전체 수집 실행

```bash
cd ~/Projects/apartment-mcp
set -a && . ./.env && set +a
mkdir -p logs
nohup .venv/bin/python -m etl.collect --mode full > logs/collect_full.log 2>&1 &
tail -f logs/collect_full.log
```

- 진행 줄 `[i/N] 종류 시군구 월: fetched … | inserted …`. 성공한 구간이 있으면 끝에 모든 단지의 위치를 다시 계산하고(약 30초) `단지 위치 다시 계산: N개 바뀜`, 이어서 `끝 (소요시간): 성공 …, 실패 …` 줄이 나온다
- 중단·실패·한도 초과(`멈춘 종류: … (일일 한도 초과)`)면 같은 명령을 다시 실행한다. 'ok'가 아닌 구간과 재수집 창만 받는다
- 매매·전월세를 `--kind sale`, `--kind rent`로 나눠 두 프로세스로 동시에 돌려도 된다(단지 upsert를 `aptSeq` 순서로 잠가 교착하지 않는다)
- 진행 확인: `psql "$DATABASE_URL" -c "select kind, status, count(*) from ingest_log group by 1, 2"`

## 완료한 것

- `db/migrations/0001_phase1_schema.sql`, `0002_api_verified_fields.sql`: SPEC 스키마 + 실측 반영
- `etl/migrate.py`, `etl/lawd.py`: 마이그레이션 실행기, 법정동코드 적재. 처음엔 data.go.kr CSV 20260813(49,861행)을 넣었고, 3단계에서 행안부 `KIKcd_B` 20260701(말소코드포함, 53,391행) 고정폭 형식을 읽게 해 다시 적재했다(lawd 53,414행, 활성 20,570)
- `etl/rtms.py`: 실거래가 API 호출. 모든 페이지 수집, `totalCount` 대조, 요청 간격 제한(기본 5 rps), 429/5xx·네트워크 오류 지수 백오프(6회), 오류 메시지에서 인증키 가림
- `etl/records.py`: 응답 항목 → `SaleRow`/`RentRow` 정규화(쉼표·공백 숫자, `YY.MM.DD`, 전용면적 소수 4자리, 빈 값), `src_hash`, 표시용 단지명
- `etl/collect.py`: (종류, 시군구, 월) 단위 DB 동기화, 단지 upsert, `ingest_log` 기록, CLI(구간별 에러 격리, 끝에 실패 목록, 실패 있으면 종료 코드 1)
  - 3단계: `--mode full|incremental`, `--force`, `--dry-run`. 재개(`ok` 구간 건너뜀), 재수집 창(현재월 포함 3개월, 한국 시간), 일일 한도 초과·같은 종류 연속 5구간 실패 시 그 종류만 멈춤, 진행 번호 `[i/N]`, 줄 단위 출력(파일로 돌려도 바로 보임), Ctrl+C 시 안내
- `etl/regions.py`: 수집 대상 83개와 2026 개편으로 옮겨 간 옛 코드(`RECODED`). 이름은 행안부 코드 파일 기준
- `etl/rtms.py` 3단계: 오류 코드 22(`LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR`)를 `QuotaExceeded`로 구분하고 재시도하지 않는다
- `etl/collect.py` 3단계 마무리: `refresh_danji_locations()` — 단지 위치를 거래 행 최빈값으로 다시 정한다. 수집 실행 끝에 모든 단지 대상으로 호출(아래 "단지 위치 보정")
- 4단계: `db/migrations/0003_bldg_register.sql`(원문 `bldg_recap`·`bldg_title`, 이력 `bldg_recap_log`, 필지 결과 `bldg_parcel`, `danji.bldg_danji_cnt`, 대지구분 함수 `plat_gb_of`, 리포트 뷰 `v_bldg_missing`)
- `etl/datagokr.py`: 공공데이터포털 공통 클라이언트(간격 제한, 재시도, 페이지 반복·`totalCount` 대조, 한도 초과 구분, 키 가림). `etl/rtms.py`에서 옮겼다. 바뀐 동작은 둘: 본문이 빈 HTTP 200을 재시도하고, 재시도할 때 stderr에 사유를 찍는다. `etl/rtms.py`는 실거래가 엔드포인트만 남았고 기존 import는 그대로 된다
- `etl/bldg.py`: 건축HUB 클라이언트(재시도 8회), 파생 규칙 `derive`, 법정동 단위 총괄표제부 → 필지 단위 표제부 순서의 계획·실행(재개, 한도 초과·연속 5회 실패 시 멈춤), 원문 재계산 `rederive`, 단지 반영 `apply_to_danji`, 리포트
- 5단계(Phase 2): `db/migrations/0004_phase2_base.sql`(기준일 `as_of_date()`, `data_completeness_of()`, 예외 목록 `danji_flag`, 기반 뷰 `v_danji_status`·`v_sale_basis`·`v_rent_basis`, `idx_rent_danji`, `policy_params`·`regulated_area`·`analysis_profile`), `0005_phase2_views.sql`(`v_*` 5개와 스냅숏 `mv_*` 5개, `mv_refresh_log`), `0006_phase2_budget.sql`(`policy_num`, `is_regulated`, `max_loan_by_dsr`, `acquisition_tax`, `purchase_loan_terms`, `max_purchase_price`), `0007_policy_20260925.sql`(정책 값·규제지역 40곳·분석 기준값)
- `etl/refresh.py`: `mv_*` 5개를 한 트랜잭션에서 갱신, `mv_refresh_log` 기록. 개발 DB 약 110초
- `etl/flags.py`: 예외 목록 후보 추출(`--candidates`), `db/danji_flag.csv` 적재(`--load`, 파일과 테이블을 맞춘다), 목록(`--list`)
- `etl/collect.py --recent N`: 재수집 창 개월 수(기본 3). 주 1회 12개월 매매용
- 6단계(Phase 3): `db/migrations/0008_mcp_read.sql`(뷰 `v_danji_summary`·`v_region_distribution`·`v_region_volume_monthly`·`v_regulated_area_current`, 함수 `per_pyeong`·`max_purchase_price_for_group`, 역할 `apartment_reader`), `mcp_server/`(`server.py` 도구 등록·설명문·서버 안내문, `tools.py` 조회, `db.py` 읽기 전용 접속, `__main__.py`), `.mcp.json`, `pyproject.toml`에 `mcp>=2.2`, `.env.example`에 `MCP_DATABASE_URL`
- 7단계: `mcp_server/web.py`(Streamable HTTP `/mcp`, stateless + JSON, 토큰 미들웨어 `RequireToken`, `/healthz`, Host 헤더 검사 끔), `__main__.py --http`, `mcp_server/Dockerfile`·`requirements.txt`, `.dockerignore`(허용 목록), `docker-compose.yml`(`mcp` 서비스, `db`는 프로필 `local-db`), `.env`·`.env.example`에 `MCP_API_TOKEN`
- 형상관리: `.gitignore`, `.github/workflows/deploy-prod.yml`(test → build → deploy). `docker-compose.yml`의 `POSTGRES_PASSWORD`를 필수(`:?`)에서 빈 기본값으로 바꿨다. 필수로 두면 `mcp`만 띄울 때도 compose가 이 값을 요구한다
- 테스트 356개: 6단계까지 342 + HTTP 14(`tests/test_mcp_web.py`: 토큰 거부 7·통과 3, `/healthz`, 토큰 길이, 실제 uvicorn으로 인증·공개 도메인 Host·MCP 클라이언트 호출)
- 6단계까지 테스트 342개: 5단계까지 277 + MCP 65(`tests/test_mcp.py`: 읽기 전용 계정 권한 11, `0008` 뷰·함수 15, 도구 불변식·근거 추적 36, MCP 프로토콜 경유 3). MCP 테스트는 개발 DB의 실제 `mv_*`를 읽어 값 대신 불변식을 본다
- 5단계까지 테스트 277개: 1~4단계 143 + 파생 뷰 12(가상 시군구 `99998`·`99999`에 가상 단지를 넣고 기준일을 고정. 시군구 비교 뷰 테스트 하나가 약 50초) + 예산 함수 113(손계산, Python 참조 구현과 96개 조합 대조, 규제지역) + 예외 목록 8 + 재수집 창 1
  - 1~4단계 143개: 법정동 25(행안부 고정폭 포함), 레코드 정규화 42, API 클라이언트 20(MockTransport), 수집 23(계획·멈춤·단지 위치 포함, 일부 DB 필요), 대상 시군구 2(1개 DB 필요: lawd에서 뽑은 목록 = `REGIONS`), 면적 경계 10(DB 필요), 건축물대장 21(실제 응답 fixture `tests/fixtures/bldg/`로 파생 규칙 7유형, 5개 DB 필요)
  - DB 테스트는 개발 DB에서 `force_rollback` 트랜잭션 안에서 돌아 데이터를 남기지 않는다. 롤백돼도 시퀀스는 소모되므로 `id`에 틈이 생긴다(서대문구 단지가 `danji_id` 1217부터 시작하는 이유)

### 이번 단계의 결정 (SPEC 변경 이력 2026-09-25)

- **`src_hash`**: 저장하는 모든 값 + 같은 값인 행 사이 순번. (종류, 시군구, 월) 단위로 새 해시만 넣고 응답에서 사라진 해시는 지운다. 값이 바뀐 행만 `id`가 바뀐다(사용자 승인)
- **전월세 읍면동 이름**: 리 단위도 `양평읍 양근리` 형식이라 `lawd.dong`과 그대로 대응한다(양평군 샘플로 확인)

### 2단계 확인 결과 (서대문구 2025-06, 실제 수집)

| 확인 항목 | 결과 |
| --- | --- |
| 멱등성 (SPEC 체크리스트) | 두 번째 실행에서 inserted 0, deleted 0. 응답 순서를 섞어도 같음(테스트) |
| 해제된 원 신고와 재신고가 둘 다 남는가 | 남음. SPEC 7개 필드로 겹치는 61그룹 125행 모두 적재, 해제 77건. 예: 남가좌동현대 84.78㎡ 2025-06-21 3층 88,000 → id 4628(등기 25.11.28) + id 4762(해제 25.10.30) |
| 남가좌동 385가 서로 다른 `danji_id`가 되는가 | 6개: DMC파크뷰자이 1~5단지 + 2단지(임대). 모두 `1141012000`/`0385`/`0000` |
| 전월세 행이 `aptSeq`로 단지에 붙는가 | 639건 모두 연결. 전월세에만 나온 단지 45개도 `umdNm`으로 `lawd_cd`를 얻었다(NULL 0) |
| `aptSeq` 하나가 지번 하나에 대응하는가 | 이 한 달에서는 151개 모두 이름·지번이 하나. 전체 데이터로 3단계에서 다시 본다 |
| 전용 60.00 | `S`로 들어감(매매 2 + 전월세 7) |
| 실패 처리 | 잘못된 키로 호출하면 게이트웨이가 **HTTP 403 + `<OpenAPI_ServiceResponse>` XML**을 준다. 사유를 뽑아 `ingest_log`에 `error`로 남기고 다음 구간으로 넘어간다 |

SPEC 체크리스트 "알고 있는 실거래 1건을 국토부 사이트와 대조"는 완료했다. 2026-09-25 사용자가 rt.molit.go.kr에서 위 남가좌동현대 거래(84.78㎡, 2025-06-21, 3층, 88,000만원)를 찾아 DB와 같은 거래임을 확인했다.

### 구현 메모

- 단지 upsert는 **이번에 새로 들어가는 거래 행**에 나온 `aptSeq`만 대상으로 한다. 같은 응답을 다시 넣으면 단지도 바뀌지 않는다
  - 위치(`lawd_cd5`, `lawd_cd`, 본번, 부번, 지번 문자열)는 단지를 새로 만들 때만 그 구간 값으로 넣는다. 매매는 응답의 코드, 전월세는 읍면동 이름으로 찾은 법정동코드와 지번 문자열을 나눈 본번·부번
  - 기존 단지의 위치는 `refresh_danji_locations()`만 바꾼다: 매매 행 (시군구, 법정동코드, 본번, 부번, 지번) 최빈값 → 동률이면 최근 계약 쪽. 매매가 없으면 전월세 행 (시군구, 읍면동 이름, 지번) 최빈값. 적재 순서와 무관하다
  - 단지명은 마지막으로 적재한 매매 값을 따른다. 건축년도는 매매 값(빈 값이면 기존 유지), 전월세는 비어 있을 때만 채운다
  - 한 응답 안에서 같은 `aptSeq`의 이름·지번이 다르면 가장 많은 값을 쓴다(원본은 거래 행에 남는다)
- `area_group`은 생성 컬럼이라 넣지 않는다. 연도 파티션은 CLI가 대상 연도마다 `ensure_trade_partitions()`를 먼저 호출한다
- 형식이 예상과 다른 값(모르는 `cdealType`, 빈 금액, 소수 5자리 면적, 요청과 다른 `sggCd`·계약월)은 추측하지 않고 그 구간을 실패로 남긴다
- API 응답 시간은 호출당 0.2~4초로 편차가 크다

### 3단계 확인 결과 (2026-09-25, 실제 호출)

| 확인 항목 | 결과 |
| --- | --- |
| 수집 대상 코드 | lawd 기준 79개 중 4개(인천 중구 `28110`·동구 `28140`·서구 `28260`, 화성시 `41590`)가 2021-06·2025-06 매매 모두 0건. API는 새 코드 8개(`28125`, `28155`, `28275`, `28290`, `41591`, `41593`, `41595`, `41597`)로 과거 거래까지 준다. `2811x~2819x`, `2826x~2829x`, `4159x`를 훑어 찾았다. 나머지 75개는 두 달 모두 데이터 있음(옹진군만 0건). 수치는 SPEC 변경 이력 |
| 부천 | 2021-06도 새 구 코드로 조회됨(`41192` 323, `41194` 192, `41196` 76건). `41190`은 0건 |
| 1,000건 초과 페이지 | 강남 전월세 2025-06: 3페이지, 2,033건 = `totalCount`, 6초 |
| 중단 후 재개 (SPEC 체크리스트) | 서대문 매매 2025-01~06을 도중에 SIGINT로 끊고 다시 실행: 끝난 구간은 건너뛰고 나머지만 받음. 세 번째 실행은 받을 구간 0. 커밋 직후 끊기면 진행 줄은 안 찍혀도 구간은 `ok`로 남는다(데이터와 로그가 한 트랜잭션) |
| 속도 | 첫 API 호출만 연결 수립에 약 4초. 이후 호출당 0.1초 안팎, 적재 0~0.5초 |
| 법정동 갱신 | data.go.kr CSV(20260813)에 새 코드가 없어 행안부 공지(mois.go.kr, 2026.7.1. 시행 변경내역)의 `jscode20260701(말소코드포함).zip`에서 `KIKcd_B`를 받아 적재. 새 코드 8개 지역 2025-06: 매매 읍면동 코드·이름, 전월세 읍면동 이름 모두 lawd와 일치. `28275`는 서해구(서구로 추정했던 것을 고침). 인천 출장소 코드 3개(`28115`, `28116`, `28265`)는 활성이지만 하위 동이 없고 조회 0건 |
| 쿼터 사용 | 2026-09-25 이 세션에서 매매 약 370회(대부분 코드 탐색)·전월세 약 20회 호출 |

## 전체 수집 점검 결과 (2026-09-25)

로그 파일은 남기지 않았다. 구간별 결과는 `ingest_log`에 있다.

| 확인 항목 | 결과 |
| --- | --- |
| 구간 | `ingest_log` 11,454행(83 × 69 × 2) 모두 `ok`. `row_count` 합계 = 테이블 행 수(매매 1,155,313, 전월세 4,066,952) |
| 용량 추정과 비교 | SPEC 추정 매매 약 150만·전월세 약 350만. 매매는 2022~2023 거래절벽 때문에 적고, 전월세는 많다 |
| 해제 | 매매 59,397건(5.1%) |
| 0건 시군구 | 옹진군(`28720`)뿐 |
| 적은 달 | 시군구 월 중위값의 20% 미만인 달은 대부분 2022년 하반기(거래절벽). 수도권 매매 월 합계가 2021-01 31,029 → 2022-07~10 4,200~4,700 → 2025-06 35,010. 2026-08(16,863)·09(9,303)은 신고 지연 |
| 단지 | 20,724개. `lawd_cd` NULL 0. 거래 행 `danji_id` NULL 0 |
| `aptSeq` → 이름 | 매매 `aptSeq` 18,189개 모두 이름 하나 |
| `aptSeq` → 지번 | 지번이 둘 이상인 `aptSeq`는 5개이고, 모두 국토부 원본에서 시군구 코드가 잘못 붙은 소수 행(단지당 1~4건) 때문이다. 같은 거래가 두 구에 중복된 경우는 0. 예: 관악구 봉천동 `현대(관악)`(`11620-28`) 매매 338건 중 1건이 동작구 코드(`1159010100`=노량진동), `한진해모로`(`11140-1012`) 3건이 `1120016200`(lawd에 없는 조합) |
| 구가 갈린 단지 | 매매+전월세에서 시군구가 둘 이상인 `aptSeq`는 6개, 소수 쪽 75행. 위 5개 + `서동탄역양우내안애`(`41590-2250`, 능동 1287): 능동이 병점구·동탄구 양쪽에 있는 법정동이고, 전월세 2021-08~2023-11은 동탄구(263건), 2024-12 이후와 매매(14건)는 병점구로 온다. 단지 위치는 매매 기준이라 병점구. 전월세 263건은 거래 행대로 동탄구에 집계된다 |
| `aptSeq` 앞자리 ≠ 조회 시군구 | 매매 107,834행·전월세 342,696행. 위 5개와 미추홀구 1행(`28170-`)을 빼면 전부 개편 지역이 옛 코드 앞자리(`41590-`, `28260-`, `28110-`, `28140-`)를 쓰는 것. 문제 아님 |
| 한 지번에 여러 `aptSeq` | 지번(10자리+본번+부번) 17,706개는 `aptSeq` 1개, 176개는 2개, 최대 6개(남가좌동 385 등) |
| 한 단지가 `aptSeq` 여럿으로? | 같은 법정동·같은 `name_norm`인데 `aptSeq`가 여럿인 묶음 127개(단지 270개). 준공연도까지 같은 묶음은 63개(127개). 준공연도가 다른 64개 묶음은 이름만 같은 다른 건물(`다성이즈빌` 등). 같은 해 묶음 중 일부는 한 단지가 쪼개진 것으로 보인다: 대표 `aptSeq`가 매매·전월세를 모두 갖고 나머지는 전월세만 있다(`약수하이츠` `11140-37` 매매 389·전월세 1,548 + `11140-1506` 전월세 89, `이편한세상금호파크힐스` 1823 매매 311·전월세 1,881 + 1824 전월세 239). `킨텍스아이파크`처럼 지번마다 매매·전월세가 다 있는 경우도 있다 |
| 본번 NULL 단지 | 25개. 전부 전월세에만 나온 단지이고 지번이 `가-240`, `BL-1`, `산65-74`, 빈 값 같은 형식(택지 블록·산 지번) |
| 전월세 완전 중복 | 저장 값이 모두 같은 그룹 178,215개, 383,061행(한 그룹에 최대 35행). 1행만 남기면 204,846행(5.0%)이 줄어든다 |
| 전월세 계약정보 빈 행 | 계약구분·계약기간이 둘 다 빈 행 비율: 2021 45.6%, 2022 20.6%, 2023 18.0%, 2024 4.9%, 2025 3.1%, 2026 1.9%. 같은 거래(단지·면적·계약일·보증금·월세·층)에 빈 행과 채워진 행이 함께 있는 키 145,212개(빈 행 152,062) |

### 단지 위치 보정 (2026-09-25, 사용자 승인)

2단계 구현은 `lawd_cd5`를 처음 넣을 때 정하고 매매 지번 코드를 마지막 적재 행으로 덮어써서, 적재 순서에 따라 단지 위치가 틀렸다. 위 "단지 위치" 규칙(구현 메모)으로 바꾸고 개발 DB의 모든 단지를 다시 계산했다(약 26초). 9개가 바뀌었고, 다시 돌리면 0개.

| 단지 | 바뀐 값 | 원인 |
| --- | --- | --- |
| `11620-28` 현대(관악) | `lawd_cd5` `11590` → `11620` | 원본 시군구 코드 오류(동작구 코드로 온 1건이 먼저 적재) |
| `11320-87` 현대성우 | `lawd_cd5` `11305` → `11320` | 같음 |
| `11140-1012` 한진해모로 | `lawd_cd` `1120016200`(없는 코드) → `1114016200` | 같음(성동구 코드로 온 3건이 마지막에 적재) |
| `11230-2029` 샹그레빌아파트 | `lawd_cd` `1129010700` → `1123010700` | 같음 |
| `11590-3369` 관악푸르지오102동 | `lawd_cd` `1162010700` → `1159010700` | 같음(매매 `11590` 5 / `11620` 4) |
| `11740-4688` 푸르내 | `lawd_cd` 강일동 → 상일동 `1174010300` | 전월세 전용. 2021-09에 법정동이 바뀜(강일동 14건 → 상일동 278건) |
| `41220-3518` 평택고덕경기행복주택 | `lawd_cd` 고덕면 → 고덕동 `4122012800` | 전월세 전용. 고덕면 289 / 고덕동 3,143 |
| `41220-3820` 고덕국제신도시호반써밋3차더트리아츠 | 같음 | 고덕면 13 / 고덕동 48 |
| `41480-2328` 해링턴플레이스GTX운정물향기마을3단지 | `lawd_cd` 목동동 → 동패동 `4148011300` | 전월세 전용. 38 / 38 동률이라 최근 계약(동패동) |

보정 뒤: 모든 단지의 `lawd_cd5`가 그 단지 매매 다수 쪽과 같고, `left(lawd_cd, 5) = lawd_cd5`이고, `lawd_cd`가 전부 lawd에 있다.

## 4단계 확인 결과 (2026-09-25, 실제 호출)

규칙과 근거는 SPEC "건축물대장 부가정보"와 변경 이력 4단계에 있다. 여기는 그 밖의 관찰이다.

| 확인 항목 | 결과 |
| --- | --- |
| 표본 조사 | 무작위 300필지를 총괄표제부·표제부 모두 조회(`md5` 순서). 대장 있음 297, 사용승인연도가 있는 294필지 모두 실거래 건축년도와 같은 해 |
| 법정동 단위 총괄표제부 | 52개 법정동에서 필지 단위 조회 결과와 같음. 1개 동(`4159710900`)은 장애 구간에 걸려 비교 못 함(나중에 정상 응답 18건 확인) |
| 개편 지역 새 코드 | 서해구 `28275` 등 새 코드로 조회됨. 옛 코드(`28260…`)는 0건 |
| 산 지번 | 매매가 있는 산 지번 단지 37개. 대지로 조회하면 0건, 산으로 조회하면 금호2(쌍문동 산69-1)·신동아·수봉아파트B동 찾음, 세한숲속마을(산11-244)은 둘 다 0건 |
| 장애 구간 | 한동안 표제부는 5초 뒤 503, 총괄표제부는 빈 200만 옴. 1분 안팎 뒤 정상. 실거래가 API는 같은 시각 정상 |
| 서대문구 시험 실행 | 346회(총괄 17 + 표제부 329), 2분 33초, 503 재시도 6회(모두 1회로 회복), 실패 0. 필지 ok 324, not_found 5. 필지 합계를 쓰는 단지 19. 용적률 89.9%, 건폐율 90.2%, 주차 88.7%, 최고층·동수 100% |
| 사용승인연도 ≠ 건축년도(2년 이상) | 서대문구 4단지: 홍연(1981/1984), 북한산삼부르네상스 3개(2021/2024). 같은 건물로 보이지만 전체 실행 뒤 규모를 본다 |
| 조회 키 없음 | 67단지: 본번 NULL 25(택지 블록, 전월세에만 나온 산 지번 3) + 본번 `0000` 42 |
| 쿼터 사용 | 2026-09-25 건축HUB 약 1,900회(탐색·표본·fixture·서대문구 2회) |

## 5단계 결정과 확인 결과 (2026-09-25)

결정과 근거 수치는 SPEC 변경 이력 "Phase 2 착수 전 결정과 구현"에 있다(모두 사용자 승인). 요약:

1. aptSeq 매핑 테이블은 만들지 않는다
2. 전세 시세는 갱신 계약 제외, 완전 중복 유지, 월세 0만. 전세가율은 m2당 중위끼리
3. 재수집: 매일 3개월 + 주 1회 12개월 매매(`--recent 12`)
4. 이상치는 뷰에서 계산(m2당, 연도 칸 5건 이상). 직거래는 시세에서 뺀다(빈 값은 포함)
5. 임대: 시세용 매매(해제·직거래 제외)가 없는 단지 2,919개는 모든 통계에서 뺀다. 예외 목록 `db/danji_flag.csv`: 토지임대부 2(`land_lease`), 분양전환 52(`sale_conversion`, 후보 묶음 1+2 일괄 승인)
6. 회복률 `mv_danji_recovery` 구현. 7. 역세권은 보류(그대로)
- 정책(웹에서 확인, 출처는 `0007` 주석): 규제지역 서울 25 + 경기 15(2026-07-01 동탄·기흥·구리 추가), 규제지역 LTV 40%·서민실수요자 60%·비규제 70%, 주담대 상한 15억/25억 구간, 스트레스 금리 3.0%. 사용자는 미혼 무주택 세대주·연소득 8천이라 서민·실수요자에 해당, 생애최초는 아님
- 금리 4.5% + 스트레스 3.0%p 전부 반영(변동금리 기준, 보수적)은 가정값이다(사용자 승인)

| 확인 항목 | 결과 |
| --- | --- |
| 예산(기준값) | 규제지역 67,054 / 비규제 67,054, 둘 다 DSR(대출 38,138, 취득세 1,084). 서민·실수요자가 아니면 규제지역 49,100(LTV) |
| 예산 함수 결함(구현 중 발견, 고침) | 8억 서민 경계에서 막는 것은 LTV인데 대출을 정한 한도(DSR)로 답했다. `binding_factor`를 "1만원 비싼 집을 막는 한도"로 바꾸고 `binding_detail` 추가 |
| 분석 범위 | `full` 17,803 / `danji_only` 2 / `excluded` 2,919 |
| 시세에 쓰는 매매 | 1,155,313행 중 1,047,579(`is_basis`) |
| 이상치 비율(해제 제외) | 직거래 0.068%, 중개 0.001%, 빈 값 0.024%. 층 0 이하 0.002~0.006% |
| 회복률 | 전고점 있음 11,144, 회복률 있음 10,154(표본 3건 이상 8,177). 중위 91.6%, 저점 중위 78.0%. 시군구 M 중위: 분당 131.4, 서초 130.1, 강남 129.1, 송파 126.2, 서대문 109.9, 노원 92.7, 도봉 87.8, 부평 82.9 |
| 전세가율(양쪽 표본 3건 이상 7,405칸) | 10/25/50/75/90% = 43.1/53.7/66.7/76.5/83.3 |
| 토지임대부 효과 | `강남브리즈힐` 강남 M 가격 분위 8.2·회복률 분위 0 → 분위·비교 뷰에서 빠짐 |
| `mv_*` 행 수 | price_monthly 491,755 / latest 21,708 / region_monthly 26,191 / percentile 19,396 / recovery 20,389 |
| 조회 속도 | `v_*`를 단지로 좁히면 수십 ms. 시군구로 좁혀도 `v_danji_percentile` 18초, `v_danji_recovery` 29초(전체를 계산). MCP는 `mv_*`만 읽는다 |
| 관찰 | `서울역센트럴자이`(`11140-1300`, 1,341세대)는 2021-01~11 매매가 0건이다(같은 필지에 다른 aptSeq 없음). 분양전환 후보 묶음 2에 걸려 `sale_conversion`으로 올라갔지만 첫 매매가 2022-01이라 현재 시세에는 영향 없음 |

## 이후 단계 메모

- **건축물대장 전체 실행 뒤 점검(4단계 마무리)**: `--report`로 (1) 부가정보 없는 단지 비율이 5% 이하인지 (2) `no_households` 규모. 대부분 도시형생활주택(세대수 0, 호수만 있음)이면 호수를 세대수 대신 쓸지 정한다. 호수는 총괄표제부에서 상가 호수로도 쓰이니 주거동에 한정해야 한다 (3) 사용승인연도 ≠ 건축년도 단지가 엉뚱한 건물인지 (4) `error` 필지. 결과를 이 파일과 SPEC 변경 이력에 적는다
- **cron에 건축물대장 추가(배포 때)**: 실거래 수집이 새 단지를 만들면 `danji` 부가정보가 NULL이다. 수집 뒤 `python -m etl.bldg`를 돌리면 새 필지만 받는다. SPEC "cron 구성"에는 아직 없다
- **전월세에만 나온 산 지번 단지 3개**: `split_jibun`이 `산22-36`을 읽지 않아 본번이 NULL이다(강동리엔파크 9·13단지, 일원상도푸르지오클라베뉴). 읽게 하면 `plat_gb_of`로 산 조회가 된다. 수가 적어 미뤘다
- **법정동 코드**: 개편 8개 지역의 `lawd_cd`는 새 코드(예: 서해구 `2827510100`)이고 건축HUB도 새 코드로 조회된다(4단계 확인). 행정구역이 또 바뀌면 행안부 공지의 새 `jscode…(말소코드포함).zip`으로 `lawd`를 다시 적재하고 `etl/regions.py`를 맞춘다(`tests/test_regions.py`가 둘의 차이를 잡는다)
- **`get_recent_trades`의 id**: 행 값이 바뀌면(등기일 추가, 해제) 그 거래의 `id`가 바뀐다. 근거 추적은 id보다 계약일·층·금액·면적으로 보여 준다
- **건축물대장 error 2건**: 용적률 원문이 `NUMERIC(6,2)`를 넘는다(249,024.56 등). 원문 오류로 보이는데, 범위 밖 값을 NULL로 둘지 전체 실행 뒤 점검 때 정한다
- **예외 목록 갱신**: 새 수집으로 분양전환 단지가 생긴다. 가끔 `etl.flags --candidates`를 다시 돌려 새 후보를 확인받는다. 묶음 3(월 3~5건, 11곳)은 근거가 약해 올리지 않았다
- **정책이 바뀌면**: `0007`을 고치지 말고 새 시행일 행을 넣는 마이그레이션을 추가하고 `etl.refresh`는 필요 없다(예산 함수는 조회 시점에 읽는다). 규제지역 지정·해제도 같다. 토지거래허가구역은 2026-12-31(동탄·기흥·구리 2027-12-31)에 끝나므로 연장 여부를 확인한다

## 6단계 결정과 확인 결과 (2026-09-25)

결정과 근거 수치는 SPEC 변경 이력 "6단계"에 있다. 요약:

1. `search_candidates`는 예산을 모델에게서 받지 않고 `max_purchase_price_for_group`으로 시군구 규제 여부·평형그룹마다 계산한다. `max_price_manwon`은 사용자 상한(사용자 승인)
2. `find_danji` 추가(사용자 승인)
3. 기본 정렬은 같은 시군구·평형그룹 안 가격 분위 낮은 순(사용자 승인). 표본 3건 미만은 빼지 않고 뒤로
4. 세대수 모르는 단지는 `min_households` > 0이면 빼고 `meta.counts.excluded_unknown_households`에 센다
5. `get_recent_trades`: 해제 기본 제외, `kind`·`area_group`·`since`·`include_canceled` 추가
6. 계산은 `0008`의 뷰·함수, 서버는 조회만. 읽기 전용 계정(사용자 승인으로 Claude가 생성)

| 확인 항목 | 결과 |
| --- | --- |
| 읽기 전용 계정 | `trade_sale`·`trade_rent`·`ingest_log`·`bldg_title`·`danji_flag` SELECT 거부, INSERT·UPDATE·`REFRESH`·`CREATE` 거부, superuser 아님 |
| 근거 추적(SPEC 체크리스트) | DMC파크뷰자이1단지 S·M·XL: `get_recent_trades(since=기간 시작일)`의 `is_basis` 건수와 중위가 = `get_danji`의 `sample_size`·`median_price_manwon` |
| `binding_factor` 규제/비규제(체크리스트) | 서민·실수요자가 아니면 규제지역 LTV, 비규제지역 DSR |
| `partial`·`low_confidence`(체크리스트) | 가격 시계열의 기준월 포함 3개월이 `partial`, 표본 2건 칸이 `low_confidence` |
| stdio 실행 | 환경변수 없이 다른 디렉터리에서 `python -m mcp_server`를 서브프로세스로 띄워 도구 호출 성공(`.env`에서 계정을 읽음) |
| 기본 조건 전체 검색 | 검토 15,853칸 = 최근 거래 부족 1,793 + 예산 초과 5,169 + 세대수 모름 7,314 + 세대수 300 미만 1,293 + 후보 284(그중 표본 부족 21). 후보는 사실상 서울뿐이다(인천·경기 건축물대장 미수집) |
| 조회 속도 | 83개 전체 후보 검색 0.9초, 2개 구 0.2초, `get_danji` 30ms, `find_danji` 0.3초, `compare_danji` 0.7초, 단지 원시 거래 20ms, 시군구 원시 거래 매매 1.6초·전월세 0.8초, `get_region_stats` 15ms |
| 응답 크기 | `search_candidates` 50행 약 30KB, `get_danji` 약 6KB, `get_region_stats`(S·M, 24개월) 약 15KB |
| 관찰 | 기본 정렬 상위: `서초이오빌`(서초 S 4억, 43m2, 분위 0), `신동아`(영등포 대림 S), `대광`(성북 안암 1971년 S 1.8억, 회복률 65.8%). 분위 0은 같은 구 S 그룹 안에서 m2당가가 가장 낮다는 뜻이라 소형 면적·노후 단지가 위로 온다. 싼 이유를 보는 것은 모델 몫이다 |

## 7단계 결정과 확인 결과 (2026-09-25)

결정은 SPEC 변경 이력 "7단계"에 있다. 요약: NAS 컨테이너 28001 + 공인 IP 도메인, 고정 토큰(OAuth 안 씀), Streamable HTTP `/mcp` stateless + JSON, Host 헤더 검사 끔.

| 확인 항목 | 결과 |
| --- | --- |
| 인증 | 토큰 없음·틀림·접두어 없음·`Basic`·한 글자 차이 모두 401. `Bearer`(대소문자 무관)·`X-API-Key` 통과. `/healthz`만 열림 |
| 공개 도메인 Host | `Host: apartment-mcp.movingjin.com`으로 200(SDK 기본값이면 421) |
| HTTP 경유 도구 호출 | MCP 클라이언트(`Authorization` 헤더)로 도구 9개 목록, `search_candidates`·`compute_budget` 성공, 없는 단지는 오류 메시지 |
| 이미지 내용 재현 | 빈 가상환경에 `mcp_server/requirements.txt`만 설치, `etl` 세 파일 + `mcp_server/`만 복사, `.env` 없이 환경변수만으로 인증·도구 호출 성공. Docker 빌드 자체는 이 PC에 Docker가 없어 NAS에서 처음 한다 |
| 공개 주소 | 도메인은 211.222.168.70으로 풀리지만 NAS 안에서 80·443 연결 시간 초과(NAT 루프백 없음으로 보임). 외부망 확인 필요 |
| claude.ai 커넥터 조건 | Anthropic 클라우드 `160.79.104.0/21`에서 접속. 인증은 OAuth(DCR·CIMD) 또는 요청 헤더(베타). Pro 화면에 "로그인 없음 + 요청 헤더(최대 4개)"가 있다 |

남은 것
- 배포 뒤 외부망 `/healthz`, 커넥터 저장, 실제 대화로 도구 선택·응답 크기 확인
- 필요하면 방화벽에서 28001(또는 443 뒤 프록시)을 `160.79.104.0/21`로만 열어 한 겹 더 막는다(토큰과 별개)
- 건축물대장 전체 조회가 끝나면 인천·경기 후보가 나온다. 그 전까지 이 지역은 `min_households=0`으로 불러야 보인다(응답 `meta.notes`가 알려 준다)

## 참고 수치

- 서대문구 2025-06: 매매 578건(해제 77, 직거래 14), 전월세 639건, 단지 151개(매매에 나온 106 + 전월세에만 나온 45), 매매·전월세 공통 `aptSeq` 75개. 전용 60.00 거래 9건
- 양평군(`41830`) 2025-06: 매매 88건, 전월세 84건. 리 단위 `umdNm` 예: `양평읍 양근리` = `4183025021`
