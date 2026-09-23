import os
from pathlib import Path

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k] = v

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
from main import get_conn, get_origin, row_to_video

with get_conn() as conn:
    origin = get_origin(conn)
    rows = conn.execute(
        """
        SELECT * FROM videos
        WHERE status = 'published'
          AND pre_filecode IS NOT NULL
          AND full_filecode IS NOT NULL
        """
    ).fetchall()
    assert rows, "no published videos"
    v = row_to_video(dict(rows[0]), origin, include_full=True)
    assert v["fullAvailable"] is True
    assert v["preVideo"].startswith("https://vidara.so/e/")
    assert v["fullVideo"].startswith("https://vidara.so/e/")
    v_public = row_to_video(dict(rows[0]), origin, include_full=False)
    assert v_public["fullAvailable"] is True
    assert v_public["fullVideo"] == ""
    from main import video_availability

    assert video_availability(str(v["id"])) == {"fullAvailable": True}
    print("api ok:", v["id"], v["preVideo"], v["fullVideo"])
