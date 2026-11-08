"""The news photo as the cover of a lesson's audio post.

A lesson used to be a video (photo + audio), but Telegram autoplays channel videos
muted and hides the caption when one is opened to listen. An audio post plays with
sound in place and keeps the text visible; the photo shows as its cover.

Needs ffmpeg; without it, or for any failure, callers get None and post the audio
without a cover.
"""
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import requests

from news_platform.pipeline.ingestion import USER_AGENT

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_COVER_BYTES = 200 * 1024   # Telegram's limit for a thumbnail
COVER_SIZE = 320


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def download_image(url: Optional[str], timeout: float = 15) -> Optional[bytes]:
    if not url:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        return None
    if not resp.content or len(resp.content) > MAX_IMAGE_BYTES:
        return None
    return resp.content


def cover(image: bytes, timeout: float = 60) -> Optional[bytes]:
    """The news photo as an audio cover: a centred square JPEG of at most 320 px and
    200 kB, which is what Telegram accepts as an audio thumbnail."""
    if not image or not available():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "image").write_bytes(image)
        out = folder / "cover.jpg"
        for quality in (3, 6, 10):  # ffmpeg's JPEG scale: lower is better
            command = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(folder / "image"),
                       "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={COVER_SIZE}:{COVER_SIZE}",
                       "-frames:v", "1", "-q:v", str(quality), str(out)]
            try:
                done = subprocess.run(command, capture_output=True, timeout=timeout)
            except (subprocess.SubprocessError, OSError) as exc:
                logging.getLogger(__name__).warning("lesson cover failed: %s", exc)
                return None
            if done.returncode != 0 or not out.exists():
                logging.getLogger(__name__).warning("lesson cover failed: %s",
                                                    done.stderr.decode(errors="replace")[-300:])
                return None
            data = out.read_bytes()
            if len(data) <= MAX_COVER_BYTES:
                return data
    return None


def _duration(path: Path) -> Optional[float]:
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                               capture_output=True, timeout=30)
        return float(json.loads(probe.stdout or b"{}")["format"]["duration"]) or None
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return None
