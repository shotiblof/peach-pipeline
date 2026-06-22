"""Local smoke tests — ebalka URL, download, Vidara status. No DB writes."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import httpx

from ebalka_parser import HEADERS, extract_mp4_url, _fetch as fetch_html, parse_cards
from peach_db import abs_url
from vidara_client import get_file_status, upload_file, wait_for_active


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def test_ebalka_fetch_and_download(origin: str = "https://a.ebalka.love") -> tuple[str, Path]:
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        list_html = fetch_html(client, abs_url(origin, "/latest-videos/1/"))
        cards = parse_cards(list_html, origin)
        if not cards:
            raise RuntimeError("No cards on ebalka list page")
        card = cards[0]
        page_html = fetch_html(client, abs_url(origin, card.video_path))
        mp4_url = extract_mp4_url(page_html)
        if not mp4_url:
            raise RuntimeError(f"No mp4 on page {card.video_path}")
        print(f"ok ebalka: id={card.id} mp4={mp4_url[:80]}...")

        dest = Path(tempfile.gettempdir()) / f"peach-test-{card.id}.mp4"
        with client.stream("GET", mp4_url, timeout=httpx.Timeout(30.0, read=300.0)) as res:
            res.raise_for_status()
            size = 0
            with dest.open("wb") as handle:
                for chunk in res.iter_bytes():
                    handle.write(chunk)
                    size += len(chunk)
        if size < 100_000:
            raise RuntimeError(f"Download too small ({size} bytes)")
        print(f"ok download: {dest} ({size / (1024 * 1024):.1f} MB)")
        return card.id, dest


def test_vidara_pre_upload(mp4_path: Path, api_key: str) -> str:
    import subprocess

    pre_path = mp4_path.with_name(f"{mp4_path.stem}-pre-test.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(mp4_path),
            "-t",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(pre_path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    print(f"ok ffmpeg pre clip: {pre_path.stat().st_size / 1024:.0f} KB")
    fc = upload_file(api_key, str(pre_path))
    print(f"ok vidara upload: filecode={fc} status={get_file_status(api_key, fc)}")
    wait_for_active(api_key, fc, label="pre-test", timeout_sec=1200, poll_sec=15)
    pre_path.unlink(missing_ok=True)
    return fc


def main() -> int:
    _load_env()
    api_key = os.environ.get("VIDARA_API_KEY", "").strip()
    if not api_key:
        print("VIDARA_API_KEY missing", file=sys.stderr)
        return 1

    origin = os.environ.get("EBALKA_SOURCE_ORIGIN", "https://a.ebalka.love").strip()
    vid, mp4_path = test_ebalka_fetch_and_download(origin)

    if "--vidara" in sys.argv:
        test_vidara_pre_upload(mp4_path, api_key)

    mp4_path.unlink(missing_ok=True)
    print(f"all local checks passed (video {vid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
