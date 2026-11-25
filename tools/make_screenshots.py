"""Generate anonymised README screenshots (2x for sharpness)."""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes

os.environ["WCA_UI_SCALE"] = "2"

from PIL import Image  # noqa: E402

import app as A  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "screenshots")
os.makedirs(OUT, exist_ok=True)

subprocess.run(["powershell", "-NoProfile", "-Command",
                "Get-Process WeChatDBAlert -ErrorAction SilentlyContinue | "
                "ForEach-Object { Stop-Process -Id $_.Id -Force }"], capture_output=True)

a = A.App()
a.cfg.save = lambda: None            # never persist demo data
a.cfg.theme = "midnight"
a._rebuild()
a.update()
a.geometry("+0+0")
a.enabled_var.set(True)
a.update()

# fake, anonymised session list
a._session_cache = [
    ("demo_1", "张三", 2), ("demo_2", "项目讨论组", 17), ("demo_3", "李四", 0),
    ("demo_4", "家人群", 5), ("demo_5", "外卖小哥", 1), ("demo_6", "摄影交流群", 38),
    ("demo_7", "王五", 0), ("demo_8", "课程通知", 3), ("demo_9", "羽毛球局", 9),
]
a.cfg.contact_rules = {"张三": True, "项目讨论组": True}
a.default_announce_var.set(False)
a._refresh_rules_tree()

# demo log lines for the 最近消息 page
a.log_box.delete(0, "end")
for line in (
    "21:42:07  张三：资料我放群里了，明早看   (提醒)",
    "21:41:52  项目讨论组：下周三下午两点评审   (提醒)",
    "21:40:10  摄影交流群：这组夜景太好看了   (名单外不提醒)",
    "21:38:44  李四：收到，我明早回你   (名单外不提醒)",
    "21:35:02  家人群：周末回来吃饭吗   (名单外不提醒)",
):
    a.log_box.insert("end", line)

a._show_page("sound")
a.cfg.sound_path = "builtin:f1_start"
a.sound_var.set("F1 起步")
a.sound_hint.configure(text="当前正在使用：F1 起步")
a.status_label.set_text("运行中 · 数据库 · 9 个会话", True)
a.update()


U = ctypes.windll.user32
G = ctypes.windll.gdi32
U.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
U.GetWindowDC.restype = wintypes.HDC
U.GetWindowDC.argtypes = [wintypes.HWND]
U.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
G.CreateCompatibleDC.restype = wintypes.HDC
G.CreateCompatibleDC.argtypes = [wintypes.HDC]
G.CreateCompatibleBitmap.restype = wintypes.HBITMAP
G.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
G.SelectObject.restype = wintypes.HGDIOBJ
G.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
G.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]


def capture(hwnd, path):
    r = wintypes.RECT()
    U.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = U.GetWindowDC(hwnd)
    mdc = G.CreateCompatibleDC(hdc)
    bmp = G.CreateCompatibleBitmap(hdc, w, h)
    G.SelectObject(mdc, bmp)
    U.PrintWindow(hwnd, mdc, 2)

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]
    bi = BIH()
    bi.biSize = ctypes.sizeof(BIH)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    G.GetDIBits(mdc, bmp, 0, h, ctypes.byref(buf), ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB").save(path)
    print("saved", path, (w, h))


hwnd = int(a.wm_frame(), 16)
for page in ("sound", "filter", "voice", "theme", "recent"):
    a._show_page(page)
    a.status_label.set_text("运行中 · 数据库 · 9 个会话", True)
    a.update()
    a.update_idletasks()
    time.sleep(0.5)
    capture(hwnd, os.path.join(OUT, f"{page}.png"))

a._announce_token += 1
a.destroy()
