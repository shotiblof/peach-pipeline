#!/usr/bin/env python3
"""Update DATABASE_URL on Render peach-api after Neon migration."""
from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    service_id = os.environ.get("RENDER_API_SERVICE_ID", "").strip()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not api_key or not service_id or not database_url:
        print("RENDER_API_KEY, RENDER_API_SERVICE_ID, DATABASE_URL required", file=sys.stderr)
        return 1

    res = httpx.put(
        f"https://api.render.com/v1/services/{service_id}/env-vars/DATABASE_URL",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"value": database_url},
        timeout=30.0,
    )
    if res.status_code >= 400:
        print(res.text, file=sys.stderr)
        return 1
    print("render: DATABASE_URL updated, redeploy peach-api manually or via deploy hook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
