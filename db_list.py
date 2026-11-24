"""消息来源：直接解密微信 4.x 的本地数据库（不需要微信窗口）。

- 密钥从微信进程内存里扫出来（wechatauto-replica 的 WeChatDB 负责），只读；
- 每个会话用 sort_seq 做增量，只取真正的新消息；
- 发送者直接按 wxid 比对，自己发的消息永远不会被当成新消息。
"""
from __future__ import annotations

import re
import threading
import time

_MAX_CHATS = 60          # 轮询的会话上限（按最近活跃排序）
_PREFIX_RE = re.compile(r"^wxid_[0-9a-zA-Z_\-]+:\s*\n?")
_KIND_TEXT = {
    "图片": "发来一张图片", "语音": "发来一条语音", "视频": "发来一个视频",
    "文件": "发来一个文件", "链接": "发来一个链接", "引用": "发来一条引用消息",
    "位置": "发来一个位置", "名片": "发来一张名片", "转账": "发来一个转账",
    "红包": "发来一个红包", "动画表情": "发来一个表情", "表情": "发来一个表情",
}


# ------------------------------------------------------------------ win32 helpers
# 数据库版不需要窗口，这几个函数只给界面上的「正在看微信时不再播报」和状态显示用。
def wechat_running() -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1):
        return False
    try:
        entry = PE()
        entry.dwSize = ctypes.sizeof(PE)
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return False
        while True:
            if entry.szExeFile.lower() in ("weixin.exe", "wechat.exe"):
                return True
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                return False
    finally:
        k32.CloseHandle(snap)
    return False


def find_wechat_window() -> int:
    import ctypes
    u32 = ctypes.windll.user32
    for cls in ("mmui::MainWindow", "Qt51514QWindowIcon"):
        hwnd = u32.FindWindowW(cls, "微信")
        if hwnd:
            return hwnd
    return 0


