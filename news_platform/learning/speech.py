"""Slow read-aloud audio for lessons, with a short intro jingle.

Uses Microsoft's neural voices through edge-tts (free, no key, but not an official
API: if it fails, the lesson goes out without audio). A different voice is picked
for each lesson. Output is MP3 (24 kHz mono, 48 kbit/s), the same format as
assets/intro.mp3, so the two can simply be joined.
"""
import asyncio
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VOICES: Dict[str, List[Tuple[str, str]]] = {
    "de": [
        ("de-DE-KatjaNeural", "Katja"),
        ("de-DE-AmalaNeural", "Amala"),
        ("de-DE-SeraphinaMultilingualNeural", "Seraphina"),
        ("de-DE-ConradNeural", "Conrad"),
        ("de-DE-KillianNeural", "Killian"),
        ("de-DE-FlorianMultilingualNeural", "Florian"),
    ],
    "en": [
        ("en-GB-SoniaNeural", "Sonia"),
        ("en-US-JennyNeural", "Jenny"),
        ("en-GB-RyanNeural", "Ryan"),
        ("en-US-GuyNeural", "Guy"),
    ],
}
INTRO = Path(__file__).parent / "assets" / "intro.mp3"
BITRATE = 48000  # bits per second of both the intro and the voice

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
             "November", "Dezember"]
_ORDINALS_DE = ["erste", "zweite", "dritte", "vierte", "fünfte", "sechste", "siebte", "achte", "neunte", "zehnte",
                "elfte", "zwölfte", "dreizehnte", "vierzehnte", "fünfzehnte", "sechzehnte", "siebzehnte",
                "achtzehnte", "neunzehnte"]
_UNITS_DE = ["", "einund", "zweiund", "dreiund", "vierund", "fünfund", "sechsund", "siebenund", "achtund", "neunund"]


def ordinal_de(day: int) -> str:
    """Day of the month as a spoken ordinal: 8 -> "achte", 21 -> "einundzwanzigste"."""
    if day < 20:
        return _ORDINALS_DE[day - 1]
    return _UNITS_DE[day % 10] + ("zwanzigste" if day < 30 else "dreißigste")


def spoken_intro(channel: str, language: str = "de", when: Optional[datetime] = None) -> str:
    """What the voice says before the news: today's date and the channel's name."""
    when = when or datetime.now(_berlin())
    if language == "de":
        return (f"Heute ist {WEEKDAYS_DE[when.weekday()]}, der {ordinal_de(when.day)} {MONTHS_DE[when.month - 1]} "
                f"{when.year}. Sie hören kurze Nachrichten von {channel}.")
    return f"Today is {when.strftime('%A, %B')} {when.day}, {when.year}. You are listening to {channel}."


def lesson_script(title: str, text: str, channel: str, language: str = "de") -> str:
    """The full read-aloud text; the blank lines give short pauses."""
    return f"{spoken_intro(channel, language)}\n\n{title}.\n\n{text}"


def _berlin():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("TZ_LESSONS", "Europe/Berlin"))
    except Exception:
        return timezone.utc


@dataclass
class Audio:
    data: bytes
    voice: str
    duration: int  # seconds


def synthesize(text: str, language: str = "de", rate: Optional[str] = None,
               with_intro: bool = True, timeout: float = 120) -> Optional[Audio]:
    """Read `text` aloud slowly; None if the language has no voice or synthesis failed."""
    voices = VOICES.get(language)
    if not voices or not text.strip():
        return None
    voice, name = random.choice(voices)
    rate = rate or os.getenv("TTS_RATE", "-25%")
    try:
        import edge_tts
    except ImportError:
        logging.getLogger(__name__).warning("edge-tts is not installed; lesson goes out without audio")
        return None

    async def stream() -> bytes:
        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
            if chunk.get("type") == "audio":
                audio.extend(chunk["data"])
        return bytes(audio)

    try:
        speech = asyncio.run(asyncio.wait_for(stream(), timeout))
    except Exception as exc:
        logging.getLogger(__name__).warning("speech synthesis with %s failed: %s", voice, exc)
        return None
    if not speech:
        return None
    intro = INTRO.read_bytes() if with_intro and INTRO.exists() else b""
    data = intro + speech
    return Audio(data=data, voice=name, duration=round(len(data) * 8 / BITRATE))
