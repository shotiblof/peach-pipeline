"""DoodStream upload API — fallback when Vidara is rate-limited."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

API_BASE = "https://doodapi.co/api"
MIN_REQUEST_GAP_SEC = 0.12  # API limit: 10 req/s


class DoodStreamError(RuntimeError):
    pass


_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    now = time.monotonic()
    wait = MIN_REQUEST_GAP_SEC - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _api_get(path: str, *, api_key: str, params: dict | None = None) -> dict:
    _throttle()
    query = {"key": api_key, **(params or {})}
    res = httpx.get(f"{API_BASE}{path}", params=query, timeout=60.0)
    if res.status_code == 429:
        raise DoodStreamError("DoodStream 429 rate limit")
    res.raise_for_status()
    data = res.json()
    status = data.get("status")
    if status not in (200, "200"):
        raise DoodStreamError(f"DoodStream API error: {data}")
    return data


def log_account_info(api_key: str) -> None:
    try:
        data = _api_get("/account/info", api_key=api_key)
        result = data.get("result") or {}
        print(
            "doodstream: storage_used="
            f"{result.get('storage_used')} storage_left={result.get('storage_left')}"
        )
    except Exception as exc:
        print(f"doodstream: account info skipped: {exc}")


def normalize_filecode(filecode: str) -> str:
    value = (filecode or "").strip()
    if "/e/" in value:
        return value.rsplit("/e/", 1)[-1].strip("/")
    return value


def get_upload_server(api_key: str) -> str:
    data = _api_get("/upload/server", api_key=api_key)
    server = data.get("result")
    if not isinstance(server, str) or not server.startswith("http"):
        raise DoodStreamError(f"DoodStream upload server missing: {data}")
    return server


def upload_file(api_key: str, file_path: str) -> str:
    server = get_upload_server(api_key)
    path = Path(file_path)
    _throttle()
    with path.open("rb") as handle:
        res = httpx.post(
            server,
            data={"api_key": api_key},
            files={"file": (path.name, handle, "video/mp4")},
            timeout=900.0,
        )
    if res.status_code == 429:
        raise DoodStreamError("DoodStream upload 429 rate limit")
    res.raise_for_status()
    data = res.json()
    status = data.get("status")
    if status not in (200, "200"):
        raise DoodStreamError(f"DoodStream upload failed: {data}")
    result = data.get("result")
    entry: dict | None = None
    if isinstance(result, list) and result:
        entry = result[0] if isinstance(result[0], dict) else None
    elif isinstance(result, dict):
        entry = result
    filecode = ""
    if entry:
        filecode = str(entry.get("filecode") or entry.get("file_code") or "")
    if not filecode:
        raise DoodStreamError(f"DoodStream missing filecode: {data}")
    return normalize_filecode(filecode)


def get_file_status(api_key: str, filecode: str) -> str:
    fc = normalize_filecode(filecode)
    if not fc:
        return "missing"
    try:
        data = _api_get("/file/check", api_key=api_key, params={"file_code": fc})
        rows = data.get("result") or []
        if isinstance(rows, list) and rows:
            return str(rows[0].get("status") or "unknown").lower()
    except Exception:
        return "unknown"
    return "missing"


def embed_url(filecode: str) -> str:
    fc = normalize_filecode(filecode)
    return f"https://dood.la/e/{fc}" if fc else ""
