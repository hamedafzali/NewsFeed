"""The news photo and the lesson audio as one small video, so a lesson is one post
with a big picture (Telegram shows audio files only with a tiny cover).

Needs ffmpeg; without it, or for any failure, callers get None and post the audio alone.
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
MAX_VIDEO_BYTES = 45 * 1024 * 1024  # bots may upload up to 50 MB
WIDTH = 1280


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


def _duration(path: Path) -> Optional[float]:
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                               capture_output=True, timeout=30)
        return float(json.loads(probe.stdout or b"{}")["format"]["duration"]) or None
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        return None
