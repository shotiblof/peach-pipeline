"""Smoke-test DoodStream API (no upload unless DOOD_TEST_UPLOAD=1)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k] = v

from doodstream_client import get_file_status, get_upload_server, log_account_info, upload_file


def main() -> int:
    key = os.environ.get("DOODSTREAM_API_KEY", "").strip()
    if not key:
        print("DOODSTREAM_API_KEY missing")
        return 1

    log_account_info(key)
    server = get_upload_server(key)
    print(f"upload_server={server}")

    if os.environ.get("DOOD_TEST_UPLOAD") != "1":
        print("skip upload (set DOOD_TEST_UPLOAD=1 to upload tiny file)")
        return 0

    tiny = Path(tempfile.gettempdir()) / "dood-test.mp4"
    if not tiny.exists():
        print("create a tiny mp4 at", tiny, "or skip upload test")
        return 1

    fc = upload_file(key, str(tiny))
    print(f"uploaded filecode={fc} status={get_file_status(key, fc)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