def _on_screen(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    vx, vy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
    vw, vh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
    return not (r.right <= vx or r.left >= vx + vw or r.bottom <= vy or r.top >= vy + vh)


def wechat_focused() -> bool:
    """微信窗口在前台且真的在屏幕上（数据库版里这一项只影响「正在看微信时不播报」）。"""
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    hwnd = u32.GetForegroundWindow()
    if not hwnd or not _on_screen(hwnd):
        return False
    cls = ctypes.create_unicode_buffer(128)
    u32.GetClassNameW(hwnd, cls, 128)
    return cls.value.startswith(("mmui::", "Qt51514QWindowIcon"))


def park_offscreen(hwnd: int) -> bool:
    return True          # 数据库版不需要挪窗口


def bring_on_screen(hwnd: int) -> bool:
    """如果窗口被上一版留在屏幕外，把它挪回可见区域（不改大小、不抢焦点）。"""
    import ctypes
    from ctypes import wintypes
    if not hwnd:
        return False
    u32 = ctypes.windll.user32
    if _on_screen(hwnd):
        return True
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w = max(400, r.right - r.left)
    h = max(300, r.bottom - r.top)
    vx, vy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
    vw, vh = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
    x = vx + max(0, (vw - w) // 2)
    y = vy + max(0, (vh - h) // 3)
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    u32.SetWindowPos(hwnd, 0, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    return True


class DBWatcher(threading.Thread):
    """和控件树版同样的回调接口，方便共用界面。"""

    def __init__(self, on_message, on_status, interval: float = 1.0,
                 *args, max_chats: int = _MAX_CHATS, **kwargs) -> None:
        super().__init__(name="db-watcher", daemon=True)
        self.on_message = on_message
        self.on_status = on_status
        self.interval = max(0.5, float(interval))
        self.max_chats = max_chats
        self._stop = threading.Event()
        self._db = None
        self._me = ""
        self._last: dict[str, tuple[int, int]] = {}   # chat -> (create_time, local_id)
        self._seq: dict[str, int] = {}                # chat -> 已播报到的 sort_seq
        self._watch: dict[str, tuple] = {}            # chat -> (unread, summary)
        self._cooldown: dict[str, float] = {}         # chat -> 继续观察到这个时刻
        self._last_sweep = 0.0
        self._names: dict[str, str] = {}
        self._status = ""
        self._seeded = False
        self._sessions: list[tuple[str, str, int]] = []
        # 兼容旧界面的参数
        self.auto_restore = lambda: False
        self.stealth = lambda: False
        self.full_read = lambda: False

    # ------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            try:
                self.on_status(text)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    def _open(self) -> bool:
        try:
            from wechatauto import WeChatDB
        except Exception as exc:
            self._set_status(f"缺少 wechatauto：{exc}")
            return False
        try:
            self._db = WeChatDB()
            info = self._db.get_self_info() or {}
            self._me = info.get("username", "")
            return True
        except Exception as exc:
            self._set_status(f"打开微信数据库失败：{type(exc).__name__}")
            return False

    def _chat_name(self, username: str, session: dict | None = None) -> str:
        if session and session.get("nickname"):
            return session["nickname"]
        if username in self._names:
            return self._names[username]
        try:
            name = self._db.get_nickname(username) or username
        except Exception:
            name = username
        self._names[username] = name
        return name

    def _sender_name(self, msg: dict) -> str:
        try:
            wxid = self._db.sender_username(msg.get("shard", ""), msg.get("sender_id"))
        except Exception:
            return ""
        if not wxid:
            return ""
        return self._chat_name(wxid)

    # ------------------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            if self._db is None and not self._open():
                time.sleep(5)
                continue
            if not self._poll_once():
                time.sleep(2)
                continue
            time.sleep(self.interval)

    def _poll_once(self) -> bool:
        try:
            sessions = self._db.get_sessions(limit=self.max_chats)
        except Exception as exc:
            self._set_status(f"读取会话失败：{type(exc).__name__}")
            self._db = None
            return False
        if not sessions:
            self._set_status("等待微信登录")
            return False

        new_count = 0
        snapshot: list[tuple[str, str, int]] = []
        now = time.monotonic()
        sweep = now - getattr(self, "_last_sweep", 0.0) > 15.0
        if sweep:
            self._last_sweep = now
        for idx, session in enumerate(sessions):
            user = session.get("username") or ""
            if not user or user.startswith("gh_") or "brand" in user or "placeholder" in user:
                continue
            chat = self._chat_name(user, session)
            snapshot.append((user, chat, int(session.get("unread") or 0)))
            key = (session.get("unread"), str(session.get("summary") or "")[:24])
            prev = self._watch.get(user)
            self._watch[user] = key
            try:
                if user not in self._seq:
                    latest = self._db.get_messages(user, limit=1)
                    self._seq[user] = int(latest[0].get("sort_seq") or 0) if latest else 0
                    continue                  # 首次只建立基线，不补播历史
                if not self._seq.get(user):
                    continue                  # 没有消息的会话（公众号等），不轮询
                changed = prev != key
                if changed:
                    self._cooldown[user] = now + 4.0
                due = changed or now < self._cooldown.get(user, 0) or (sweep and idx < 15)
                if not due:
                    continue
                msgs = self._db.get_new_messages(user, since_seq=self._seq[user], limit=50)
            except Exception:
                continue
            if not msgs:
                continue
            self._seq[user] = max(int(m.get("sort_seq") or 0) for m in msgs)
            if not self._seeded:
                continue
            for m in msgs:
                if self._is_mine(m):
                    continue
                content = self._content(m)
                if not content:
                    continue
                sender = self._sender_name(m) or chat
                self.on_message({"chat": chat, "sender": sender, "content": content,
                                 "source": "db", "full": True, "seq": m.get("sort_seq")})
                new_count += 1

        self._seeded = True
        self._sessions = snapshot
        self._set_status(f"运行中 · 数据库 · {len(sessions)} 个会话"
                         + (f" · 新消息 {new_count}" if new_count else ""))
        return True

    def sessions(self) -> list[tuple[str, str, int]]:
        """最近一次轮询看到的会话：[(username, 名称, 未读)]，供设置页直接选用。"""
        return list(self._sessions)

    def _is_mine(self, msg: dict) -> bool:
        try:
            who = self._db.sender_username(msg.get("shard", ""), msg.get("sender_id"))
        except Exception:
            return False
        return bool(self._me) and who == self._me

    @staticmethod
    def _content(msg: dict) -> str:
        kind = str(msg.get("type") or "")
        text = _PREFIX_RE.sub("", str(msg.get("content") or "").strip()).strip()
        if kind and kind != "文本":
            return _KIND_TEXT.get(kind, "发来一条消息")
        if not text or text.startswith("<"):
            return "发来一条消息"
        return text


# 兼容旧代码里的名字
ChatListWatcher = DBWatcher


if __name__ == "__main__":
    w = DBWatcher(lambda m: print("MSG", m, flush=True),
                  lambda s: print("STATUS", s, flush=True), 1.2)
    w.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()
