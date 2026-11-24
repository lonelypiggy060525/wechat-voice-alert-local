"""微信语音提醒 - WeChat Voice Alert.

Replaces WeChat's default notification sound with one you choose, speaks who messaged
you and what they said, and lets a global hotkey cut the speech short.

Data source: Windows notification center is useless for WeChat 4.x, so this reads the
chat list off the window (PrintWindow + RapidOCR). No injection, no DB access.
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

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

# 深色配色（参考「岛仔」的暗色系：近黑底、低饱和描边、蓝色主色、绿=在线/红=警告）
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

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TINY = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")
FONT_H1 = ("Microsoft YaHei UI", 15, "bold")
FONT_NAV = ("Microsoft YaHei UI", 10)

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
        round_rect(self, 0, y - 3, self._cw, y + 3, 3, fill="#242429", outline="")
        x = self._x_of(self._value)
        if x > 2:
            round_rect(self, 0, y - 3, x, y + 3, 3, fill=ACCENT, outline="")
        self.create_oval(x - 8, y - 8, x + 8, y + 8, fill=CARD, outline=ACCENT, width=2)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
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
        self.geometry("920x740")
        self.minsize(860, 680)
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

            on = ctypes.c_int(1)
            dwm = ctypes.windll.dwmapi
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
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#2A2A31")],
                  foreground=[("disabled", DIM)])
        style.configure("TRadiobutton", background=CARD, foreground=TEXT, font=FONT)
        style.map("TRadiobutton", background=[("active", CARD)],
                  indicatorcolor=[("selected", ACCENT), ("!selected", "#2A2A31")])
        style.configure("TButton", font=FONT, padding=(12, 5), relief="flat",
                        background="#1E1E24", foreground=TEXT, borderwidth=0)
        style.map("TButton", background=[("active", "#26262E"), ("pressed", "#2E2E37")])
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
        style.configure("TCombobox", fieldbackground=FIELD, background="#1E1E24", foreground=TEXT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        arrowcolor=MUTED, padding=5, relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", FIELD), ("focus", FIELD)],
                  background=[("active", "#26262E"), ("readonly", "#1E1E24")],
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
        style.configure("Treeview.Heading", background="#191920", foreground=MUTED,
                        font=FONT_SMALL, relief="flat", borderwidth=0)
        style.map("Treeview.Heading", background=[("active", "#202028")])
        style.map("Treeview", background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", gripcount=0, background="#2E2E36",
                        troughcolor=CARD, bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                        arrowcolor=MUTED, arrowsize=12, borderwidth=0)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#3A3A44"), ("pressed", "#3A3A44")],
                  arrowcolor=[("active", ACCENT)])

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        header = tk.Frame(self, bg=CARD, height=62)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        tk.Frame(header, bg=BORDER, height=1).pack(side="bottom", fill="x")

        logo = tk.Canvas(header, width=34, height=34, bg=CARD, highlightthickness=0)
        logo.pack(side="left", padx=(18, 10), pady=14)
        round_rect(logo, 1, 1, 33, 33, 10, fill=ACCENT, outline="")
        logo.create_oval(9, 8, 25, 21, fill="white", outline="")
        logo.create_polygon([12, 19, 12, 26, 19, 20], fill="white", outline="")

        titles = tk.Frame(header, bg=CARD)
        titles.pack(side="left", pady=10)
        tk.Label(titles, text=APP_TITLE, bg=CARD, fg=TEXT, font=FONT_H1).pack(anchor="w")
        tk.Label(titles, text="微信来消息 → 自定义提示音 + 语音播报", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w")

        self.status_label = tk.Label(header, text="● 启动中", bg=CARD_HOVER, fg=MUTED,
                                     font=FONT_SMALL, padx=12, pady=5)
        self.status_label.pack(side="right", padx=(0, 18))
        self.enabled_var = tk.BooleanVar(value=self.cfg.enabled)
        self._check(header, "启用提醒", self.enabled_var, self._on_toggle).pack(side="right", padx=14)

        footer = tk.Frame(self, bg=CARD, height=56)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(side="bottom", fill="x")

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self._check(footer, "开机自动启动", self.autostart_var, self._on_autostart).pack(
            side="left", padx=18)
        self.dead_hint = tk.Label(footer, text="", bg=CARD, fg=DANGER, font=FONT_SMALL)
        self.dead_hint.pack(side="left", padx=6)
        self.save_hint = tk.Label(footer, text="", bg=CARD, fg=MUTED, font=FONT_SMALL)
        self.save_hint.pack(side="left", padx=6)
        ttk.Button(footer, text="保存并应用", style="Accent.TButton",
                   command=lambda: self._save()).pack(side="right", padx=18, pady=10)

        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        sidebar = tk.Frame(body, bg=BG, width=196)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        content = tk.Frame(body, bg=BG)
        content.pack(side="left", fill="both", expand=True)

        self._nav: dict[str, dict] = {}
        tk.Label(sidebar, text="设置", bg=BG, fg=DIM, font=FONT_TINY,
                 anchor="w", padx=20, pady=10).pack(fill="x")
        for key, label in (("sound", "提示音"), ("voice", "语音播报"), ("filter", "提醒范围"),
                           ("hotkey", "快捷键"), ("recent", "最近消息")):
            self._nav_item(sidebar, key, label)
        tk.Label(sidebar, text="数据来源：本地数据库\n（不需要微信窗口）", bg=BG,
                 fg=DIM, font=FONT_TINY, justify="left",
                 anchor="w", padx=20).pack(side="bottom", fill="x", pady=12)

        self.pages: dict[str, tk.Frame] = {}
        for key in ("sound", "voice", "filter", "hotkey", "recent"):
            page = tk.Frame(content, bg=BG)
            self.pages[key] = page

        self._build_sound_page(self.pages["sound"])
        self._build_voice_page(self.pages["voice"])
        self._build_filter_page(self.pages["filter"])
        self._build_hotkey_page(self.pages["hotkey"])
        self._build_recent_page(self.pages["recent"])
        self._show_page("sound")

    def _nav_item(self, parent: tk.Frame, key: str, text: str) -> None:
        row = tk.Frame(parent, bg=BG, cursor="hand2")
        row.pack(fill="x")
        bar = tk.Frame(row, bg=BG, width=3)
        bar.pack(side="left", fill="y")
        label = tk.Label(row, text=text, bg=BG, fg=MUTED, font=FONT_NAV,
                         anchor="w", padx=16, pady=9)
        label.pack(side="left", fill="x", expand=True)
        for widget in (row, label):
            widget.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            widget.bind("<Enter>", lambda _e, r=row, k=key: self._nav_paint(k, hover=True))
            widget.bind("<Leave>", lambda _e, r=row, k=key: self._nav_paint(k, hover=False))
        self._nav[key] = {"row": row, "bar": bar, "label": label}

    def _nav_paint(self, key: str, hover: bool = False) -> None:
        item = self._nav[key]
        active = key == self._page
        bg = ACCENT_SOFT if active else (CARD_HOVER if hover else BG)
        fg = ACCENT if active else (TEXT if hover else MUTED)
        for widget in (item["row"], item["label"]):
            widget.configure(bg=bg)
        item["label"].configure(fg=fg)
        item["bar"].configure(bg=ACCENT if active else bg)

    def _show_page(self, key: str) -> None:
        self._page = key
        for name, page in self.pages.items():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for name in self._nav:
            self._nav_paint(name)

    def _card(self, parent: tk.Frame, title: str, subtitle: str = "") -> tk.Frame:
        shell = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        shell.pack(fill="x", pady=(0, 14))
        inner = tk.Frame(shell, bg=CARD)
        inner.pack(fill="both", expand=True, padx=22, pady=18)
        tk.Label(inner, text=title, bg=CARD, fg=TEXT, font=FONT_TITLE, anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(inner, text=subtitle, bg=CARD, fg=MUTED, font=FONT_SMALL,
                     anchor="w", justify="left").pack(fill="x", pady=(4, 14))
        else:
            tk.Frame(inner, bg=CARD, height=12).pack(fill="x")
        return inner

    def _field_row(self, parent: tk.Frame, label: str, width: int = 14) -> tk.Frame:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=5)
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
        wrap.pack(fill="both", expand=True, padx=18, pady=16)

        card = self._card(wrap, "提示音", "来消息时先响这个声音，随后再念出是谁说了什么。")
        row = self._field_row(card, "音效")
        self.sound_var = tk.StringVar()
        self.sound_box = ttk.Combobox(row, textvariable=self.sound_var, state="readonly", width=30)
        self.sound_box.pack(side="left")
        self.sound_box.bind("<<ComboboxSelected>>", lambda _e: self._on_sound_pick())
        ttk.Button(row, text="试听", command=self._preview_sound).pack(side="left", padx=8)
        ttk.Button(row, text="删除", width=6, command=self._remove_sound).pack(side="left")

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
        tk.Label(card, text="支持 wav / mp3 / wma；m4a、aac、flac、ogg 首次选择时自动转码。"
                            "加过的音频会一直保留在列表里。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=560).pack(fill="x", pady=(2, 0))

        tips = self._card(wrap, "它怎么读到消息的？",
                          "微信把聊天记录存在本地数据库里（SQLCipher 加密）。程序从微信进程内存里"
                          "取出主密钥，在内存中解密后直接读库——不截图、不用 OCR、不看界面。")
        tk.Label(tips, text="所以微信开不开窗口、最小化、关到托盘都无所谓；「谁发的」直接按发送者 "
                            "wxid 判断，你自己发的消息永远不会被念。全部在本机完成，不上传任何内容。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=560).pack(fill="x")

        self._refresh_sound_list()

    def _build_voice_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        card = self._card(wrap, "语音播报", "把「谁发的、说了什么」念出来。")

        self.tts_enabled_var = tk.BooleanVar(value=self.cfg.tts_enabled)
        self._check(card, "朗读消息内容", self.tts_enabled_var, self._mark_dirty).pack(anchor="w")

        trow = self._field_row(card, "播报格式")
        self.tpl_var = tk.StringVar(value=self.cfg.tts_template)
        self.tpl_var.trace_add("write", self._mark_dirty)
        ttk.Entry(trow, textvariable=self.tpl_var, width=44).pack(side="left")
        tk.Label(card, text="可用占位符：{sender} 发送者　{chat} 会话名　{content} 内容",
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
                     state="readonly", width=32).pack(side="left")
        ttk.Button(vrow, text="试听", command=self._preview_tts).pack(side="left", padx=8)

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
        btns.pack(fill="x", pady=(8, 0))
        self.full_read_var = tk.BooleanVar(value=self.cfg.full_read)
        self._check(btns, "朗读完整内容（长消息会打开会话去读，代价是该会话变成已读）",
                    self.full_read_var, self._mark_dirty).pack(anchor="w")
        btns2 = tk.Frame(card, bg=CARD)
        btns2.pack(fill="x", pady=(10, 0))
        ttk.Button(btns2, text="停止播报", command=self._stop_speech).pack(side="left")
        tk.Label(btns2, text="快捷键也能随时打断", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=10)

    def _build_filter_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        card = self._card(wrap, "提醒范围", "每个联系人一个开关，点第一列切换；名单外的人按下面的默认处理。")

        self.default_announce_var = tk.BooleanVar(value=self.cfg.default_announce)
        self._check(card, "名单外的联系人默认提醒", self.default_announce_var,
                    self._mark_dirty).pack(anchor="w")

        tree_wrap = tk.Frame(card, bg=CARD)
        tree_wrap.pack(fill="both", expand=True, pady=(10, 0))
        self.rules_tree = ttk.Treeview(tree_wrap, columns=("state", "name"), show="headings",
                                       height=9, selectmode="browse")
        self.rules_tree.heading("state", text="提醒")
        self.rules_tree.heading("name", text="联系人 / 群名（点第一列切换）")
        self.rules_tree.column("state", width=76, anchor="center", stretch=False)
        self.rules_tree.column("name", width=420, anchor="w")
        scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.rules_tree.yview)
        self.rules_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.rules_tree.pack(side="left", fill="both", expand=True)
        self.rules_tree.tag_configure("off", foreground=DIM)
        self.rules_tree.bind("<Button-1>", self._on_rule_click)
        self._refresh_rules_tree()

        tools = tk.Frame(card, bg=CARD)
        tools.pack(fill="x", pady=(12, 0))
        ttk.Button(tools, text="添加联系人…", command=self._add_rule).pack(side="left")
        ttk.Button(tools, text="删除", width=6,
                   command=self._remove_rule).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="全开", width=6,
                   command=lambda: self._set_all_rules(True)).pack(side="left", padx=(8, 0))
        ttk.Button(tools, text="全关", width=6,
                   command=lambda: self._set_all_rules(False)).pack(side="left", padx=(8, 0))
        tk.Label(tools, text="支持 emoji 名字，改动自动保存", bg=CARD, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=10)

        opts = tk.Frame(card, bg=CARD)
        opts.pack(fill="x", pady=(14, 0))
        self.skip_focus_var = tk.BooleanVar(value=self.cfg.skip_when_wechat_focused)
        self._check(opts, "正在看微信时不再播报", self.skip_focus_var,
                    self._mark_dirty).pack(anchor="w")
        # UIA 读的是控件树，不依赖窗口是否可见，这两项只为兼容旧配置保留
        self.auto_restore_var = tk.BooleanVar(value=False)
        self.stealth_var = tk.BooleanVar(value=False)
        tk.Label(opts, text="数据库版与窗口无关：微信开着就行，最小化 / 关到托盘 / 不开窗口都能读。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w").pack(anchor="w", pady=(6, 0))

    def _on_stealth_toggle(self) -> None:
        self._mark_dirty()
        hwnd = wechat_list.find_wechat_window()
        if not hwnd:
            return
        if self.stealth_var.get():
            wechat_list.park_offscreen(hwnd)
        else:
            wechat_list.bring_on_screen(hwnd)

    def _bring_wechat_back(self) -> None:
        hwnd = wechat_list.find_wechat_window()
        if not hwnd:
            self._set_status("没找到微信窗口", ok=False)
            return
        wechat_list.bring_on_screen(hwnd)
        if self.stealth_var.get():
            self.stealth_var.set(False)
            self._mark_dirty()
        self._set_status("微信已调回屏幕")

    def _build_hotkey_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        card = self._card(wrap, "打断快捷键", "播报到一半按一下就能掐断。")

        row = self._field_row(card, "组合键")
        self.hotkey_var = tk.StringVar(value=self.cfg.hotkey)
        ttk.Entry(row, textvariable=self.hotkey_var, width=22).pack(side="left")
        ttk.Button(row, text="应用", command=self._apply_hotkey).pack(side="left", padx=8)
        self.hotkey_status = tk.Label(row, text="", bg=CARD, fg=ACCENT_DARK, font=FONT_SMALL)
        self.hotkey_status.pack(side="left", padx=6)

        tk.Label(card, text="写法示例：ctrl+alt+q、ctrl+shift+F9、alt+space。"
                            "组合键被别的程序占用时会提示，换一个即可。",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w",
                 wraplength=560, justify="left").pack(fill="x", pady=(12, 0))

    def _build_recent_page(self, page: tk.Frame) -> None:
        wrap = tk.Frame(page, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
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
        ttk.Button(btns, text="模拟一条消息", command=self._simulate).pack(side="left")
        ttk.Button(btns, text="打开 Windows 通知设置",
                   command=lambda: os.startfile("ms-settings:notifications")).pack(side="left", padx=8)
        tk.Label(card, text="每次播报也会追加写入 last_messages.log（在 %APPDATA%\\WeChatDBAlert 下）",
                 bg=CARD, fg=MUTED, font=FONT_SMALL, anchor="w").pack(fill="x", pady=(8, 0))

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
        if color == DANGER or color.startswith("#c0"):
            ok = False
        self.status_label.configure(text=f"● {text}", bg=GREEN_SOFT if ok else DANGER_SOFT,
                                    fg=GREEN if ok else DANGER)

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
    def _refresh_rules_tree(self) -> None:
        for iid in self.rules_tree.get_children():
            self.rules_tree.delete(iid)
        items = sorted(self.cfg.contact_rules.items(), key=lambda kv: (not kv[1], kv[0].casefold()))
        for name, on in items:
            self.rules_tree.insert("", "end", iid=name,
                                   values=("● 提醒" if on else "○ 不提醒", name),
                                   tags=() if on else ("off",))

    def _on_rule_click(self, event):
        if self.rules_tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.rules_tree.identify_column(event.x) != "#1":
            return None
        iid = self.rules_tree.identify_row(event.y)
        if not iid:
            return None
        self.cfg.contact_rules[iid] = not self.cfg.contact_rules.get(iid, True)
        self._refresh_rules_tree()
        self.rules_tree.selection_set(iid)
        state = "提醒" if self.cfg.contact_rules[iid] else "不提醒"
        append_log(f"名单: {iid} → {state}")
        self._mark_dirty()
        return "break"

    def _add_rule(self) -> None:
        name = simpledialog.askstring(APP_TITLE, "输入聊天名或发送者名（可含 emoji）：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.cfg.contact_rules:
            messagebox.showinfo(APP_TITLE, f"「{name}」已在名单里，直接点第一列切换就行。")
            return
        self.cfg.contact_rules[name] = True
        self._refresh_rules_tree()
        self.rules_tree.selection_set(name)
        self.rules_tree.see(name)
        append_log(f"名单: 新增 {name} → 提醒")
        self._mark_dirty()

    def _remove_rule(self) -> None:
        selected = self.rules_tree.selection()
        if not selected:
            messagebox.showinfo(APP_TITLE, "先在列表里选中一行。")
            return
        for iid in selected:
            self.cfg.contact_rules.pop(iid, None)
            append_log(f"名单: 删除 {iid}")
        self._refresh_rules_tree()
        self._mark_dirty()

    def _set_all_rules(self, on: bool) -> None:
        for name in list(self.cfg.contact_rules):
            self.cfg.contact_rules[name] = on
        self._refresh_rules_tree()
        append_log(f"名单: 全部设为{'提醒' if on else '不提醒'}")
        self._mark_dirty()

    # -------------------------------------------------------------- runtime
    def _start_watcher(self) -> None:
        if self.watcher:
            self.watcher.stop()
        self.watcher = wechat_list.ChatListWatcher(
            lambda m: self.queue.put(("msg", m)),
            lambda s: self.queue.put(("status", s)),
            max(0.6, self.cfg.poll_interval_ms / 1000),
            lambda: self.auto_restore_var.get(),
            lambda: self.stealth_var.get(),
            lambda: self.full_read_var.get())
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
        self.cfg.auto_restore_window = self.auto_restore_var.get()
        self.cfg.stealth_mode = self.stealth_var.get()
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
