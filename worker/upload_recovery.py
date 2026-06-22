"""Recover uploads that already reached host but never reached published."""
from __future__ import annotations

import os
from typing import Any

from doodstream_client import get_file_status as dood_file_status
from peach_db import db_conn, get_upload_account
from vidara_client import PENDING_STATUSES, get_file_status as vidara_file_status


def _namevids_only_enabled() -> bool:
    return os.environ.get("UPLOADER_NAMEVIDS_ONLY", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _recover_namevids_only(conn) -> int:
    published = 0
    rows = conn.execute(
        """
        SELECT id
        FROM videos
        WHERE status IN ('failed', 'uploading')
          AND namevids_file_id IS NOT NULL
          AND namevids_file_id <> ''
        ORDER BY updated_at ASC
        LIMIT 20
        """
    ).fetchall()
    for row in rows:
        vid = str(row["id"])
        conn.execute(
            """
            UPDATE videos SET
              status = 'published',
              published_at = COALESCE(published_at, now()),
              error_message = NULL,
              updated_at = now()
            WHERE id = %s
            """,
            (vid,),
        )
        published += 1
        print(f"recover: published {vid} (namevids-only)")
    return published


def _try_publish_vidara_row(conn, row: dict[str, Any], api_key: str) -> bool:
    vid = str(row["id"])
    pre_fc = str(row.get("pre_filecode") or "")
    full_fc = str(row.get("full_filecode") or "")
    if not pre_fc:
        return False

    pre_status = vidara_file_status(api_key, pre_fc)
    full_status = vidara_file_status(api_key, full_fc) if full_fc else "missing"
    print(f"recover: {vid} pre={pre_status} full={full_status}")

    if pre_status in ("error", "missing"):
        conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                pre_filecode = NULL,
                full_filecode = NULL,
                namevids_file_id = NULL,
                error_message = 'reset: vidara pre broken',
                updated_at = now()
            WHERE id = %s
            """,
            (vid,),
        )
        print(f"recover: reset {vid} (broken pre)")
        return False

    if pre_status not in ("active", *PENDING_STATUSES):
        return False

    note = None
    if pre_status != "active":
        note = "pre still transcoding on Vidara"
    elif full_status != "active":
        note = "full still transcoding on Vidara"

    conn.execute(
        """
        UPDATE videos SET
          status = 'published',
          published_at = COALESCE(published_at, now()),
          error_message = %s,
          updated_at = now()
        WHERE id = %s
        """,
        (note, vid),
    )
    print(f"recover: published {vid}")
    return True


def _try_publish_dood_row(conn, row: dict[str, Any], api_key: str) -> bool:
    vid = str(row["id"])
    pre_fc = str(row.get("pre_filecode") or "")
    full_fc = str(row.get("full_filecode") or "")
    if not pre_fc:
        return False

    pre_status = dood_file_status(api_key, pre_fc)
    full_status = dood_file_status(api_key, full_fc) if full_fc else "unknown"
    print(f"recover: {vid} dood pre={pre_status} full={full_status}")

    if pre_status in ("error", "missing", "deleted"):
        conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                pre_filecode = NULL,
                full_filecode = NULL,
                namevids_file_id = NULL,
                error_message = 'reset: dood pre broken',
                updated_at = now()
            WHERE id = %s
            """,
            (vid,),
        )
        print(f"recover: reset {vid} (broken dood pre)")
        return False

    if pre_status not in ("active", "live", "unknown"):
        return False

    note = None
    if full_status not in ("active", "live", "unknown"):
        note = "full still processing on DoodStream"

    conn.execute(
        """
        UPDATE videos SET
          status = 'published',
          host_provider = 'doodstream',
          published_at = COALESCE(published_at, now()),
          error_message = %s,
          updated_at = now()
        WHERE id = %s
        """,
        (note, vid),
    )
    print(f"recover: published {vid} (dood)")
    return True


def recover_pending_uploads() -> int:
    published = 0
    with db_conn() as conn:
        if _namevids_only_enabled():
            published = _recover_namevids_only(conn)
            conn.commit()
            if published:
                print(f"recover: published {published} video(s)")
            return published

        vidara_acc = get_upload_account(conn, "vidara")
        dood_acc = get_upload_account(conn, "doodstream")

        rows = conn.execute(
            """
            SELECT *
            FROM videos
            WHERE status IN ('failed', 'uploading')
              AND pre_filecode IS NOT NULL
              AND pre_filecode <> ''
            ORDER BY updated_at ASC
            LIMIT 10
            """
        ).fetchall()

        for row in rows:
            row_dict = dict(row)
            host = str(row_dict.get("host_provider") or "vidara")
            if host == "doodstream":
                if not dood_acc:
                    continue
                if _try_publish_dood_row(conn, row_dict, str(dood_acc["secret"])):
                    published += 1
            elif vidara_acc:
                if _try_publish_vidara_row(conn, row_dict, str(vidara_acc["secret"])):
                    published += 1

        conn.commit()

    if published:
        print(f"recover: published {published} video(s)")
    return published


if __name__ == "__main__":
    recover_pending_uploads()
