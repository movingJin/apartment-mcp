import os

import psycopg


def connect(**kwargs) -> psycopg.Connection:
    """DATABASE_URL로 접속한다. 없으면 libpq 표준 환경변수(PGHOST, PGUSER 등)를 따른다."""
    return psycopg.connect(os.environ.get("DATABASE_URL", ""), **kwargs)
