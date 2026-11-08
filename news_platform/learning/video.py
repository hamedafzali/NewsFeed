"""Media built from the news photo and the lesson audio.

- cover(): the photo as a small square cover for the Telegram audio post. Telegram
  autoplays channel videos muted and hides the caption when one is opened to listen,
  so the channel gets audio, not video.
- still_video(): the photo with the audio as an MP4, kept for Instagram (planned).

Needs ffmpeg; without it, or for any failure, these return None.
"""
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from news_platform.pipeline.ingestion import USER_AGENT

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VIDEO_BYTES = 45 * 1024 * 1024
WIDTH = 1280
MAX_COVER_BYTES = 200 * 1024   # Telegram's limit for a thumbnail
COVER_SIZE = 320


@dataclass
class Video:
    data: bytes
    width: int
    height: int
    duration: int


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


def still_video(image: bytes, audio: bytes, timeout: float = 180) -> Optional[Video]:
    """An H.264/AAC MP4 that shows the image for the length of the audio."""
    if not image or not audio or not available():
        return None
    logger = logging.getLogger(__name__)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "image").write_bytes(image)
        (folder / "audio.mp3").write_bytes(audio)
        out = folder / "lesson.mp4"
        length = _duration(folder / "audio.mp3")
        if not length:
            logger.warning("lesson video: could not read the audio length")
            return None
        command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-loop", "1", "-framerate", "1", "-i", str(folder / "image"),
            "-i", str(folder / "audio.mp3"),
            # Even dimensions and yuv420p: what phones can play. One frame per second is plenty for a still.
            "-vf", f"scale='trunc(min({WIDTH},iw)/2)*2':-2,format=yuv420p", "-r", "1",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-g", "15",
            "-c:a", "aac", "-b:a", "48k",
            # The audio's own length: -shortest overshoots with a looped image.
            "-t", f"{length:.2f}", "-movflags", "+faststart", str(out),
        ]
        try:
            done = subprocess.run(command, capture_output=True, timeout=timeout)
            if done.returncode != 0 or not out.exists():
                logger.warning("lesson video failed: %s", done.stderr.decode(errors="replace")[-300:])
                return None
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                 "-show_entries", "format=duration", "-of", "json", str(out)],
                capture_output=True, timeout=30)
            info = json.loads(probe.stdout or b"{}")
            stream = (info.get("streams") or [{}])[0]
            data = out.read_bytes()
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            logger.warning("lesson video failed: %s", exc)
            return None
    if len(data) > MAX_VIDEO_BYTES:
        return None
    return Video(data=data, width=int(stream.get("width") or 0), height=int(stream.get("height") or 0),
                 duration=round(float((info.get("format") or {}).get("duration") or 0)))


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
