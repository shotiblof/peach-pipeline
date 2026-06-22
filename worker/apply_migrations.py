"""Apply idempotent SQL migrations from db/migrations/."""
from __future__ import annotations

from pathlib import Path

from peach_db import db_conn

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "db" / "migrations").glob("*.sql"))


def apply_migrations() -> None:
    if not MIGRATIONS:
        print("migrations: none")
        return
    with db_conn() as conn:
        for path in MIGRATIONS:
            sql = path.read_text(encoding="utf-8").strip()
            if not sql:
                continue
            conn.execute(sql)
            print(f"migrations: applied {path.name}")
        conn.commit()


if __name__ == "__main__":
    apply_migrations()
