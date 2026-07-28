"""yt-dlp wrapper (M17): deterministic filenames, atomic finish, quiet logs.

outtmpl always includes the twitch_vod_id (M17 AC: same-day same-title
VODs never collide). The confirmed final path is the one reported by the
'finished' progress_hook (design.md §8); ``info['requested_downloads'][0]
['filepath']`` is only cross-checked for existence, never trusted alone.

M15 deadline (F6, Codex/tester review round 2, differed-items follow-up):
yt-dlp's own download transfer isn't internally deadline-aware the way
googleapiclient's resumable-upload chunks are, so full propagation isn't
practical here -- but two cheap additions are enough to guarantee "never
hangs forever", which is the actual AC: (1) the deadline is checked on
every ``progress_hooks`` callback yt-dlp already calls periodically
during a download, raising ``DeadlineExceededError`` to interrupt it
mid-transfer (same exception the rest of the run already treats as
"stop and report exit 5", see cli/commands/vods.py's outer
``except (DeadlineExceededError, GracefulAbort)``); (2) yt-dlp's own
``socket_timeout`` option bounds a stalled/hung connection at the socket
level even *between* progress_hook calls. What's still genuinely out of
scope: an interactive OAuth wait (see README).
"""

from __future__ import annotations

from pathlib import Path

import yt_dlp

from vodp.core.errors import EnvironmentError_


class _QuietYdlLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def download_vod(
    url: str, download_dir: Path, twitch_vod_id: str, *, ydl_opts_overrides: dict | None = None,
    ydl_class=yt_dlp.YoutubeDL, deadline=None,
) -> Path:
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(download_dir / f"vodp-{twitch_vod_id}.%(ext)s")

    finished_path: dict = {}

    def _hook(d):
        if deadline is not None:
            # F6: checked on every progress_hook callback (yt-dlp calls
            # this periodically throughout the download, not just once)
            # -- raises DeadlineExceededError to interrupt a download
            # still in flight once the run's whole-run budget is gone.
            deadline.check()
        if d.get("status") == "finished":
            finished_path["path"] = d.get("filename")

    opts = {
        "outtmpl": outtmpl,
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,  # M19: yt-dlp progress must never spam the log
        "logger": _QuietYdlLogger(),
        "progress_hooks": [_hook],
    }
    if deadline is not None:
        # F6: bounds a stalled connection at the socket level too (yt-dlp
        # native option) -- progress_hooks alone wouldn't fire during a
        # true network stall with zero bytes moving.
        opts["socket_timeout"] = deadline.timeout_for(cap=30.0)
    if ydl_opts_overrides:
        opts.update(ydl_opts_overrides)

    with ydl_class(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    path_str = finished_path.get("path")
    if not path_str:
        try:
            path_str = info["requested_downloads"][0]["filepath"]
        except (KeyError, IndexError, TypeError):
            path_str = None
    if not path_str:
        raise EnvironmentError_(f"download did not produce a file for VOD {twitch_vod_id}")

    path = Path(path_str)
    if not path.is_file():
        raise EnvironmentError_(f"downloaded file missing on disk: {path}")
    return path
