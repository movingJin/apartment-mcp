# apartment-mcp

수도권(서울·인천·경기 83개 시군구) 아파트 실거래 DB(2021-01~)와 이를 노출하는 MCP 서버. 자세한 사양은 [SPEC.md](SPEC.md), 진행 상황은 [PROGRESS.md](PROGRESS.md) 참고.

## 준비

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # SERVICE_KEY, POSTGRES_PASSWORD, DATABASE_URL, MCP_DATABASE_URL, MCP_API_TOKEN 채우기
```

- `SERVICE_KEY`: 공공데이터포털 디코딩 키
- `DATABASE_URL`: ETL이 쓰는 접속 문자열 (쓰기 권한)
- `MCP_DATABASE_URL`: MCP 서버 전용 읽기 전용 계정
- `MCP_API_TOKEN`: HTTP 모드에서 쓰는 Bearer 토큰 (`python -c "import secrets; print(secrets.token_urlsafe(32))"`로 생성)

DB는 최초 1회 스키마 적용과 법정동 마스터 적재가 필요하다.

```bash
.venv/bin/python -m etl.migrate
.venv/bin/python -m etl.lawd KIKcd_B.20260701_말소코드포함.txt
```

## 수집 스크립트 실행

```bash
# 실거래가 (국토부)
.venv/bin/python -m etl.collect --mode full --dry-run   # 받을 구간 수만 확인, API 호출 없음
.venv/bin/python -m etl.collect --mode full              # 83개 시군구 × 2021-01~현재월 전체 수집
.venv/bin/python -m etl.collect --mode incremental        # 최근 3개월만 (cron용)

# 건축물대장 (건축HUB) 부가정보
.venv/bin/python -m etl.bldg --dry-run
.venv/bin/python -m etl.bldg

# 파생 지표(평당가·전세가율 등) 뷰 재계산 — 수집 뒤 반드시 실행
.venv/bin/python -m etl.refresh
```

오래 걸리는 전체 수집은 백그라운드로 돌리고 로그를 남긴다.

```bash
nohup .venv/bin/python -m etl.collect --mode full > logs/collect_full.log 2>&1 &
```

테스트: `.venv/bin/python -m pytest` (DATABASE_URL·MCP_DATABASE_URL이 없으면 DB 관련 테스트는 건너뜀)

## 로컬에서 빌드·실행 (MCP 서버)

빌드는 따로 없고 그대로 실행한다.

```bash
# stdio (Claude Code가 .mcp.json으로 띄우는 기본 방식)
.venv/bin/python -m mcp_server

# HTTP (로컬 확인용)
.venv/bin/python -m mcp_server --http --port 28011
```

## Docker로 빌드·실행 (MCP 서버, HTTP)

이미지에는 `mcp_server/`와 ETL 중 단지명 정규화·대상 시군구에 쓰이는 `etl/records.py`, `etl/regions.py`만 들어간다(`.dockerignore` 참고). DB 접속 문자열과 토큰은 `.env`에서 compose가 읽는다.

```bash
docker compose up -d --build mcp
curl http://127.0.0.1:28001/healthz   # ok
```

필요하면 로컬 PostgreSQL도 같이 띄울 수 있다 (기본은 꺼져 있음).

```bash
docker compose --profile local-db up -d db
```

## 배포

`main`에 push하면 GitHub Actions(`.github/workflows/deploy-prod.yml`)가 테스트 → 이미지 스모크 테스트 → NAS 전송 → `mcp` 컨테이너 재기동까지 수행한다. NAS는 공개 HTTPS로 claude.ai 커넥터에 연결된다. 수동 실행은 Actions 탭에서 가능.
