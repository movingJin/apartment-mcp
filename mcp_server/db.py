"""MCP 서버의 DB 접속. 읽기 전용 계정(MCP_DATABASE_URL)만 쓴다.

- ETL용 DATABASE_URL(superuser)로 넘어가지 않는다. MCP_DATABASE_URL이 없으면 시작하지 않는다
- 환경변수에 없으면 저장소 루트의 .env에서 읽는다(Claude Code가 서버를 띄울 때는 셸의 .env를 거치지 않는다)
- 도구 호출 하나가 읽기 전용 트랜잭션 하나다(REPEATABLE READ). 그 사이에 etl.refresh가 끝나도
  한 응답 안의 mv_*는 같은 스냅숏이다
"""

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
STATEMENT_TIMEOUT = "30s"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 줄만 읽는다. 따옴표·변수 치환은 없다(.env에 쓰지 않는다)."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def database_url() -> str:
    url = os.environ.get("MCP_DATABASE_URL") or read_env_file(ENV_FILE).get("MCP_DATABASE_URL")
    if not url:
        raise RuntimeError("MCP_DATABASE_URL이 없다. 읽기 전용 계정 접속 문자열을 환경변수나 .env에 둔다(PROGRESS.md)")
    return url


def connect() -> psycopg.Connection:
    conn = psycopg.connect(
        database_url(),
        row_factory=dict_row,
        application_name="apartment-mcp",
        options=f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT}",
    )
    conn.read_only = True
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    return conn
