"""db/migrations/*.sql을 파일명 순서대로 적용한다.

적용한 파일은 schema_migrations에 기록하고 다시 실행하지 않는다. 파일 하나가 트랜잭션 하나다.

    python -m etl.migrate
"""

from pathlib import Path

import psycopg

from etl.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    newly_applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
        newly_applied.append(path.name)
    return newly_applied


def main() -> None:
    with connect(autocommit=True) as conn:
        newly_applied = migrate(conn)
    if newly_applied:
        for name in newly_applied:
            print(f"applied {name}")
    else:
        print("already up to date")


if __name__ == "__main__":
    main()
