"""Catalog parser entry: hiden.live first, then ebalka when hiden backlog is done."""
from __future__ import annotations

import logging

from peach_db import db_conn, get_setting
from queue_guard import parser_ingest_allowed

logger = logging.getLogger(__name__)


def _hiden_active(conn) -> bool:
    primary = get_setting(conn, "parser.primary_source", "hiden").strip().lower()
    if primary not in ("hiden", "auto"):
        return False
    row = conn.execute(
        "SELECT hiden_complete FROM parser_state WHERE id = 1"
    ).fetchone()
    return not bool(row and row.get("hiden_complete"))


def run_parser() -> int:
    with db_conn() as conn:
        use_hiden = _hiden_active(conn)

    if use_hiden:
        from hiden_parser import run_hiden_parser

        with db_conn() as conn:
            if not parser_ingest_allowed(conn):
                return 0

        try:
            processed = run_hiden_parser()
        except Exception as exc:  # noqa: BLE001
            # GitHub-hosted runner IPs may be blocked; use HIDEN_FETCH_PROXY when set.
            logger.warning("hiden parser failed (will not switch to ebalka yet): %s", exc)
            processed = 0
        if processed > 0:
            return processed
        with db_conn() as conn:
            if _hiden_active(conn):
                return 0

    from ebalka_parser import run_parser as run_ebalka_parser

    with db_conn() as conn:
        if not parser_ingest_allowed(conn):
            return 0

    return run_ebalka_parser()


if __name__ == "__main__":
    run_parser()
