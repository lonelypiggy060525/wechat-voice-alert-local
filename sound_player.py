"""Play notification sounds (wav / mp3 / m4a / flac ...) through MCI, with instant stop."""
from __future__ import annotations

import ctypes
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import threading

_winmm = ctypes.WinDLL("winmm", use_last_error=True)
_winmm.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
_winmm.mciSendStringW.restype = ctypes.c_uint

def resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


BUILTIN_DIR = resource_path("assets", "sounds")
CACHE_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WeChatVoiceAlert", "sounds")
MCI_NATIVE = {".wav", ".mp3", ".wma", ".mid", ".rmi"}

BUILTIN_SOUNDS = [
    ("bell_soft", "空灵铃 (默认)"),
    ("marimba", "木琴双音"),
    ("chime_triad", "三音琶音"),
    ("water_drop", "水滴"),
    ("soft_ping", "轻点 (最短)"),
    ("wooden_tap", "木鱼扣击"),
]


def resolve_sound(value: str) -> str | None:
    """'builtin:name' or absolute path -> real file path (or None)."""
    if not value:
        return None
    if value.startswith("builtin:"):
        path = os.path.join(BUILTIN_DIR, value.split(":", 1)[1] + ".wav")
        return path if os.path.exists(path) else None
    return value if os.path.exists(value) else None


def display_name(value: str) -> str:
    if value.startswith("builtin:"):
        key = value.split(":", 1)[1]
        for k, label in BUILTIN_SOUNDS:
            if k == key:
                return label
        return key
    return os.path.basename(value)


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
                     "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for pattern in patterns:
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return hits[0]
    return None


def ensure_playable(path: str) -> str | None:
    """MCI cannot decode m4a/aac/flac/ogg; convert once with ffmpeg and cache the mp3."""
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in MCI_NATIVE:
        return path
    exe = ffmpeg_path()
    if not exe:
        return None
    try:
        st = os.stat(path)
        tag = hashlib.md5(f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")).hexdigest()[:12]
        os.makedirs(CACHE_DIR, exist_ok=True)
        out = os.path.join(CACHE_DIR, f"{os.path.splitext(os.path.basename(path))[0]}_{tag}.mp3")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        subprocess.run(
            [exe, "-y", "-loglevel", "error", "-i", path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", out],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            timeout=60, check=False)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        return None
    return None


class SoundPlayer:
    """MCI based player: no extra deps, supports wav/mp3/wma/m4a."""

    def __init__(self) -> None:
        self._alias: str | None = None
        self._lock = threading.RLock()
        self._counter = 0

    def _mci(self, cmd: str) -> int:
        return _winmm.mciSendStringW(cmd, None, 0, None)

    def play(self, path: str, volume: float = 0.85) -> bool:
        path = ensure_playable(path)
        if not path:
            return False
        with self._lock:
            self.stop()
            self._counter += 1
            alias = f"va{self._counter % 1000}"
            ext = os.path.splitext(path)[1].lower()
            kind = "waveaudio" if ext == ".wav" else "mpegvideo"
            err = self._mci(f'open "{path}" type {kind} alias {alias}')
            if err != 0:
                # some files refuse the explicit type - retry letting MCI guess
                err = self._mci(f'open "{path}" alias {alias}')
            if err != 0:
                return False
            self._alias = alias
            vol = max(0, min(1000, int(volume * 1000)))
            self._mci(f"setaudio {alias} volume to {vol}")
            if self._mci(f"play {alias}") != 0:
                self.stop()
                return False
            return True

    def stop(self) -> None:
        with self._lock:
            if self._alias:
                self._mci(f"stop {self._alias}")
                self._mci(f"close {self._alias}")
                self._alias = None

    def is_playing(self) -> bool:
        with self._lock:
            if not self._alias:
                return False
            buf = ctypes.create_unicode_buffer(64)
            if self._mci(f"status {self._alias} mode") != 0:
                return False
            _winmm.mciSendStringW(f"status {self._alias} mode", buf, 64, None)
            return buf.value.strip() != "stopped"

    def duration_ms(self) -> int:
        """Length of the currently opened clip (must be called from the playing thread)."""
        with self._lock:
            if not self._alias:
                return 0
            buf = ctypes.create_unicode_buffer(64)
            _winmm.mciSendStringW(f"status {self._alias} length", buf, 64, None)
            try:
                return int(buf.value or 0)
            except ValueError:
                return 0
