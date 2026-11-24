"""微信语音提醒 - WeChat Voice Alert.

Replaces WeChat's default notification sound with one you choose, speaks who messaged
you and what they said, and lets a global hotkey cut the speech short.

Data source: Windows notification center is useless for WeChat 4.x, so this reads the
chat list off the window (PrintWindow + RapidOCR). No injection, no DB access.
"""
from __future__ import annotations

import ctypes
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageTk

import hotkey as hotkey_mod
import sound_player
import tts_engine
import db_list as wechat_list
from config import Config, append_log

APP_TITLE = "微信语音提醒 · 数据库版"
FROZEN = bool(getattr(sys, "frozen", False))
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WeChatDBAlert"

# ---------------------------------------------------------------- 主题
THEMES = {
    "midnight": {
        "name": "深空黑", "desc": "近黑底 · 蓝色主色", "dark": True,
        "bg": "#0D0D10", "card": "#151518", "card_hover": "#1C1C21", "field": "#101013",
        "border": "#26262C", "text": "#F2F3F5", "muted": "#9C9EA8", "dim": "#6E7079",
        "accent": "#3EACF8", "accent_dark": "#2F8FD4", "accent_soft": "#10293B",
        "danger": "#F23046", "danger_soft": "#3A1116",
        "green": "#30D158", "green_soft": "#0F2A1B", "on_accent": "#04121C",
    },
    "cream": {
        "name": "奶油", "desc": "暖白底 · 砖红主色", "dark": False,
        "bg": "#F3ECE0", "card": "#FFFCF6", "card_hover": "#F8F1E5", "field": "#FBF5EA",
        "border": "#E5DACA", "text": "#2B2620", "muted": "#7F7466", "dim": "#AA9E8D",
        "accent": "#D2603F", "accent_dark": "#B44E31", "accent_soft": "#F7E3D9",
        "danger": "#C0392B", "danger_soft": "#FAE3DE",
        "green": "#2F9E63", "green_soft": "#E2F3E8", "on_accent": "#FFFFFF",
    },
    "mist": {
        "name": "雾蓝", "desc": "海军蓝底 · 冰蓝主色", "dark": True,
        "bg": "#0A111D", "card": "#111A2A", "card_hover": "#18243A", "field": "#0D1625",
        "border": "#22304A", "text": "#E9EFF9", "muted": "#95A6C2", "dim": "#62748F",
        "accent": "#6EA8FE", "accent_dark": "#4C8BE8", "accent_soft": "#172B49",
        "danger": "#F25C54", "danger_soft": "#3A1518",
        "green": "#4ED08A", "green_soft": "#0F2A1E", "on_accent": "#06101F",
    },
}
THEME_ORDER = ["midnight", "cream", "mist"]

# 运行期颜色（apply_theme 填充；换主题时整窗重建）
BG = "#0D0D10"
CARD = "#151518"
CARD_HOVER = "#1C1C21"
FIELD = "#101013"
BORDER = "#26262C"
TEXT = "#F2F3F5"
MUTED = "#9C9EA8"
DIM = "#6E7079"
ACCENT = "#3EACF8"
ACCENT_DARK = "#2F8FD4"
ACCENT_SOFT = "#10293B"
DANGER = "#F23046"
DANGER_SOFT = "#3A1116"
GREEN = "#30D158"
GREEN_SOFT = "#0F2A1B"
ON_ACCENT = "#04121C"
IS_DARK = True


def apply_theme(key: str) -> str:
    global BG, CARD, CARD_HOVER, FIELD, BORDER, TEXT, MUTED, DIM
    global ACCENT, ACCENT_DARK, ACCENT_SOFT, DANGER, DANGER_SOFT, GREEN, GREEN_SOFT
    global ON_ACCENT, IS_DARK
    key = key if key in THEMES else "midnight"
    t = THEMES[key]
    BG, CARD, CARD_HOVER = t["bg"], t["card"], t["card_hover"]
    FIELD, BORDER, TEXT = t["field"], t["border"], t["text"]
    MUTED, DIM = t["muted"], t["dim"]
    ACCENT, ACCENT_DARK, ACCENT_SOFT = t["accent"], t["accent_dark"], t["accent_soft"]
    DANGER, DANGER_SOFT = t["danger"], t["danger_soft"]
    GREEN, GREEN_SOFT = t["green"], t["green_soft"]
    ON_ACCENT, IS_DARK = t["on_accent"], t["dark"]
    return key


apply_theme("midnight")

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TINY = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_H1 = ("Microsoft YaHei UI", 15, "bold")
FONT_NAV = ("Microsoft YaHei UI", 10)


