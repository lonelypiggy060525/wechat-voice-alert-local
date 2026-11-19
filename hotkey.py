"""Global hotkey via RegisterHotKey (no admin rights, no keyboard hooks)."""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT,
         "shift": MOD_SHIFT, "win": MOD_WIN, "cmd": MOD_WIN}

_VK_NAMES = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "pause": 0x13, "printscreen": 0x2C,
}


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """'ctrl+alt+q' -> (modifiers, vk)."""
    if not spec:
        return None
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key = None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        else:
            key = p
    if key is None:
        return None
    if key in _VK_NAMES:
        vk = _VK_NAMES[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.upper().startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        return None
    return mods | MOD_NOREPEAT, vk


def hotkey_label(spec: str) -> str:
    return "+".join(p.capitalize() for p in spec.split("+"))


class HotkeyManager:
    def __init__(self, on_press: Callable[[], None]) -> None:
        self._cb = on_press
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._current: tuple[int, int] | None = None
        self._spec = ""

    def start(self, spec: str) -> tuple[bool, str]:
        """Register spec; returns (ok, message)."""
        parsed = parse_hotkey(spec)
        if not parsed:
            return False, f"快捷键格式无法识别：{spec}"
        self.stop()
        self._spec = spec
        self._pending = parsed
        self._result: list = []
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(2)
        return self._result[0] if self._result else (False, "快捷键注册超时")

    def _run(self) -> None:
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        mods, vk = self._pending
        ok = bool(user32.RegisterHotKey(None, 1, mods, vk))
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if ok:
            self._current = (mods, vk)
            self._result.append((True, ""))
        else:
            err = ctypes.get_last_error()
            self._result.append((False, f"快捷键被占用或注册失败（错误码 {err}），换一个组合试试"))
        self._ready.set()
        if not ok:
            return

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == 1:
                try:
                    self._cb()
                except Exception:
                    pass

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._thread_id = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None
