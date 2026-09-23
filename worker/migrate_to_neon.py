"""Create Neon project (if needed) and copy data from Render Postgres."""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

NEON_API = "https://console.neon.tech/api/v2"
REGION = "aws-eu-central-1"
PROJECT_NAME = "peach"
DEFAULT_ORG_ID = "org-billowing-resonance-09312870"
DEFAULT_PROJECT_ID = ""

TABLES = [
    "app_settings",
    "upload_accounts",
    "parser_state",
    "videos",
]


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def neon_headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def wait_neon_operations(api_key: str, project_id: str, timeout_sec: int = 120) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        res = httpx.get(
            f"{NEON_API}/projects/{project_id}/operations",
            headers=neon_headers(api_key),
            timeout=30.0,
        )
        res.raise_for_status()
        ops = res.json().get("operations") or []
        pending = [op for op in ops if op.get("status") not in ("finished", "failed", "cancelled")]
        if not pending:
            return
        time.sleep(2)
    raise RuntimeError("Neon operations timed out")


def find_existing_project(api_key: str) -> dict[str, Any] | None:
    org_id = os.environ.get("NEON_ORG_ID", "").strip() or DEFAULT_ORG_ID
    res = httpx.get(
        f"{NEON_API}/projects",
        headers=neon_headers(api_key),
        params={"org_id": org_id},
        timeout=30.0,
    )
    res.raise_for_status()
    for project in res.json().get("projects") or []:
        if project.get("name") == PROJECT_NAME:
            return project
    return None


def resolve_neon_project(api_key: str) -> tuple[str, str]:
    project_id = os.environ.get("NEON_PROJECT_ID", "").strip()
    if project_id:
        print(f"neon: use project {project_id}")
        return project_id, fetch_connection_uri(api_key, project_id)

    existing = find_existing_project(api_key)
    if existing:
        project_id = str(existing["id"])
        print(f"neon: reuse project {existing.get('name', PROJECT_NAME)} ({project_id})")
        return project_id, fetch_connection_uri(api_key, project_id)

    return create_neon_project(api_key)


def fetch_connection_uri(api_key: str, project_id: str) -> str:
    conn_res = httpx.get(
        f"{NEON_API}/projects/{project_id}/connection_uri",
        headers=neon_headers(api_key),
        params={"database_name": "neondb", "role_name": "neondb_owner"},
        timeout=30.0,
    )
    conn_res.raise_for_status()
    database_url = str(conn_res.json()["uri"])
    if "sslmode=" not in database_url:
        sep = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{sep}sslmode=require"
    return database_url


def create_neon_project(api_key: str) -> tuple[str, str]:
    org_id = os.environ.get("NEON_ORG_ID", "").strip() or DEFAULT_ORG_ID
    res = httpx.post(
        f"{NEON_API}/projects",
        headers=neon_headers(api_key),
        json={
            "project": {
                "name": PROJECT_NAME,
                "pg_version": 16,
                "region_id": REGION,
                "org_id": org_id,
            }
        },
        timeout=60.0,
    )
    res.raise_for_status()
    body = res.json()
    project = body["project"]
    project_id = str(project["id"])
    print(f"neon: created project {PROJECT_NAME} ({project_id})")
    wait_neon_operations(api_key, project_id)
    uri = body.get("connection_uris", [{}])[0].get("connection_uri")
    if uri:
        database_url = str(uri)
        if "sslmode=" not in database_url:
            sep = "&" if "?" in database_url else "?"
            database_url = f"{database_url}{sep}sslmode=require"
        return project_id, database_url
    return project_id, fetch_connection_uri(api_key, project_id)


def table_exists(conn: psycopg.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL AS ok",
        (f"public.{table}",),
    ).fetchone()
    return bool(row and row[0])


def apply_schema(target_url: str, schema_path: str) -> None:
    schema = open(schema_path, encoding="utf-8").read()
    with psycopg.connect(target_url) as conn:
        conn.execute(schema)
        conn.commit()
    print("neon: applied schema")


def apply_schema_if_needed(target_url: str, schema_path: str) -> None:
    with psycopg.connect(target_url) as conn:
        if table_exists(conn, "videos"):
            print("neon: schema already present, skip apply")
            return
    apply_schema(target_url, schema_path)


def adapt_value(value: object) -> object:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def copy_table(source: psycopg.Connection, target: psycopg.Connection, table: str) -> int:
    rows = source.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        return 0
    columns = list(rows[0].keys())
    cols_sql = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    target.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
    for row in rows:
        target.execute(
            f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})",
            tuple(adapt_value(row[col]) for col in columns),
        )
    return len(rows)


def migrate_data(source_url: str, target_url: str) -> None:
    with (
        psycopg.connect(source_url, row_factory=dict_row) as source,
        psycopg.connect(target_url, row_factory=dict_row) as target,
    ):
        for table in TABLES:
            count = copy_table(source, target, table)
            target.commit()
            print(f"neon: copied {table} ({count} rows)")


def main() -> int:
    api_key = require_env("NEON_API_KEY")
    source_url = require_env("SOURCE_DATABASE_URL")
    schema_path = os.environ.get("SCHEMA_PATH", "db/schema.sql")

    _, target_url = resolve_neon_project(api_key)
    apply_schema_if_needed(target_url, schema_path)
    migrate_data(source_url, target_url)

    print("NEON_DATABASE_URL=" + target_url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"migrate_to_neon: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
