"""Catalog parser entry — ebalka only."""
from __future__ import annotations

from queue_guard import parser_ingest_allowed
from peach_db import db_conn


def run_parser() -> int:
    with db_conn() as conn:
        if not parser_ingest_allowed(conn):
            return 0

    from ebalka_parser import run_parser as run_ebalka_parser

    return run_ebalka_parser()


if __name__ == "__main__":
    run_parser()
