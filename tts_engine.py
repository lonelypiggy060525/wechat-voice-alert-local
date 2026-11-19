"""Speech output with two backends: Microsoft Edge neural voices (online) and SAPI (offline).

Voice values look like 'edge:zh-CN-XiaoxiaoNeural' or 'sapi:Microsoft Huihui Desktop - ...'.
The engine runs in its own thread; stop() cuts the current sentence within ~100 ms.
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import tempfile
import threading
import time

import pythoncom
import win32com.client

from sound_player import SoundPlayer

# (edge voice id, label)
EDGE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声，温柔自然"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声，活泼一点"),
    ("zh-CN-YunxiNeural", "云希 · 男声，年轻"),
    ("zh-CN-YunjianNeural", "云健 · 男声，沉稳"),
    ("zh-CN-YunyangNeural", "云扬 · 男声，播音腔"),
    ("zh-CN-YunxiaNeural", "云夏 · 少年音"),
]

DEFAULT_EDGE_VOICE = "edge:zh-CN-XiaoxiaoNeural"


def sapi_voices() -> list[str]:
    pythoncom.CoInitialize()
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    return [t.GetDescription() for t in voice.GetVoices()]


def pick_chinese_voice(voices: list[str]) -> str:
    for v in voices:
        if "Chinese" in v or "中文" in v or "Huihui" in v or "Xiaoxiao" in v:
            return v
    return voices[0] if voices else ""


def voice_options() -> list[tuple[str, str]]:
    """[(stored value, combobox label)] - Edge voices first, then local SAPI ones."""
    opts = [(f"edge:{vid}", f"在线 · {label}") for vid, label in EDGE_VOICES]
    for v in sapi_voices():
        opts.append((f"sapi:{v}", f"离线 · {v}"))
    return opts


def normalize_voice(value: str) -> str:
    if not value:
        return DEFAULT_EDGE_VOICE
    if value.startswith("edge:") or value.startswith("sapi:"):
        return value
    return f"sapi:{value}"   # old configs stored a bare SAPI name


def voice_label(value: str) -> str:
    value = normalize_voice(value)
    for stored, label in voice_options():
        if stored == value:
            return label
    if value.startswith("edge:"):
        return "在线 · " + value.split(":", 1)[1]
    return value.split(":", 1)[-1]


_CLEAN_PATTERNS = [
    (re.compile(r"\[(图片|照片|表情|动画表情|语音|视频|文件|链接|位置|名片|小程序|音乐|红包|转账|聊天记录)\]"),
     "发来一条消息"),
    (re.compile(r"\[[^\]]{0,10}$"), "发来一条消息"),
    (re.compile(r"https?://\S+"), "一个链接"),
    (re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]+"), ""),
    (re.compile(r"[*#`_~]{2,}"), ""),
    (re.compile(r"\s+"), " "),
]


def clean_for_speech(text: str) -> str:
    out = text or ""
    for pat, repl in _CLEAN_PATTERNS:
        out = pat.sub(repl, out)
    return out.strip(" ，,。.：:")


class TTSEngine:
    def __init__(self, voice: str = "", rate: int = 0, volume: int = 100) -> None:
        self.voice = normalize_voice(voice)
        self.rate = rate
        self.volume = volume
        self._q: queue.Queue = queue.Queue()
        self._voice_obj = None
        self._ready = threading.Event()
        self._stop_flag = threading.Event()
        self._playing_until = 0.0
        self._busy = 0
        self._busy_lock = threading.Lock()
        self._player = SoundPlayer()
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()
        self._ready.wait(3)

    # -- public API -------------------------------------------------
    def speak(self, text: str, replace: bool = True) -> None:
        if not text.strip():
            return
        if replace:
            self.stop()
        with self._busy_lock:
            self._busy += 1
        self._q.put(("say", text))

    def stop(self) -> None:
        # MCI belongs to the playing thread, so only raise the flag here; the worker
        # stops the clip itself (within ~40 ms).
        self._stop_flag.set()
        self._playing_until = 0.0
        self._q.put(("stop", None))

    def busy(self) -> bool:
        """True while a clip is queued, being synthesised, or still playing."""
        with self._busy_lock:
            if self._busy > 0:
                return True
        return self.is_speaking()

    def set_voice(self, value: str) -> None:
        self.voice = normalize_voice(value)
        self._q.put(("voice", self.voice))

    def set_rate(self, rate: int) -> None:
        self.rate = rate
        self._q.put(("rate", rate))

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self._q.put(("volume", self.volume))

    def is_speaking(self) -> bool:
        return time.time() < self._playing_until

    # -- worker ------------------------------------------------------
    def _apply_sapi_voice(self, name: str) -> None:
        tokens = self._voice_obj.GetVoices()
        for i in range(tokens.Count):
            tok = tokens.Item(i)
            if tok.GetDescription() == name:
                self._voice_obj.Voice = tok
                return

    def _run(self) -> None:
        pythoncom.CoInitialize()
        try:
            self._voice_obj = win32com.client.Dispatch("SAPI.SpVoice")
            sapi_name = self.voice.split(":", 1)[1] if self.voice.startswith("sapi:") else ""
            if not sapi_name:
                sapi_name = pick_chinese_voice([t.GetDescription() for t in self._voice_obj.GetVoices()])
            self._apply_sapi_voice(sapi_name)
            try:
                self._voice_obj.Volume = max(0, min(100, self.volume))
                self._voice_obj.Rate = max(-10, min(10, self.rate))
            except Exception:
                pass
            self._ready.set()
            while True:
                try:
                    cmd, arg = self._q.get(timeout=0.3)
                except queue.Empty:
                    continue
                try:
                    if cmd == "say":
                        self._stop_flag.clear()
                        try:
                            self._say(arg)
                        finally:
                            with self._busy_lock:
                                self._busy = max(0, self._busy - 1)
                    elif cmd == "stop":
                        self._stop_flag.set()
                        self._playing_until = 0.0
                        self._player.stop()
                        if self._voice_obj:
                            self._voice_obj.Speak("", 3)
                    elif cmd == "voice":
                        if arg.startswith("sapi:"):
                            self._apply_sapi_voice(arg.split(":", 1)[1])
                    elif cmd == "rate":
                        if self._voice_obj:
                            self._voice_obj.Rate = max(-10, min(10, int(arg)))
                    elif cmd == "volume":
                        if self._voice_obj:
                            self._voice_obj.Volume = max(0, min(100, int(arg)))
                except Exception:
                    pass
        finally:
            pythoncom.CoUninitialize()

    def _say(self, text: str) -> None:
        if self.voice.startswith("edge:"):
            if self._say_edge(text):
                return
            # network/voice failure: fall back to SAPI so something is still heard
        self._say_sapi(text)

    def _say_sapi(self, text: str) -> None:
        try:
            self._voice_obj.Speak(text, 3)   # async + purge
            self._playing_until = time.time() + max(1.0, len(text) * 0.15)
        except Exception:
            pass

    def _say_edge(self, text: str) -> bool:
        voice_id = self.voice.split(":", 1)[1]
        # a unique file per clip: MCI keeps the last one open, and reusing the same
        # path made every second clip fail to write (PermissionError -> SAPI fallback)
        path = os.path.join(tempfile.gettempdir(),
                            f"wechat_voice_alert_tts_{int(time.time() * 1000)}_{os.getpid()}.mp3")
        try:
            ok = asyncio.run(self._stream_edge(text, voice_id, path))
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            return False
        if not ok or self._stop_flag.is_set():
            try:
                os.remove(path)
            except OSError:
                pass
            return True   # cancelled or empty - do not fall back
        try:
            self._player.play(path, max(0.05, min(1.0, self.volume / 100)))
            ms = self._player.duration_ms() or 3000
            self._playing_until = time.time() + ms / 1000.0
            while time.time() < self._playing_until:
                if self._stop_flag.is_set():
                    self._player.stop()
                    self._playing_until = 0.0
                    break
                time.sleep(0.04)
        except Exception:
            return False
        finally:
            self._player.stop()          # close the MCI alias, otherwise the mp3 stays locked
            try:
                os.remove(path)
            except OSError:
                pass
        return True

    async def _stream_edge(self, text: str, voice_id: str, path: str) -> bool:
        import edge_tts

        rate_pct = max(-60, min(60, int(self.rate) * 10))
        communicate = edge_tts.Communicate(text, voice_id, rate=f"{rate_pct:+d}%")
        wrote = False
        with open(path, "wb") as f:
            async for chunk in communicate.stream():
                if self._stop_flag.is_set():
                    return False
                if chunk.get("type") == "audio" and chunk.get("data"):
                    f.write(chunk["data"])
                    wrote = True
        return wrote
