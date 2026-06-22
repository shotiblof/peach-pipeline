"""Reset broken / stale catalog rows so uploader can republish cleanly."""
from __future__ import annotations

import os

from peach_db import db_conn, get_upload_account
from preview_refresh import purge_stale_previews
from queue_guard import rebalance_upload_queue
from upload_recovery import recover_pending_uploads
from vidara_client import PENDING_STATUSES, get_file_status as vidara_file_status


def _namevids_only_enabled() -> bool:
    return os.environ.get("UPLOADER_NAMEVIDS_ONLY", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _pre_is_broken(api_key: str, filecode: str | None) -> bool:
    if not filecode:
        return True
    status = vidara_file_status(api_key, str(filecode))
    return status in ("error", "missing")


def _is_playable(api_key: str, filecode: str | None) -> bool:
    if not filecode:
        return False
    status = vidara_file_status(api_key, str(filecode))
    return status == "active"


def _is_pending(api_key: str, filecode: str | None) -> bool:
    if not filecode:
        return False
    return vidara_file_status(api_key, str(filecode)) in PENDING_STATUSES


def cleanup_catalog(*, skip_legacy_ids: tuple[str, ...] = ("13",)) -> dict[str, int]:
    stats = {"skipped_legacy": 0, "reset_broken": 0, "kept": 0, "recovered": 0}
    rebalance = rebalance_upload_queue()
    stats["rebalance"] = rebalance

    if _namevids_only_enabled():
        stats["preview_purge"] = purge_stale_previews()
        recovered = recover_pending_uploads()
        stats["recovered"] = recovered
        print(f"cleanup: namevids-only mode {stats}")
        return stats

    with db_conn() as conn:
        vidara_acc = get_upload_account(conn, "vidara")
        dood_acc = get_upload_account(conn, "doodstream")
        if not vidara_acc and not dood_acc:
            raise RuntimeError("No enabled vidara or doodstream account")

        if vidara_acc:
            api_key = str(vidara_acc["secret"])

            for legacy_id in skip_legacy_ids:
                cur = conn.execute(
                    """
                    UPDATE videos
                    SET status = 'skipped',
                        error_message = 'legacy broken vidara embed',
                        updated_at = now()
                    WHERE id = %s AND status = 'published'
                    RETURNING id
                    """,
                    (legacy_id,),
                )
                if cur.fetchone():
                    stats["skipped_legacy"] += 1

            rows = conn.execute(
                """
                SELECT id, pre_filecode, full_filecode
                FROM videos
                WHERE status = 'published'
                  AND (host_provider IS NULL OR host_provider = 'vidara')
                ORDER BY id
                """
            ).fetchall()

            for row in rows:
                vid = str(row["id"])
                pre_fc = row.get("pre_filecode")
                pre_ok = _is_playable(api_key, pre_fc)
                pre_pending = _is_pending(api_key, pre_fc)
                full_ok = _is_playable(api_key, row.get("full_filecode"))
                if not _pre_is_broken(api_key, pre_fc):
                    stats["kept"] += 1
                    if pre_ok and full_ok:
                        print(f"cleanup: keep {vid} (pre+full active)")
                    elif pre_ok:
                        print(f"cleanup: keep {vid} (pre active, full pending)")
                    elif pre_pending:
                        print(f"cleanup: keep {vid} (pre pending on Vidara)")
                    else:
                        print(f"cleanup: keep {vid} (pre status unknown, not resetting)")
                    continue

                conn.execute(
                    """
                    UPDATE videos
                    SET status = 'parsed',
                        pre_filecode = NULL,
                        full_filecode = NULL,
                        namevids_file_id = NULL,
                        published_at = NULL,
                        error_message = 'reset: vidara not active',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (vid,),
                )
                stats["reset_broken"] += 1
                print(f"cleanup: reset {vid} (vidara pre broken or missing)")
        else:
            print("cleanup: Vidara disabled — skip published embed checks")

        conn.commit()

    recovered = recover_pending_uploads()
    stats["recovered"] = recovered
    print(f"cleanup: {stats}")
    return stats


if __name__ == "__main__":
    cleanup_catalog()