# ------------------------------------------------------------ 圆角与图标
def _hex_rgb(color: str) -> tuple:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def mix(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    return "#%02X%02X%02X" % (round(r1 + (r2 - r1) * t), round(g1 + (g2 - g1) * t),
                              round(b1 + (b2 - b1) * t))


_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


def rounded_image(w: int, h: int, radius: int, fill: str, border: str,
                  border_w: int = 1, ss: int = 3) -> Image.Image:
    """抗锯齿圆角矩形：3 倍超采样后缩回。"""
    w, h = max(2, int(w)), max(2, int(h))
    big = (w * ss, h * ss)
    img = Image.new("RGBA", big, (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, big[0] - 1, big[1] - 1), radius=radius * ss,
                        fill=fill, outline=border, width=max(1, border_w * ss))
    return img.resize((w, h), _RESAMPLE)


_ICON_CACHE: dict = {}


def icon_image(name: str, color: str, size: int = 16) -> ImageTk.PhotoImage:
    """在 16×16 网格上画的小图标，4 倍超采样，返回 Tk 图片。"""
    key = (name, color, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 16.0
    lw = max(2, int(1.7 * u))

    def line(x1, y1, x2, y2, w=lw):
        d.line([x1 * u, y1 * u, x2 * u, y2 * u], fill=color, width=w)

    if name == "speaker":
        d.polygon([(2 * u, 6 * u), (5 * u, 6 * u), (9 * u, 2.5 * u),
                   (9 * u, 13.5 * u), (5 * u, 10 * u), (2 * u, 10 * u)], fill=color)
        d.arc([8 * u, 4 * u, 14 * u, 12 * u], start=-55, end=55, fill=color, width=lw)
        d.arc([6.5 * u, 2 * u, 15 * u, 14 * u], start=-50, end=50, fill=color, width=lw)
    elif name == "wave":
        for x, hh in ((3, 4), (6.6, 8), (10.3, 11), (14, 6)):
            line(x, 8 - hh / 2, x, 8 + hh / 2, max(2, int(1.8 * u)))
    elif name == "filter":
        for y, kx in ((4, 10), (8, 5.5), (12, 11.5)):
            line(2, y, 14, y, max(2, int(1.4 * u)))
            d.ellipse([(kx - 1.6) * u, (y - 1.6) * u, (kx + 1.6) * u, (y + 1.6) * u],
                      fill=color)
    elif name == "key":
        d.rounded_rectangle([1.5 * u, 5 * u, 14.5 * u, 12 * u], radius=2 * u,
                            outline=color, width=lw)
        for i in range(3):
            d.ellipse([(4 + i * 3.4) * u - 0.7 * u, 8.5 * u - 0.7 * u,
                       (4 + i * 3.4) * u + 0.7 * u, 8.5 * u + 0.7 * u], fill=color)
    elif name == "clock":
        d.ellipse([1.5 * u, 1.5 * u, 14.5 * u, 14.5 * u], outline=color, width=lw)
        line(8, 8, 8, 4.2)
        line(8, 8, 11, 9.6)
    elif name == "palette":
        d.ellipse([1.5 * u, 1.5 * u, 14.5 * u, 14.5 * u], outline=color, width=lw)
        for cx, cy in ((6, 6.4), (10.4, 6), (11, 10.2), (5.6, 10.8)):
            d.ellipse([(cx - 1.1) * u, (cy - 1.1) * u, (cx + 1.1) * u, (cy + 1.1) * u],
                      fill=color)
    elif name == "gear":
        d.ellipse([5.2 * u, 5.2 * u, 10.8 * u, 10.8 * u], outline=color, width=lw)
        for i in range(8):
            a = i * math.pi / 4
            cx, cy = 8 * u, 8 * u
            x1 = cx + math.cos(a) * 4.6 * u
            y1 = cy + math.sin(a) * 4.6 * u
            x2 = cx + math.cos(a) * 6.6 * u
            y2 = cy + math.sin(a) * 6.6 * u
            d.line([x1, y1, x2, y2], fill=color, width=max(2, int(1.5 * u)))
    elif name == "check":
        d.line([(3 * u, 8.5 * u), (6.6 * u, 12 * u), (13 * u, 4.5 * u)],
               fill=color, width=max(2, int(2.0 * u)))
    elif name == "close":
        line(4, 4, 12, 12, max(2, int(1.8 * u)))
        line(12, 4, 4, 12, max(2, int(1.8 * u)))
    photo = ImageTk.PhotoImage(img.resize((size, size), _RESAMPLE))
    _ICON_CACHE[key] = photo
    return photo


def _bg_of(widget) -> str:
    try:
        return widget.cget("bg")
    except Exception:
        return BG


class RoundedPanel(tk.Frame):
    """卡片：底部是 PIL 画的圆角矩形，内容正常 pack 在上面。"""

    def __init__(self, parent, radius: int = 16, pad: int = 20,
                 fill: str | None = None, border: str | None = None, bg: str | None = None):
        bg = bg or _bg_of(parent)
        super().__init__(parent, bg=bg)
        self._fill, self._border, self._radius = fill or CARD, border or BORDER, radius
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self, bg=self._fill)
        self.inner.pack(fill="both", expand=True, padx=pad, pady=pad)
        self._photo = None
        self._job = None
        self._last = (0, 0)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        if (event.width, event.height) == self._last:
            return
        self._last = (event.width, event.height)
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(16, self._render)

    def _render(self) -> None:
        w, h = self._last
        if w < 8 or h < 8:
            return
        self._photo = ImageTk.PhotoImage(
            rounded_image(w, h, self._radius, self._fill, self._border))
        self._canvas.delete("all")
        self._canvas.create_image(0, 0, anchor="nw", image=self._photo)


class RoundButton(tk.Canvas):
    """圆角按钮：PIL 画底，Tk 画字，带 hover / pressed 状态。"""

    def __init__(self, parent, text: str, command=None, kind: str = "secondary",
                 icon: str | None = None, height: int = 34, pad_x: int = 16,
                 radius: int = 10, width: int | None = None, font=None):
        bg = _bg_of(parent)
        self._kind = kind
        self._text = text
        self._command = command
        self._icon_name = icon
        self._font = font or FONT
        self._pad_x = pad_x
        self._radius = radius
        self._enabled = True
        self._hover = False
        self._pressed = False
        tw = tkfont.Font(font=self._font).measure(text)
        self._iw = 18 if icon else 0
        w = width or (tw + self._pad_x * 2 + (self._iw + 6 if icon else 0))
        super().__init__(parent, width=w, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._photo = None
        self._draw()
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", lambda _e: self._set_pressed(True))
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- colors -------------------------------------------------------
    def _palette(self):
        if not self._enabled:
            return CARD, BORDER, DIM
        if self._kind == "primary":
            base = ACCENT_DARK if (self._hover or self._pressed) else ACCENT
            return base, base, ON_ACCENT
        if self._kind == "danger":
            return mix(CARD, DANGER, 0.10), mix(CARD, DANGER, 0.35), DANGER
        if self._kind == "ghost":
            base = CARD_HOVER if self._hover else _bg_of(self.master)
            return base, base, MUTED if not self._hover else TEXT
        base = mix(CARD, TEXT, 0.10 if self._hover else 0.05)
        if self._pressed:
            base = mix(CARD, TEXT, 0.14)
        return base, BORDER, TEXT

    def _draw(self) -> None:
        w, h = int(self["width"]), int(self["height"])
        fill, border, fg = self._palette()
        self._photo = ImageTk.PhotoImage(rounded_image(w, h, self._radius, fill, border))
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._photo)
        x = self._pad_x
        if self._icon_name:
            self.create_image(x, h // 2, anchor="w", image=icon_image(self._icon_name, fg))
            x += self._iw + 6
        self.create_text(x, h // 2, anchor="w", text=self._text, fill=fg, font=self._font)

    def _set_hover(self, on: bool) -> None:
        if on != self._hover:
            self._hover = on
            self._draw()

    def _set_pressed(self, on: bool) -> None:
        self._pressed = on
        if on:
            self._draw()

    def _on_release(self, event) -> None:
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._enabled and 0 <= event.x <= int(self["width"]) \
                and 0 <= event.y <= int(self["height"]) and self._command:
            self._command()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()


class StatusPill(tk.Canvas):
    """右上角状态：圆角胶囊 + 状态点。"""

    def __init__(self, parent, text: str = "启动中", ok: bool = True, height: int = 24):
        super().__init__(parent, width=110, height=height, bg=_bg_of(parent),
                         highlightthickness=0, bd=0)
        self._h = height
        self._photo = None
        self.set_text(text, ok)

    def set_text(self, text: str, ok: bool = True) -> None:
        font = tkfont.Font(font=FONT_SMALL)
        w = font.measure(text) + 38
        self.configure(width=w, height=self._h)
        fill, fg = (GREEN_SOFT, GREEN) if ok else (DANGER_SOFT, DANGER)
        self._photo = ImageTk.PhotoImage(rounded_image(w, self._h, self._h // 2, fill, fill))
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._photo)
        cy = self._h // 2
        self.create_oval(12, cy - 3, 18, cy + 3, fill=fg, outline="")
        self.create_text(24, cy, anchor="w", text=text, fill=fg, font=FONT_SMALL)


class NavItem(tk.Canvas):
    """侧栏条目：圆角选中块 + 图标 + 文字。"""

    def __init__(self, parent, key: str, text: str, icon: str, on_click,
                 width: int = 180, height: int = 38):
        super().__init__(parent, width=width, height=height, bg=_bg_of(parent),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.key, self.text, self.icon_name, self.on_click = key, text, icon, on_click
        self._active = False
        self._hover = False
        self._photo = None
        self._draw()
        self.bind("<Button-1>", lambda _e: self.on_click(self.key))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))

    def _set_hover(self, on: bool) -> None:
        if on != self._hover:
            self._hover = on
            self._draw()

    def set_active(self, on: bool) -> None:
        if on != self._active:
            self._active = on
            self._draw()

    def _draw(self) -> None:
        w, h = int(self["width"]), int(self["height"])
        if self._active:
            fill, fg = ACCENT_SOFT, ACCENT
        elif self._hover:
            fill, fg = CARD_HOVER, TEXT
        else:
            fill, fg = _bg_of(self.master), MUTED
        self._photo = ImageTk.PhotoImage(rounded_image(w, h, 8, fill, fill))
        self.delete("all")
        self.create_image(0, 0, anchor="nw", image=self._photo)
        self.create_image(12, h // 2, anchor="w", image=icon_image(self.icon_name, fg, 14))
        self.create_text(32, h // 2, anchor="w", text=self.text, fill=fg, font=FONT_SMALL)

# emoji / pictographs / dingbats: OCR never returns these, so they must not count
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2300-\u23FF\u2B00-\u2BFF"
    "\uFE0F\u200D\u20E3\u3030\u303D\u3297\u3299]"
)


def normalize_name(text: str) -> str:
    """Drop emoji, spaces and punctuation: '🐷 小猪' -> '小猪' (what OCR actually sees)."""
    text = _EMOJI_RE.sub("", text or "")
    return re.sub(r"[\s\W_]+", "", text).casefold()


def enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def resource_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def autostart_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.join(HERE, "app.py")}"'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, RUN_VALUE)
            return bool(val)
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Slider(tk.Canvas):
    """Flat canvas slider: rounded track, accent fill, round handle."""

    def __init__(self, parent, minimum: float, maximum: float, value: float,
                 on_change=None, width: int = 260, height: int = 26,
                 step: float | None = None) -> None:
        super().__init__(parent, width=width, height=height, bg=CARD,
                         highlightthickness=0, cursor="hand2")
        self._min, self._max = minimum, maximum
        self._step = step
        self._value = min(max(value, minimum), maximum)
        self._on_change = on_change
        self._cw, self._ch = width, height
        self._pad = 9
        self.bind("<Button-1>", self._on_drag)
        self.bind("<B1-Motion>", self._on_drag)
        self._draw()

    def get(self) -> float:
        return self._value

    def set(self, value: float) -> None:
        value = min(max(value, self._min), self._max)
        if self._step:
            value = round(value / self._step) * self._step
        if value != self._value:
            self._value = value
            self._draw()
            if self._on_change:
                self._on_change(value)

    def _x_of(self, value: float) -> float:
        span = self._max - self._min or 1
        return self._pad + (value - self._min) / span * (self._cw - 2 * self._pad)

    def _on_drag(self, event) -> None:
        span = self._cw - 2 * self._pad
        ratio = min(max((event.x - self._pad) / span, 0.0), 1.0)
        value = self._min + ratio * (self._max - self._min)
        if self._step:
            value = round(value / self._step) * self._step
        self._value = min(max(value, self._min), self._max)
        self._draw()
        if self._on_change:
            self._on_change(self._value)

    def _draw(self) -> None:
        self.delete("all")
        y = self._ch - 9
        round_rect(self, 0, y - 3, self._cw, y + 3, 3, fill=mix(CARD, TEXT, 0.14), outline="")
        x = self._x_of(self._value)
        if x > 2:
            round_rect(self, 0, y - 3, x, y + 3, 3, fill=ACCENT, outline="")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill=CARD, outline=ACCENT, width=2)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        apply_theme(self.cfg.theme)
        if "--theme" in sys.argv:            # 截图/调试用：--theme cream
            try:
                key = sys.argv[sys.argv.index("--theme") + 1]
                if key in THEMES:
                    self.cfg.theme = apply_theme(key)
                    self.cfg.save()
            except (IndexError, OSError):
                pass
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.player = sound_player.SoundPlayer()
        self.tts = tts_engine.TTSEngine(self.cfg.tts_voice, self.cfg.tts_rate, self.cfg.tts_volume)
        self.watcher: wechat_list.ChatListWatcher | None = None
        self.hotkeys = hotkey_mod.HotkeyManager(lambda: self.queue.put(("stop", None)))
        self._announce_token = 0
        self._last_spoken_sender = ""
        self._last_spoken_at = 0.0
        self._announce_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._announce_worker, name="announcer", daemon=True).start()
        self._save_job = None
        self.tray = None
        self._tray_thread = None
        self._sound_choices: list[tuple[str, str]] = []
        self._page = "sound"

        self.title(APP_TITLE)
        try:
            ico = resource_path("assets", "icon.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            pass
        self.geometry("680x500")
        self.minsize(640, 460)
        self.configure(bg=BG)
        self._setup_style()
        self._build()
        self._setup_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close_button)

        ok, msg = self.hotkeys.start(self.cfg.hotkey)
        if not ok:
            self.hotkey_status.configure(text=msg, foreground=DANGER)

        self._start_watcher()
        self.after(1200, self._restore_wechat_position)
        self.after(300, self._dark_title_bar)
        self.bind("<Map>", lambda _e: self._dark_title_bar())
        self.after(150, self._pump)
        self.after(600, self._mark_dirty)   # persist migrations (e.g. sound library) once

    # ------------------------------------------------------------------ style
    def _restore_wechat_position(self) -> None:
        """上一版可能把微信留在了屏幕外，启动时纠正一次（数据库版不需要藏窗口）。"""
        try:
            hwnd = wechat_list.find_wechat_window()
            if hwnd:
                wechat_list.bring_on_screen(hwnd)
        except Exception:
            pass

    def _dark_title_bar(self) -> None:
        """让 Windows 的标题栏也变成深色，和界面连成一片。"""
        try:
            try:
                hwnd = int(self.wm_frame(), 16)   # Tk 给出的顶层窗口句柄
            except (ValueError, tk.TclError):
                hwnd = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            if not hwnd:
                return

            def ref(color: str) -> int:
                r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
                return r | (g << 8) | (b << 16)

            on = ctypes.c_int(1 if IS_DARK else 0)
            dwm = ctypes.windll.dwmapi
            round_pref = ctypes.c_int(2)                               # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(round_pref), 4)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)   # 深色标题栏
            dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(on), 4)   # 旧版本编号
            cap, txt = ctypes.c_int(ref(CARD)), ctypes.c_int(ref(TEXT))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(cap), 4)  # Win11 标题栏底色
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(txt), 4)  # Win11 标题文字
        except Exception:
            pass
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        style.configure(".", font=FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=TEXT, font=FONT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=FONT_SMALL)
        style.configure("CardTitle.TLabel", background=CARD, foreground=TEXT, font=FONT_TITLE)
        style.configure("TCheckbutton", background=CARD, foreground=TEXT, font=FONT)
        style.map("TCheckbutton", background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", mix(CARD, TEXT, 0.12))],
                  foreground=[("disabled", DIM)])
        style.configure("TRadiobutton", background=CARD, foreground=TEXT, font=FONT)
        style.map("TRadiobutton", background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", mix(CARD, TEXT, 0.12))])
        style.configure("TButton", font=FONT, padding=(12, 5), relief="flat",
                        background=mix(CARD, TEXT, 0.08), foreground=TEXT, borderwidth=0)
        style.map("TButton", background=[("active", mix(CARD, TEXT, 0.14)),
                                          ("pressed", mix(CARD, TEXT, 0.18))])
        style.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, padding=(16, 6))
        style.map("Accent.TButton", background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK)])
        style.configure("Ghost.TButton", background=CARD, foreground=MUTED, padding=(8, 4))
        style.map("Ghost.TButton", background=[("active", CARD_HOVER)])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=5)
        style.map("TEntry",
                  bordercolor=[("focus", ACCENT), ("!focus", BORDER)],
                  lightcolor=[("focus", ACCENT), ("!focus", BORDER)],
                  darkcolor=[("focus", ACCENT), ("!focus", BORDER)])
        style.configure("TCombobox", fieldbackground=FIELD, background=mix(FIELD, TEXT, 0.07), foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=MUTED, padding=5, relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD), ("focus", FIELD)],
                  background=[("active", mix(FIELD, TEXT, 0.14)), ("readonly", mix(FIELD, TEXT, 0.07))],
                  foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT), ("!focus", BORDER)],
                  lightcolor=[("focus", ACCENT), ("!focus", BORDER)],
                  darkcolor=[("focus", ACCENT), ("!focus", BORDER)],
                  arrowcolor=[("active", ACCENT)])
        style.configure("Horizontal.TScale", background=CARD, troughcolor="#242429", borderwidth=0)
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                        rowheight=28, borderwidth=0, font=FONT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Treeview.Heading", background=mix(CARD, TEXT, 0.03), foreground=MUTED,
                        font=FONT_SMALL, relief="flat", borderwidth=0)
        style.map("Treeview.Heading", background=[("active", mix(CARD, TEXT, 0.07))])
        style.map("Treeview", background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", gripcount=0, background=mix(CARD, TEXT, 0.12),
                        troughcolor=CARD, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                        arrowcolor=MUTED, arrowsize=12, borderwidth=0)
        style.map("Vertical.TScrollbar",
                  background=[("active", mix(CARD, TEXT, 0.2)), ("pressed", mix(CARD, TEXT, 0.2))],
                  arrowcolor=[("active", ACCENT)])

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        self.configure(bg=BG)
        header = tk.Frame(self, bg=CARD, height=56)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        tk.Frame(header, bg=BORDER, height=1).pack(side="bottom", fill="x")

        logo = tk.Canvas(header, width=28, height=28, bg=CARD, highlightthickness=0)
        logo.pack(side="left", padx=(16, 10), pady=14)
        logo.create_image(0, 0, anchor="nw",
                          image=ImageTk.PhotoImage(rounded_image(28, 28, 8, ACCENT, ACCENT)))
        logo.create_oval(7, 6, 21, 17, fill="white", outline="")
        logo.create_polygon([10, 15, 10, 21, 16, 16], fill="white", outline="")

        tk.Label(header, text=APP_TITLE, bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")

        self.status_label = StatusPill(header, "启动中", True, height=24)
        self.status_label.pack(side="right", padx=(0, 14))
        self.enabled_var = tk.BooleanVar(value=self.cfg.enabled)
        self._check(header, "启用提醒", self.enabled_var, self._on_toggle).pack(side="right", padx=12)

        footer = tk.Frame(self, bg=CARD, height=48)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(side="bottom", fill="x")

        self.dead_hint = tk.Label(footer, text="", bg=CARD, fg=DANGER, font=FONT_SMALL)
        self.dead_hint.pack(side="left", padx=(16, 4))
        self.save_hint = tk.Label(footer, text="", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self.save_hint.pack(side="left", padx=4)
        RoundButton(footer, "保存", lambda: self._save(), kind="primary",
                    icon="check", height=32).pack(side="right", padx=16, pady=8)

        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        sidebar = tk.Frame(body, bg=BG, width=150)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True)

        self._nav: dict[str, NavItem] = {}
        for key, label, icon in (("sound", "提示音", "speaker"),
                                 ("voice", "语音播报", "wave"),
                                 ("filter", "提醒范围", "filter"),
                                 ("hotkey", "快捷键", "key"),
                                 ("general", "常规", "gear"),
                                 ("recent", "最近消息", "clock"),
                                 ("theme", "外观", "palette")):
            item = NavItem(sidebar, key, label, icon, self._show_page, width=134, height=32)
            item.pack(padx=8, pady=1, anchor="w")
            self._nav[key] = item

        self.pages: dict[str, tk.Frame] = {}
        for key in ("sound", "voice", "filter", "hotkey", "general", "recent", "theme"):
            page = tk.Frame(content, bg=BG)
            self.pages[key] = page

        self._build_sound_page(self.pages["sound"])
        self._build_voice_page(self.pages["voice"])
        self._build_filter_page(self.pages["filter"])
        self._build_hotkey_page(self.pages["hotkey"])
        self._build_general_page(self.pages["general"])
        self._build_recent_page(self.pages["recent"])
        self._build_theme_page(self.pages["theme"])
        self._show_page("sound")

    def _show_page(self, key: str) -> None:
        self._page = key
        for name, page in self.pages.items():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for name, item in self._nav.items():
            item.set_active(name == key)

    def _card(self, parent: tk.Frame, title: str, subtitle: str = "") -> tk.Frame:
        panel = RoundedPanel(parent, radius=16, pad=18)
        panel.pack(fill="x", pady=(0, 14))
        inner = panel.inner
        tk.Label(inner, text=title, bg=CARD, fg=TEXT, font=FONT_TITLE, anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(inner, text=subtitle, bg=CARD, fg=MUTED, font=FONT_SMALL,
                     anchor="w", justify="left").pack(fill="x", pady=(2, 10))
        else:
            tk.Frame(inner, bg=CARD, height=12).pack(fill="x")
        return inner

    def _field_row(self, parent: tk.Frame, label: str, width: int = 14) -> tk.Frame:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=CARD, fg=MUTED, font=FONT, width=width, anchor="w").pack(side="left")
        return row

    def _check(self, parent: tk.Frame, text: str, var: tk.BooleanVar, command=None) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=var, command=command,
                              bg=CARD, fg=TEXT, font=FONT, bd=0, highlightthickness=0,
                              activebackground=CARD, activeforeground=TEXT,
                              selectcolor=FIELD, anchor="w", cursor="hand2")

    def _on_volume_change(self, value: float) -> None:
        self.vol_label.configure(text=f"{int(value * 100)}%")
        self._mark_dirty()

    def _on_rate_change(self, value: float) -> None:
        self.rate_label.configure(text=f"{int(value):+d}")
        self._mark_dirty()

    def _on_tts_volume_change(self, value: float) -> None:
        self.tts_vol_label.configure(text=f"{int(value)}%")
        self.tts.set_volume(int(value))
        self._mark_dirty()

    # ------------------------------------------------------------- pages
    def _build_sound_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)

        card = self._card(wrap, "提示音", "先响这个声音，再念出谁说了什么。")
        row = self._field_row(card, "音效")
        self.sound_var = tk.StringVar()
        self.sound_box = ttk.Combobox(row, textvariable=self.sound_var, state="readonly", width=30)
        self.sound_box.pack(side="left")
        self.sound_box.bind("<<ComboboxSelected>>", lambda _e: self._on_sound_pick())
        RoundButton(row, "试听", self._preview_sound, icon="speaker").pack(side="left", padx=8)
        RoundButton(row, "删除", self._remove_sound, kind="ghost").pack(side="left")

        vrow = self._field_row(card, "提示音音量")
        self.vol_slider = Slider(vrow, 0.05, 1.0, self.cfg.sound_volume, self._on_volume_change)
        self.vol_slider.pack(side="left")
        self.vol_label = tk.Label(vrow, text="", bg=CARD, fg=MUTED, font=FONT_SMALL,
                                  width=5, anchor="w")
        self.vol_label.pack(side="left", padx=10)
        self._on_volume_change(self.vol_slider.get())

        self.sound_hint = tk.Label(card, text="", bg=CARD, fg=MUTED, font=FONT_SMALL,
                                   anchor="w", justify="left")
        self.sound_hint.pack(fill="x", pady=(8, 0))
        tk.Label(card, text="支持 wav / mp3 / wma / m4a / aac / flac / ogg（自动转码）。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=520).pack(fill="x", pady=(2, 0))

        self._refresh_sound_list()

    def _build_voice_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "语音播报", "把「谁发的、说了什么」念出来。")

        self.tts_enabled_var = tk.BooleanVar(value=self.cfg.tts_enabled)
        self._check(card, "朗读消息内容", self.tts_enabled_var, self._mark_dirty).pack(anchor="w")

        trow = self._field_row(card, "播报格式")
        self.tpl_var = tk.StringVar(value=self.cfg.tts_template)
        self.tpl_var.trace_add("write", self._mark_dirty)
        ttk.Entry(trow, textvariable=self.tpl_var, width=30).pack(side="left")
        tk.Label(card, text="占位符：{sender} 发送者　{chat} 会话　{content} 内容",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w").pack(fill="x")

        vrow = self._field_row(card, "发音人")
        self._voice_opts = tts_engine.voice_options()
        labels = [label for _v, label in self._voice_opts]
        cur_value = tts_engine.normalize_voice(self.cfg.tts_voice)
        cur_label = next((label for value, label in self._voice_opts if value == cur_value),
                         labels[0] if labels else "")
        self.voice_var = tk.StringVar(value=cur_label)
        self.voice_var.trace_add("write", self._mark_dirty)
        ttk.Combobox(vrow, textvariable=self.voice_var, values=labels,
                     state="readonly", width=26).pack(side="left")
        RoundButton(vrow, "试听", self._preview_tts).pack(side="left", padx=8)

        rrow = self._field_row(card, "语速")
        self.rate_slider = Slider(rrow, -6, 6, self.cfg.tts_rate, self._on_rate_change,
                                  step=1)
        self.rate_slider.pack(side="left")
        self.rate_label = tk.Label(rrow, text="", bg=CARD, fg=MUTED, font=FONT_SMALL,
                                   width=5, anchor="w")
        self.rate_label.pack(side="left", padx=10)

        tvol = self._field_row(card, "播报音量")
        self.tts_vol_slider = Slider(tvol, 0, 100, self.cfg.tts_volume,
                                     self._on_tts_volume_change, step=1)
        self.tts_vol_slider.pack(side="left")
        self.tts_vol_label = tk.Label(tvol, text="", bg=CARD, fg=MUTED, font=FONT_SMALL,
                                      width=5, anchor="w")
        self.tts_vol_label.pack(side="left", padx=10)
        self._on_rate_change(self.rate_slider.get())
        self._on_tts_volume_change(self.tts_vol_slider.get())

        btns = tk.Frame(card, bg=CARD)
        btns.pack(fill="x", pady=(6, 0))
        self.full_read_var = tk.BooleanVar(value=self.cfg.full_read)
        self._check(btns, "朗读完整内容（长消息整段读）",
                    self.full_read_var, self._mark_dirty).pack(anchor="w")
        btns2 = tk.Frame(card, bg=CARD)
        btns2.pack(fill="x", pady=(6, 0))
        RoundButton(btns2, "停止播报", self._stop_speech).pack(side="left")

    def _build_filter_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "提醒范围", "点一下就切换提醒 / 静音；列表来自你的微信会话。")

        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x")
        self.default_announce_var = tk.BooleanVar(value=self.cfg.default_announce)
        self._check(top, "新会话默认提醒", self.default_announce_var,
                    self._mark_dirty).pack(side="left")
        search = ttk.Entry(top, width=14)
        search.pack(side="right")
        self.rule_search_var = tk.StringVar()
        search.configure(textvariable=self.rule_search_var)
        search.bind("<KeyRelease>", lambda _e: self._refresh_rules_tree())
        tk.Label(top, text="搜索", bg=CARD, fg=MUTED, font=FONT_SMALL).pack(side="right", padx=(0, 6))

        tools = tk.Frame(card, bg=CARD)
        tools.pack(side="bottom", fill="x", pady=(8, 0))
        RoundButton(tools, "全部提醒", lambda: self._set_all_rules(True)).pack(side="left")
        RoundButton(tools, "全部静音", lambda: self._set_all_rules(False)).pack(side="left", padx=8)
        self.rules_hint = tk.Label(tools, text="", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self.rules_hint.pack(side="left", padx=8)

        tree_wrap = tk.Frame(card, bg=CARD)
        tree_wrap.pack(fill="both", expand=True, pady=(8, 0))
        self.rules_tree = ttk.Treeview(tree_wrap, columns=("state", "name"), show="headings",
                                       height=6, selectmode="browse")
        self.rules_tree.heading("state", text="")
        self.rules_tree.heading("name", text="会话")
        self.rules_tree.column("state", width=64, anchor="center", stretch=False)
        self.rules_tree.column("name", width=330, anchor="w")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.rules_tree.yview)
        self.rules_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.rules_tree.pack(side="left", fill="both", expand=True)
        self.rules_tree.tag_configure("off", foreground=DIM)
        self.rules_tree.bind("<Button-1>", self._on_rule_click)

        self._session_cache: list[tuple[str, str, int]] = []
        self._refresh_rules_tree()
        self._load_sessions_async()

    def _build_general_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "常规", "启动与播报行为。")
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self._check(card, "开机自动启动", self.autostart_var, self._on_autostart).pack(
            anchor="w", pady=2)
        self.skip_focus_var = tk.BooleanVar(value=self.cfg.skip_when_wechat_focused)
        self._check(card, "正在看微信时不再播报", self.skip_focus_var, self._mark_dirty).pack(
            anchor="w", pady=2)

    def _build_hotkey_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "打断快捷键", "播报到一半按一下就能掐断。")

        row = self._field_row(card, "组合键")
        self.hotkey_var = tk.StringVar(value=self.cfg.hotkey)
        ttk.Entry(row, textvariable=self.hotkey_var, width=22).pack(side="left")
        RoundButton(row, "应用", self._apply_hotkey).pack(side="left", padx=8)
        self.hotkey_status = tk.Label(row, text="", bg=CARD, fg=ACCENT_DARK, font=FONT_SMALL)
        self.hotkey_status.pack(side="left", padx=6)

        tk.Label(card, text="写法示例：ctrl+alt+q、ctrl+shift+F9、alt+space。"
                            "组合键被别的程序占用时会提示，换一个即可。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w",
                 wraplength=500, justify="left").pack(fill="x", pady=(12, 0))

    def _build_recent_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "最近消息", "每次播报（或跳过）都会记录，方便核对名单有没有生效。")

        box = tk.Frame(card, bg=CARD)
        box.pack(fill="both", expand=True)
        self.log_box = tk.Listbox(box, height=12, font=FONT_SMALL, relief="flat", bg=FIELD,
                                  fg=TEXT, highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=BORDER, activestyle="none",
                                  selectbackground=ACCENT_SOFT, selectforeground=TEXT)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)
        self.log_box.insert("end", "还没有记录 —— 来一条微信消息，或点下面的「模拟一条消息」")
        self.log_box.itemconfig(0, foreground=DIM)

        btns = tk.Frame(card, bg=CARD)
        btns.pack(fill="x", pady=(12, 0))
        RoundButton(btns, "模拟一条消息", self._simulate).pack(side="left")
        RoundButton(btns, "打开 Windows 通知设置",
                    lambda: os.startfile("ms-settings:notifications")).pack(side="left", padx=8)

    def _build_theme_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        card = self._card(wrap, "外观", "点一下立即生效，程序会记住。")
        row = tk.Frame(card, bg=CARD)
        row.pack(fill="x", pady=(4, 2))
        self._theme_tiles: dict[str, tk.Canvas] = {}
        for key in THEME_ORDER:
            tile = tk.Canvas(row, width=150, height=112, bg=CARD,
                             highlightthickness=0, bd=0, cursor="hand2")
            tile.pack(side="left", padx=(0, 8))
            self._paint_theme_tile(tile, key)
            tile.bind("<Button-1>", lambda _e, k=key: self._set_theme(k))
            self._theme_tiles[key] = tile

    def _paint_theme_tile(self, tile: tk.Canvas, key: str) -> None:
        t = THEMES[key]
        active = key == self.cfg.theme
        tile.delete("all")
        tile._bg_img = ImageTk.PhotoImage(rounded_image(
            150, 112, 14, t["bg"], t["accent"] if active else t["border"], 2 if active else 1))
        tile.create_image(0, 0, anchor="nw", image=tile._bg_img)
        tile._win_img = ImageTk.PhotoImage(rounded_image(124, 50, 10, t["card"], t["border"]))
        tile.create_image(12, 10, anchor="nw", image=tile._win_img)

        tile.create_oval(20, 20, 30, 30, fill=t["accent"], outline="")
        tile.create_rectangle(36, 21, 86, 26, fill=t["text"], outline="")
        tile.create_rectangle(36, 31, 70, 34, fill=t["muted"], outline="")
        for i, y in enumerate((40, 46, 52)):
            tile.create_rectangle(20, y, 124 - i * 14, y + 4, fill=t["field"], outline="")
        tile.create_text(12, 74, anchor="w", text=t["name"], fill=t["text"],
                         font=("Microsoft YaHei UI", 10, "bold"))
        tile.create_text(12, 94, anchor="w", text=t["desc"], fill=t["muted"], font=FONT_TINY)
        if active:
            tile.create_oval(126, 12, 142, 28, fill=t["accent"], outline="")
            tile.create_text(134, 20, text="✓", fill=t["on_accent"], font=("Microsoft YaHei UI", 8))

    def _set_theme(self, key: str) -> None:
        if key not in THEMES or key == self.cfg.theme:
            return
        self._save(silent=True)
        self.cfg.theme = key
        try:
            self.cfg.save()
        except OSError:
            pass
        self._rebuild()

    def _rebuild(self) -> None:
        page = getattr(self, "_page", "theme")
        apply_theme(self.cfg.theme)
        for child in self.winfo_children():
            child.destroy()
        self._setup_style()
        self._build()
        self._show_page(page if page in self.pages else "sound")
        self._dark_title_bar()

    # ------------------------------------------------------------- helpers
    def _refresh_sound_list(self) -> None:
        self._sound_choices = []
        labels: list[str] = []
        for key, label in sound_player.BUILTIN_SOUNDS:
            self._sound_choices.append((f"builtin:{key}", label))
            labels.append(label)
        for item in self.cfg.custom_sounds:
            path = item.get("path", "")
            name = item.get("name") or os.path.splitext(os.path.basename(path))[0]
            label = name if os.path.exists(path) else f"{name}（文件缺失）"
            if any(label == existing for existing in labels):
                label = f"{label} · {len(labels)}"
            self._sound_choices.append((path, label))
            labels.append(label)
        self._sound_choices.append(("__add__", "＋ 添加音频…"))
        labels.append("＋ 添加音频…")
        self.sound_box["values"] = labels
        current = self.cfg.sound_path
        label = next((lbl for value, lbl in self._sound_choices if value == current), labels[0])
        self.sound_var.set(label)
        hint = sound_player.display_name(current)
        if not sound_player.resolve_sound(current):
            hint += "（文件找不到）"
        self.sound_hint.configure(text=f"当前正在使用：{hint}")

    def _selected_sound_value(self) -> str | None:
        label = self.sound_var.get()
        return next((value for value, lbl in self._sound_choices if lbl == label), None)

    def _on_sound_pick(self) -> None:
        value = self._selected_sound_value()
        if value == "__add__":
            self._add_sound()
            return
        if value:
            self.cfg.sound_path = value
            self.sound_hint.configure(text=f"当前正在使用：{sound_player.display_name(value)}")
            self._mark_dirty()

    def _add_sound(self) -> None:
        path = filedialog.askopenfilename(
            title="选择提示音",
            filetypes=[("音频文件", "*.wav *.mp3 *.wma *.m4a *.aac *.flac *.ogg"),
                       ("所有文件", "*.*")])
        if not path:
            self._refresh_sound_list()
            return
        if not sound_player.ensure_playable(path):
            messagebox.showerror(APP_TITLE,
                                 "这个格式需要 ffmpeg 才能播放，没找到 ffmpeg。\n"
                                 "可以换成 wav / mp3，或先安装 ffmpeg。")
            self._refresh_sound_list()
            return
        if not any(item.get("path") == path for item in self.cfg.custom_sounds):
            self.cfg.custom_sounds.append({
                "name": os.path.splitext(os.path.basename(path))[0],
                "path": path,
            })
        self.cfg.sound_path = path
        self._refresh_sound_list()
        self._mark_dirty()
        self._preview_sound()

    def _remove_sound(self) -> None:
        value = self._selected_sound_value()
        if not value or value == "__add__" or value.startswith("builtin:"):
            messagebox.showinfo(APP_TITLE, "内置音效不能删除，直接选别的就行。")
            return
        self.cfg.custom_sounds = [s for s in self.cfg.custom_sounds if s.get("path") != value]
        if self.cfg.sound_path == value:
            self.cfg.sound_path = "builtin:bell_soft"
        self._refresh_sound_list()
        self._mark_dirty()

    def _current_sound_path(self) -> str:
        return self.cfg.sound_path or "builtin:bell_soft"

    def _preview_sound(self) -> None:
        path = sound_player.resolve_sound(self._current_sound_path())
        if not path:
            messagebox.showwarning(APP_TITLE, "找不到这个音频文件")
            return
        self.player.play(path, float(self.vol_slider.get()))

    def _current_voice(self) -> str:
        label = self.voice_var.get()
        for value, lbl in self._voice_opts:
            if lbl == label:
                return value
        return self._voice_opts[0][0] if self._voice_opts else tts_engine.DEFAULT_EDGE_VOICE

    def _preview_tts(self) -> None:
        self.tts.set_voice(self._current_voice())
        self.tts.set_rate(int(self.rate_slider.get()))
        self.tts.set_volume(int(self.tts_vol_slider.get()))
        self.tts.speak("李明说：晚上七点老地方见。")

    def _stop_speech(self) -> None:
        self._announce_token += 1
        while True:                       # drop everything still waiting to be spoken
            try:
                self._announce_q.get_nowait()
            except queue.Empty:
                break
        self.tts.stop()
        self.player.stop()

    def _apply_hotkey(self) -> None:
        ok, msg = self.hotkeys.start(self.hotkey_var.get().strip())
        if ok:
            self.cfg.hotkey = self.hotkey_var.get().strip()
            self.hotkey_status.configure(text="已生效", foreground=ACCENT_DARK)
            self._mark_dirty()
        else:
            self.hotkey_status.configure(text=msg, foreground=DANGER)

    def _on_toggle(self) -> None:
        self.cfg.enabled = self.enabled_var.get()
        if not self.cfg.enabled:
            self._stop_speech()
        self._set_status("已暂停" if not self.cfg.enabled else "运行中",
                         ok=self.cfg.enabled)
        self._mark_dirty()

    def _on_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
            self.save_hint.configure(text="开机自启已更新")
            self.after(2500, lambda: self.save_hint.configure(text=""))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"设置开机自启失败：{exc}")

    def _set_status(self, text: str, color: str = ACCENT_DARK, ok: bool = True) -> None:
        if color == DANGER or str(color).startswith("#c0"):
            ok = False
        try:
            self.status_label.set_text(text, ok)
        except Exception:
            pass

    # --------------------------------------------------------------- filter
    def _match_rule(self, chat: str, sender: str):
        """Longest matching name wins: returns (rule_name, announce?) or None."""
        hay = normalize_name(f"{chat}\n{sender}")
        if not hay:
            return None
        best = None
        for name, on in self.cfg.contact_rules.items():
            key = normalize_name(name)
            if key and key in hay and (best is None or len(key) > best[0]):
                best = (len(key), name, bool(on))
        return (best[1], best[2]) if best else None

    def _passes_filter(self, chat: str, sender: str) -> bool:
        rule = self._match_rule(chat, sender)
        if rule is not None:
            return rule[1]
        return self.default_announce_var.get()

    # ---------------------------------------------------------------- rules
    def _load_sessions_async(self) -> None:
        """等监视器拿到会话列表后，把它们灌进「提醒范围」。"""
        def work():
            for _ in range(120):
                watcher = getattr(self, "watcher", None)
                if watcher is not None:
                    snap = watcher.sessions()
                    if snap:
                        self.queue.put(("sessions", snap))
                        return
                time.sleep(0.5)
            self.queue.put(("sessions", []))
        threading.Thread(target=work, daemon=True).start()

    def _refresh_rules_tree(self) -> None:
        tree = getattr(self, "rules_tree", None)
        if tree is None or not tree.winfo_exists():
            return
        for iid in tree.get_children():
            tree.delete(iid)
        query = normalize_name(self.rule_search_var.get()) if hasattr(self, "rule_search_var") else ""
        rows = [(u, n, c) for (u, n, c) in getattr(self, "_session_cache", [])
                if not query or query in normalize_name(n)]
        if not rows:
            self.rules_hint.configure(
                text="正在读取会话…" if not getattr(self, "_session_cache", []) else "没有匹配的会话")
            return
        remind = 0
        for user, name, unread in rows:
            on = self.cfg.contact_rules.get(name, self.default_announce_var.get())
            remind += bool(on)
            label = name + (f"   {unread}" if unread else "")
            tree.insert("", "end", iid=user,
                        values=("● 提醒" if on else "○ 静音", label),
                        tags=() if on else ("off",))
        self.rules_hint.configure(text=f"{remind}/{len(rows)} 个提醒")

    def _on_rule_click(self, event):
        if self.rules_tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.rules_tree.identify_column(event.x) != "#1":
            return None
        iid = self.rules_tree.identify_row(event.y)
        if not iid:
            return None
        name = next((n for u, n, _c in self._session_cache if u == iid), None)
        if not name:
            return None
        current = self.cfg.contact_rules.get(name, self.default_announce_var.get())
        self.cfg.contact_rules[name] = not current
        self._refresh_rules_tree()
        self._mark_dirty()
        return "break"

    def _set_all_rules(self, on: bool) -> None:
        query = normalize_name(self.rule_search_var.get()) if hasattr(self, "rule_search_var") else ""
        for _user, name, _unread in getattr(self, "_session_cache", []):
            if query and query not in normalize_name(name):
                continue
            self.cfg.contact_rules[name] = on
        self._refresh_rules_tree()
        self._mark_dirty()

    # -------------------------------------------------------------- runtime
    def _start_watcher(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.watcher = wechat_list.ChatListWatcher(
            lambda m: self.queue.put(("msg", m)),
            lambda s: self.queue.put(("status", s)),
            max(0.6, self.cfg.poll_interval_ms / 1000))
        self.watcher.start()

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "msg":
                    self._on_message(payload)
                elif kind == "status":
                    running = wechat_list.wechat_running()
                    self._set_status(payload, ACCENT_DARK if "运行中" in payload else DANGER)
                    if "运行中" in payload and not running:
                        self.dead_hint.configure(text="微信未登录")
                    elif "运行中" in payload:
                        self.dead_hint.configure(text="")
                elif kind == "sessions":
                    self._session_cache = list(payload or [])
                    self._refresh_rules_tree()
                elif kind == "stop":
                    self._stop_speech()
                    self._flash_hotkey()
                elif kind == "cmd":
                    if payload == "show":
                        self.deiconify()
                        self.lift()
                        self.focus_force()
                    elif payload == "toggle":
                        self.enabled_var.set(not self.enabled_var.get())
                        self._on_toggle()
                    elif payload == "stop":
                        self._stop_speech()
                    elif payload == "quit":
                        self._on_close()
                        return
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _flash_hotkey(self) -> None:
        self.hotkey_status.configure(text="已打断播报", foreground=ACCENT)
        self.after(1500, lambda: self.hotkey_status.configure(text="已生效", foreground=ACCENT_DARK))

    def _on_message(self, msg: dict) -> None:
        chat, sender, content = msg.get("chat", ""), msg.get("sender", ""), msg.get("content", "")
        import datetime
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        preview = (content[:52] + "…") if len(content) > 52 else content

        skip_reason = ""
        rule = self._match_rule(chat, sender)
        allowed = rule[1] if rule else self.default_announce_var.get()
        if not self.enabled_var.get():
            skip_reason = "已暂停"
        elif self.skip_focus_var.get() and wechat_list.wechat_focused():
            skip_reason = "微信在前台"
        elif not allowed:
            skip_reason = f"名单「{rule[0]}」设为不提醒" if rule else "名单外默认不提醒"
        rule_note = (f"  [名单:{rule[0]}={'提醒' if rule[1] else '不提醒'}]" if rule
                     else f"  [名单外: {'提醒' if allowed else '不提醒'}]")
        append_log(f"{chat} | {sender} | {content}" + ("  [全文]" if msg.get("full") else "")
                   + (f"  [跳过: {skip_reason}]" if skip_reason else f"  [播报]{rule_note}"))

        if self.log_box.size() and str(self.log_box.get(0)).startswith("还没有记录"):
            self.log_box.delete(0)
        self.log_box.insert(0, f"{stamp}  {sender}：{preview}" + (f"   ({skip_reason})" if skip_reason else ""))
        if self.log_box.size() > 200:
            self.log_box.delete(200, "end")
        if skip_reason:
            return

        self._announce(sender, chat, content, msg)

    def _announce(self, sender: str, chat: str, content: str, raw: dict | None = None) -> None:
        """Queue one announcement; the worker plays them strictly one after another."""
        template = self.tpl_var.get() or "{sender}说：{content}"
        try:
            text = template.format(sender=sender, chat=chat, content=content)
        except (KeyError, IndexError):
            text = f"{sender}说：{content}"
        text = tts_engine.clean_for_speech(text)
        limit = max(20, self.cfg.max_content_chars)
        if raw and raw.get("full"):
            limit = max(limit, 400)
        if len(text) > limit:
            text = text[:limit] + "…"
        # 同一个人连着发：后面的只念正文，不再重复「某某说」
        short = tts_engine.clean_for_speech(content)
        if len(short) > limit:
            short = short[:limit] + "…"

        path = ""
        if self.cfg.sound_enabled:
            path = sound_player.resolve_sound(self._current_sound_path()) or ""
        item = {
            "text": text,
            "text_short": short,
            "sender": sender,
            "sound": path,
            "sound_vol": float(self.vol_slider.get()),
            "tts_on": bool(self.tts_enabled_var.get()),
            "voice": self._current_voice(),
            "rate": int(self.rate_slider.get()),
            "tts_vol": int(self.tts_vol_slider.get()),
            "token": self._announce_token,
        }
        if self._announce_q.qsize() >= 8:      # a burst of spam should not build a backlog
            return
        self._announce_q.put(item)

    def _announce_worker(self) -> None:
        while True:
            item = self._announce_q.get()
            if item is None:
                continue
            if item["token"] != self._announce_token:
                continue                        # stopped while it was waiting
            now = time.monotonic()
            repeat = (item.get("sender")
                      and item["sender"] == self._last_spoken_sender
                      and now - self._last_spoken_at <= 20.0)
            if repeat:
                item = dict(item, sound="", text=item.get("text_short") or item["text"])
            if item["sound"]:
                try:
                    self.player.play(item["sound"], item["sound_vol"])
                    ms = self.player.duration_ms() or 0
                    time.sleep(max(0.25, min(0.9, ms / 1000.0)))
                except Exception:
                    pass
                if item["token"] != self._announce_token:
                    continue
            if not item["tts_on"]:
                continue
            try:
                self.tts.set_voice(item["voice"])
                self.tts.set_rate(item["rate"])
                self.tts.set_volume(item["tts_vol"])
                self.tts.speak(item["text"], replace=False)
            except Exception:
                continue
            self._last_spoken_sender = item.get("sender", "")
            self._last_spoken_at = time.monotonic()
            # block until this clip has actually finished before starting the next one
            while self.tts.busy() and item["token"] == self._announce_token:
                time.sleep(0.1)
            time.sleep(0.15)

    def _simulate(self) -> None:
        self._on_message({"chat": "测试会话", "sender": "李明", "content": "晚上七点老地方见，别迟到了", "texts": []})

    # --------------------------------------------------------------- saving
    def _collect(self) -> None:
        self.cfg.enabled = self.enabled_var.get()
        self.cfg.sound_path = self._current_sound_path()
        self.cfg.sound_volume = float(self.vol_slider.get())
        self.cfg.tts_enabled = self.tts_enabled_var.get()
        self.cfg.tts_template = self.tpl_var.get() or "{sender}说：{content}"
        self.cfg.tts_voice = self._current_voice()
        self.cfg.tts_rate = int(self.rate_slider.get())
        self.cfg.tts_volume = int(self.tts_vol_slider.get())
        self.cfg.default_announce = self.default_announce_var.get()
        self.cfg.skip_when_wechat_focused = self.skip_focus_var.get()
        self.cfg.full_read = self.full_read_var.get()
        self.cfg.hotkey = self.hotkey_var.get().strip() or self.cfg.hotkey

    def _save(self, silent: bool = False) -> None:
        self._collect()
        try:
            self.cfg.save()
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")
            return
        if silent:
            return
        self.save_hint.configure(text="已保存", foreground=ACCENT_DARK)
        self.after(1800, lambda: self.save_hint.configure(text=""))

    def _mark_dirty(self, *_args) -> None:
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.after(500, self._flush_save)

    def _flush_save(self) -> None:
        self._save_job = None
        self._save(silent=True)
        self.save_hint.configure(text="已自动保存")
        self.after(1200, lambda: self.save_hint.configure(text=""))

    # ---------------------------------------------------------------- tray
    def _post(self, cmd: str) -> None:
        self.queue.put(("cmd", cmd))

    def _make_icon(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((3, 3, 61, 61), radius=15, fill=(7, 193, 96, 255))
        d.rounded_rectangle((15, 19, 49, 41), radius=7, fill=(255, 255, 255, 255))
        d.polygon([(23, 40), (23, 51), (34, 41)], fill=(255, 255, 255, 255))
        return img

    def _setup_tray(self) -> None:
        try:
            import pystray
        except Exception:
            return
        menu = pystray.Menu(
            pystray.MenuItem("打开设置", lambda: self._post("show"), default=True),
            pystray.MenuItem(lambda item: "暂停提醒" if self.enabled_var.get() else "继续提醒",
                             lambda: self._post("toggle")),
            pystray.MenuItem("停止播报", lambda: self._post("stop")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self._post("quit")),
        )
        self.tray = pystray.Icon("wechat_voice_alert", self._make_icon(), APP_TITLE, menu)
        self._tray_thread = threading.Thread(target=self.tray.run, daemon=True)
        self._tray_thread.start()

    def _on_close_button(self) -> None:
        if self.tray is not None:
            self.withdraw()
        else:
            self._on_close()

    def _on_close(self) -> None:
        self._save()
        try:
            self.hotkeys.stop()
            if self.watcher:
                self.watcher.stop()
            self.tts.stop()
            self.player.stop()
            if self.tray is not None:
                self.tray.stop()
        finally:
            self.destroy()


_mutex_handle = None


def single_instance() -> bool:
    """One app per session; a second launch just brings the first window forward."""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    _mutex_handle = kernel32.CreateMutexW(None, False, f"Local\\{RUN_VALUE}.SingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return False
    return True


def main() -> None:
    enable_dpi()
    if "--list" in sys.argv:
        import json
        idx = sys.argv.index("--list")
        out = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else os.path.join(HERE, "sessions.json")
        try:
            from wechatauto import WeChatDB
            db = WeChatDB()
            data = {"self": db.get_self_info(), "sessions": db.get_sessions(limit=30)}
        except Exception as exc:
            data = {"error": repr(exc)}
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return
    if not single_instance():
        return
    app = App()
    if "--page" in sys.argv:
        try:
            app._show_page(sys.argv[sys.argv.index("--page") + 1])
        except (IndexError, KeyError):
            pass
    if "--self-test" in sys.argv:
        app.after(1200, app._simulate)
        app.after(9000, app._on_close)
    app.mainloop()


if __name__ == "__main__":
    main()
