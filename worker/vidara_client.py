"""Vidara upload client."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

INFO_URL = "https://api.vidara.so/v1/video/info"
PENDING_STATUSES = frozenset({"waiting", "in progress", "processing", "pending", "unknown"})


def full_wait_timeout_sec(size_mb: float) -> int:
    """Scale wait with file size; cap for CI job limits."""
    return int(min(2400, max(420, size_mb * 45 + 300)))


def pre_wait_timeout_sec() -> int:
    return 900


def log_account_info(api_key: str) -> None:
    try:
        res = httpx.get(
            "https://api.vidara.so/v1/user/info",
            params={"api_key": api_key},
            timeout=20,
        )
        data = (res.json() or {}).get("result") or {}
        print(
            f"vidara: storage_used={data.get('storage_used')} "
            f"videos_total={data.get('videos_total')}"
        )
    except Exception as exc:
        print(f"vidara: account info skipped: {exc}")


def probe_upload_server(api_key: str) -> None:
    """Fail fast before large downloads if Vidara upload slot is unavailable."""
    try:
        res = httpx.get(
            "https://api.vidara.so/v1/upload/server",
            params={"api_key": api_key},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Vidara probe failed: {exc}") from exc
    if res.status_code == 429:
        raise RuntimeError(f"Vidara rate limited (429): {res.text[:200]}")
    res.raise_for_status()
    upload_server = (res.json().get("result") or {}).get("upload_server")
    if not upload_server:
        raise RuntimeError("Vidara upload_server missing")


def normalize_filecode(filecode: str) -> str:
    value = filecode.strip()
    if "/e/" in value:
        return value.rsplit("/e/", 1)[-1].strip("/")
    return value


def _parse_info_status(data: dict) -> str:
    if data.get("status") not in (200, "200"):
        return "missing"
    result = data.get("result")
    if isinstance(result, list) and result:
        entry = result[0] if isinstance(result[0], dict) else {}
        return str(entry.get("status") or "unknown").lower()
    return "unknown"


def get_file_status(api_key: str, filecode: str) -> str:
    fc = normalize_filecode(filecode)
    if not fc:
        return "missing"
    try:
        res = httpx.get(
            INFO_URL,
            params={"api_key": api_key, "filecode": fc},
            timeout=30.0,
        )
        if res.status_code == 403:
            return "missing"
        res.raise_for_status()
        return _parse_info_status(res.json())
    except Exception:
        return "unknown"


def wait_for_active(
    api_key: str,
    filecode: str,
    *,
    label: str = "video",
    timeout_sec: int = 300,
    poll_sec: int = 10,
) -> None:
    fc = normalize_filecode(filecode)
    deadline = time.time() + timeout_sec
    last = "unknown"
    while time.time() < deadline:
        last = get_file_status(api_key, fc)
        print(f"vidara: {label} {fc} status={last}")
        if last == "active":
            return
        if last == "error":
            raise RuntimeError(f"Vidara {label} {fc} status=error")
        if last == "missing":
            raise RuntimeError(f"Vidara {label} {fc} not found")
        if last not in PENDING_STATUSES:
            print(f"vidara: {label} {fc} unexpected status={last}, keep polling")
        time.sleep(poll_sec)
    raise RuntimeError(f"Vidara {label} {fc} not active within {timeout_sec}s (last={last})")


def upload_by_url(api_key: str, file_url: str) -> str:
    """Do not use for ebalka — signed URLs expire before Vidara fetches them."""
    res = httpx.get(
        "https://api.vidara.so/v1/upload/url",
        params={"api_key": api_key, "url": file_url},
        timeout=300.0,
    )
    res.raise_for_status()
    data = res.json()
    if data.get("status") != 200:
        raise RuntimeError(f"Vidara URL upload failed: {data}")
    filecode = (data.get("data") or {}).get("filecode")
    if not filecode:
        raise RuntimeError(f"Vidara missing filecode: {data}")
    return normalize_filecode(str(filecode))


def upload_file(api_key: str, file_path: str) -> str:
    server_res = httpx.get(
        "https://api.vidara.so/v1/upload/server",
        params={"api_key": api_key},
        timeout=30.0,
    )
    server_res.raise_for_status()
    upload_server = (server_res.json().get("result") or {}).get("upload_server")
    if not upload_server:
        raise RuntimeError("Vidara upload_server missing")

    path = Path(file_path)
    with path.open("rb") as handle:
        res = httpx.post(
            upload_server,
            data={"api_key": api_key, "key": api_key},
            files={"file": (path.name, handle, "video/mp4")},
            timeout=600.0,
        )
    res.raise_for_status()
    data = res.json()
    filecode = data.get("filecode")
    if not filecode:
        raise RuntimeError(f"Vidara file upload failed: {data}")
    return normalize_filecode(str(filecode))


def upload_full_file(api_key: str, source_url: str, dest_path: str, download_fn) -> str:
    """Download and upload full mp4 — caller waits for active separately."""
    path = Path(dest_path)
    download_fn(source_url, path)
    return upload_file(api_key, str(path))


def embed_url(filecode: str) -> str:
    return f"https://vidara.so/e/{normalize_filecode(filecode)}"
