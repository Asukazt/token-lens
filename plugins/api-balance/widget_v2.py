# -*- coding: utf-8 -*-
"""Token Lens: a live usage and balance window docked to ChatGPT/Codex."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import copy
import json
import math
import os
import socket
import sys
import threading
import time
import traceback

import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime
from tkinter import messagebox, simpledialog

from PIL import Image, ImageDraw, ImageFont, ImageTk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_PATH = os.path.join(HERE, "widget.log")
STATE_PATH = os.path.join(HERE, ".widget_state.json")
CONTROL_PORT = int(os.environ.get("API_BALANCE_WIDGET_PORT") or 49983)
PLUGIN_ID = "api-balance@local"

WIDTH, HEIGHT = 390, 660
COMPACT_WIDTH, COMPACT_HEIGHT = 118, 30
MARGIN = 16
EDGE = 7
SCALE = 2

TRANSPARENT = "#010203"
INK = "#0D1013"
INK_2 = "#12161A"
PANEL = "#171C21"
PANEL_2 = "#1D232A"
BORDER = "#29323A"
TEXT = "#F4F0E8"
MUTED = "#929A9F"
DIM = "#606970"
CORAL = "#FF7557"
MINT = "#48D6A1"
GOLD = "#E7B45C"
BLUE = "#76A8FF"
BAD = "#F06A74"

FONT_CN = "C:/Windows/Fonts/msyh.ttc"
FONT_CN_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_NUM = "C:/Windows/Fonts/seguisb.ttf"
FONT_UI = "Microsoft YaHei UI"
# 只认 ChatGPT / Codex 主窗口。加上 codex-plus-plus.exe 会让它抢宿主：
# 它窗口更大时悬浮窗会跟着跑到 Codex++ 上并被抬到它前面。
HOST_PROCESSES = {"chatgpt.exe", "codex.exe"}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
user32.SetWindowPos.restype = ctypes.c_bool
try:
    winmm = ctypes.WinDLL("winmm")
except OSError:
    winmm = None

_FONTS = {}


def log_exc(text):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def _font(size, bold=False, numeric=False):
    path = FONT_NUM if numeric else FONT_CN_BOLD if bold else FONT_CN
    key = (path, int(size))
    if key not in _FONTS:
        _FONTS[key] = ImageFont.truetype(path, max(6, int(size)))
    return _FONTS[key]


def _process_name(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _foreground_pid():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _rect(hwnd):
    box = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(box))
    return box.left, box.top, box.right, box.bottom


def find_host_hwnd():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        left, top, right, bottom = _rect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return True
        if user32.GetWindow(hwnd, 4) or not user32.GetWindowTextLengthW(hwnd):
            return True
        if int(user32.GetWindowLongPtrW(hwnd, -20) or 0) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_name(pid.value).lower() in HOST_PROCESSES:
            found.append((width * height, hwnd))
        return True

    user32.EnumWindows(callback, 0)
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def find_host_window():
    hwnd = find_host_hwnd()
    return _rect(hwnd) if hwnd else None


def host_alive(hwnd):
    return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd)) \
        and not user32.IsIconic(hwnd)


def read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_config(config):
    temp = CONFIG_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    os.replace(temp, CONFIG_PATH)


def write_state(payload):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        pass


def plugin_enabled(path):
    try:
        import tomllib
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
        entry = (data.get("plugins") or {}).get(PLUGIN_ID)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("enabled", False))
    except (OSError, ValueError):
        return None


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _short_tokens(value):
    value = int(_num(value, 0))
    if value >= 1_000_000:
        return "%.2fM" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%.1fk" % (value / 1_000.0)
    return str(value)


def _money(value, digits=2):
    if value is None:
        return "--"
    return ("%%.%df" % digits) % float(value)


def _short_model(name):
    if not name:
        return "未知模型"
    clean = name.split("/")[-1]
    replacements = (
        ("deepseek-", "DeepSeek "),
        ("kimi-", "Kimi "),
        ("glm-", "GLM "),
        ("qwen", "Qwen "),
    )
    for old, new in replacements:
        clean = clean.replace(old, new)
    return clean.replace("-1017", " 1017")


def _duration_short(seconds):
    seconds = max(0, int(_num(seconds, 0)))
    if seconds >= 3600:
        hours, minutes = divmod(seconds // 60, 60)
        return "%d小时%d分" % (hours, minutes) if minutes else "%d小时" % hours
    if seconds >= 60:
        return "%d分" % (seconds // 60)
    return "即将切换"


def _days_left(stamp):
    try:
        due = datetime.strptime(str(stamp)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (due - date.today()).days


class Widget:
    def __init__(self, control_socket, plugin_config=None):
        self._srv = control_socket
        self.plugin_config = plugin_config
        self._last_plugin_check = 0.0
        self._disabled_checks = 0
        self.config = read_config()
        self.ui = dict(self.config.get("ui") or {})
        self.follow = bool(self.ui.get("follow", True))
        self.refresh_sec = max(1.0, _num(self.ui.get("refresh_sec"), 2.0))
        self.balance_refresh_sec = max(3.0, _num(self.ui.get("balance_refresh_sec"), 8.0))
        self.texture = bool(self.ui.get("texture", True))
        self.compact = bool(self.ui.get("compact", False))
        saved_pos = self.ui.get("pos")
        saved_compact = self.ui.get("compact_pos")
        saved_pos = list(saved_pos) if isinstance(saved_pos, list) and len(saved_pos) == 2 else []
        saved_compact = (
            list(saved_compact)
            if isinstance(saved_compact, list) and len(saved_compact) == 2
            else []
        )
        self._has_saved_position = bool(saved_pos or saved_compact)
        self.compact_pos = saved_compact or (saved_pos if self.compact else [])
        self.position = (
            list(self.compact_pos or saved_pos) if self.compact else list(saved_pos)
        ) or [0, 0]
        self.view = self.ui.get("view") if self.ui.get("view") in (
            "overview", "conversations", "usage", "settings"
        ) else "overview"
        self.size = (COMPACT_WIDTH, COMPACT_HEIGHT) if self.compact else (WIDTH, HEIGHT)
        self.snapshot = {}
        self.last_data = {}
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as handle:
                cached = json.load(handle).get("data")
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("text") and cached.get("usage"):
            self.snapshot = cached
        self.last_error = ""
        self.visible = True
        self.busy = False
        self.fetching = False
        self._host_origin = None
        self._host_hwnd = None
        self._host_scan_at = 0.0
        self._last_z_order = 0.0
        self._front_pid = None
        self._front_name = ""
        self._tick_count = 0
        self._tick_window = time.monotonic()
        self._tick_hz = 0.0
        self._high_timer = False
        self._geom = None
        self._hits = []
        self._entries = {}
        self._settings_status = ""
        self._settings_status_color = MUTED
        self._refresh_status = ""
        self._refresh_job = None
        self.idle_enabled = False
        self.idle_windows = []
        self.idle_weekend = False
        self._idle_enabled_loaded_for = None
        self.idle_editor_open = False
        self._provider_menu = False
        self._model_prices = None
        self._time_pick = None
        self._time_hour = 0
        self._time_minute = 0
        self._press = None
        self._dragging = False
        self._drag_origin = None
        self._drag_win = None
        self._hold_job = None
        self._position_save_job = None
        self._hover_hit = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Token Lens")
        icon = os.path.join(HERE, "icon.ico")
        if os.path.exists(icon):
            try:
                self.root.iconbitmap(icon)
            except tk.TclError:
                pass
        self.root.overrideredirect(True)
        try:
            # 工具窗口：不出现在任务栏和 Alt+Tab / 任务视图里
            self.root.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self.root.attributes("-topmost", False)
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        self._apply_alpha()

        self.canvas = tk.Canvas(
            self.root,
            width=self.size[0],
            height=self.size[1],
            bg=TRANSPARENT,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        self.photo = None
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")

        self._bind()
        self._build_menu()
        rect = find_host_window()
        if rect:
            target = (
                self.position
                if self._has_saved_position and not self.follow
                else self._default_position(rect)
            )
            display_position = self._clamp(target[0], target[1], rect)
            self.position = (
                list(target) if self.compact and not self.follow
                and self._has_saved_position
                else list(display_position)
            )
            if self.compact and (self.follow or not self._has_saved_position):
                self.compact_pos = list(self.position)
            self._host_origin = (rect[0], rect[1])
        else:
            display_position = self.position
        self._apply_geometry(*display_position)
        self._render()
        self.root.deiconify()
        threading.Thread(target=self._accept_loop, daemon=True).start()
        write_state({"running": True, "pid": os.getpid(), "visible": True})
        self.root.after(220, self.tick)
        self.root.after(300, self.refresh)
        self.root.after(int(self.refresh_sec * 1000), self._periodic_refresh)

    # --------------------------------------------------------------- lifecycle

    def _accept_loop(self):
        while True:
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            with conn:
                try:
                    command = conn.recv(64).decode("utf-8", "replace").strip().lower()
                except OSError:
                    continue
                try:
                    if command == "ping":
                        conn.sendall(b"pong")
                    elif command == "quit":
                        conn.sendall(b"bye")
                        self.root.after(0, self.quit)
                    elif command == "refresh":
                        conn.sendall(b"ok")
                        self.root.after(0, lambda: self.refresh(force_balance=True))
                    elif command == "collapse":
                        conn.sendall(b"ok")
                        self.root.after(0, self.set_compact)
                    elif command == "expand":
                        conn.sendall(b"ok")
                        self.root.after(0, self.set_expanded)
                    elif command == "reload":
                        conn.sendall(b"ok")
                        self.root.after(120, self._reload)
                    elif command.startswith("view:"):
                        conn.sendall(b"ok")
                        view = command.split(":", 1)[1]
                        self.root.after(0, lambda v=view: self.set_view(v))
                    elif command == "status":
                        payload = json.dumps(self._state(), ensure_ascii=False).encode("utf-8")
                        conn.sendall(payload)
                    else:
                        conn.sendall(b"unknown")
                except OSError:
                    continue

    def _state(self):
        host = find_host_window()
        return {
            "running": True,
            "pid": os.getpid(),
            "visible": self.visible,
            "compact": self.compact,
            "view": self.view,
            "busy": self.busy,
            "host": list(host) if host else [],
            "front": _process_name(_foreground_pid()),
            "tick_hz": round(self._tick_hz, 1),
            "last_error": self.last_error,
            "data": self.last_data,
        }

    def _reload(self):
        try:
            self._srv.close()
        except OSError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        try:
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
        except OSError:
            log_exc(traceback.format_exc())

    # --------------------------------------------------------------- drawing
    def _draw_image(self, width, height, draw_fn):
        w, h = width * SCALE, height * SCALE
        compact = width == COMPACT_WIDTH
        content = self._compact_background(w, h) if compact else self._background(w, h)
        d = ImageDraw.Draw(content)
        self._hits = []
        if compact:
            self._draw_compact(d, width, height)
        else:
            self._draw_header(d, width)
            if self.view == "settings" and getattr(self, "_model_prices", None):
                self._draw_model_prices(d, width, height)
            elif self.view == "settings" and self.idle_editor_open:
                self._draw_idle_editor(d, width, height)
                if self._time_pick:
                    self._draw_time_picker(d, width, height)
            elif self.view == "settings":
                self._draw_settings(d, width, height)
                if self._provider_menu:
                    self._draw_provider_menu(d, width, height)
            elif self.view == "usage":
                self._draw_usage(d, width, height)
            elif self.view == "conversations":
                self._draw_conversations(d, width, height)
            else:
                self._draw_overview(d, width, height)
            self._draw_nav(d, width, height)
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=(9 if compact else 17) * SCALE, fill=255
        )
        out = Image.new("RGB", (w, h), TRANSPARENT)
        out.paste(content, (0, 0), mask)
        border = ImageDraw.Draw(out)
        border.rounded_rectangle(
            [SCALE // 2, SCALE // 2, w - SCALE // 2 - 1, h - SCALE // 2 - 1],
            radius=(9 if compact else 17) * SCALE,
            outline="#30363A" if compact else BORDER,
            width=max(1, SCALE // 2),
        )
        image = out.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfigure(self._image_item, image=self.photo)

    def _compact_background(self, w, h):
        img = Image.new("RGB", (w, h), (18, 21, 23))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(1, h - 1)
            d.line([(0, y), (w, y)], fill=(
                int(18 + 3 * t),
                int(21 + 2 * t),
                int(23 + t),
            ))
        return img

    def _background(self, w, h):
        img = Image.new("RGB", (w, h), INK)
        d = ImageDraw.Draw(img)
        top = (14, 17, 20)
        bottom = (21, 16, 15)
        for y in range(h):
            t = y / max(1, h - 1)
            d.line(
                [(0, y), (w, y)],
                fill=(
                    int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t),
                ),
            )
        if self.texture:
            overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for line_no in range(11):
                base_y = (h * (0.11 + line_no * 0.075))
                points = []
                for x in range(-20, w + 20, 6):
                    y = base_y + math.sin(x / (95 * SCALE) + line_no * 0.85) * (14 * SCALE)
                    y += math.sin(x / (41 * SCALE) + line_no) * (4 * SCALE)
                    points.append((x, y))
                od.line(points, fill=(255, 210, 185, 7), width=1)
            for x in range(0, w, 34 * SCALE):
                od.line([(x, 0), (x, h)], fill=(255, 255, 255, 3), width=1)
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
            noise = Image.effect_noise((w, h), 7).convert("L")
            noise_rgb = Image.merge("RGB", (noise, noise, noise))
            img = Image.blend(img, noise_rgb, 0.012)
        return img

    def _rrect(self, d, x1, y1, x2, y2, radius, fill=None, outline=None, width=1):
        d.rounded_rectangle(
            [int(x1 * SCALE), int(y1 * SCALE), int(x2 * SCALE), int(y2 * SCALE)],
            radius=max(1, int(radius * SCALE)),
            fill=fill,
            outline=outline,
            width=max(1, int(width * SCALE)),
        )

    def _line(self, d, points, fill, width=1):
        d.line(
            [(int(x * SCALE), int(y * SCALE)) for x, y in points],
            fill=fill,
            width=max(1, int(width * SCALE)),
        )

    def _display_size(self, size):
        if self.compact:
            return size
        return min(13.5, max(10, size * 1.28)) if size < 12 else size

    def _text(self, d, x, y, value, size=11, color=TEXT, bold=False,
              numeric=False, anchor="lm"):
        size = self._display_size(size)
        d.text(
            (int(x * SCALE), int(y * SCALE)),
            str(value),
            font=_font(size * SCALE, bold=bold, numeric=numeric),
            fill=color,
            anchor=anchor,
        )

    def _fit_text(self, value, max_width, size=11, bold=False, numeric=False):
        value = str(value)
        size = self._display_size(size)
        font = _font(size * SCALE, bold=bold, numeric=numeric)
        if font.getlength(value) <= max_width * SCALE:
            return value
        suffix = "…"
        while value and font.getlength(value + suffix) > max_width * SCALE:
            value = value[:-1]
        return value + suffix

    def _tracked_text(self, d, x, y, value, size=8, color=MUTED, gap=1.2):
        size = self._display_size(size)
        cursor = x
        font = _font(size * SCALE, bold=True)
        for char in value:
            d.text((int(cursor * SCALE), int(y * SCALE)), char, font=font, fill=color, anchor="lm")
            cursor += font.getlength(char) / SCALE + gap

    def _hit(self, x1, y1, x2, y2, name):
        self._hits.append((x1, y1, x2, y2, name))

    def _icon_button(self, d, x, y, kind, name, active=False):
        color = TEXT if active else MUTED
        self._rrect(d, x - 14, y - 14, x + 14, y + 14, 8,
                    fill=PANEL if active else None)
        if kind == "refresh":
            d.arc(
                [int((x - 6) * SCALE), int((y - 6) * SCALE),
                 int((x + 6) * SCALE), int((y + 6) * SCALE)],
                start=35,
                end=315,
                fill=color,
                width=int(1.6 * SCALE),
            )
            d.polygon(
                [(int((x + 4.4) * SCALE), int((y - 7.4) * SCALE)),
                 (int((x + 8.0) * SCALE), int((y - 6.1) * SCALE)),
                 (int((x + 6.0) * SCALE), int((y - 2.8) * SCALE))],
                fill=color,
            )
        elif kind == "collapse":
            self._line(d, [(x - 6, y - 4), (x, y + 2), (x + 6, y - 4)], color, 1.6)
        elif kind == "spark":
            self._line(d, [(x - 7, y + 3), (x - 3, y - 2), (x + 1, y + 1),
                           (x + 5, y - 5), (x + 8, y - 1)], color, 1.5)
        self._hit(x - 15, y - 15, x + 15, y + 15, name)

    def _draw_header(self, d, w):
        d.ellipse(
            [int(17 * SCALE), int(19 * SCALE), int(41 * SCALE), int(43 * SCALE)],
            fill=CORAL,
        )
        d.ellipse(
            [int(24 * SCALE), int(26 * SCALE), int(34 * SCALE), int(36 * SCALE)],
            fill=INK,
        )
        d.ellipse(
            [int(27.5 * SCALE), int(29.5 * SCALE), int(30.5 * SCALE), int(32.5 * SCALE)],
            fill=MINT,
        )
        self._tracked_text(d, 50, 27, "TOKEN LENS", 9, TEXT, 1.0)
        self._tracked_text(d, 50, 39, "SPEND MONITOR", 6.2, DIM, 0.8)
        self._icon_button(d, w - 60, 31, "refresh", "refresh", active=self.busy)
        self._icon_button(d, w - 26, 31, "collapse", "collapse")
        if self._refresh_status and not self.compact:
            self._text(d, w - 60, 50, self._refresh_status, 6, BAD,
                       bold=True, anchor="mm")
        self._line(d, [(17, 56), (w - 17, 56)], BORDER, 0.7)

    def _draw_nav(self, d, w, h):
        y0 = h - 54
        self._line(d, [(17, y0), (w - 17, y0)], BORDER, 0.7)
        tabs = (
            ("overview", "总览", 58),
            ("conversations", "对话", 58 + (w - 116) / 3.0),
            ("usage", "用量", 58 + (w - 116) * 2 / 3.0),
            ("settings", "设置", w - 58),
        )
        for name, label, x in tabs:
            active = self.view == name
            color = CORAL if active else DIM
            cy = y0 + 19
            if name == "overview":
                for dx, dy in ((-5, -5), (2, -5), (-5, 2), (2, 2)):
                    self._rrect(d, x + dx, cy + dy - 1, x + dx + 5, cy + dy + 4, 1,
                                fill=color)
            elif name == "usage":
                for idx, height in enumerate((4, 9, 6)):
                    xx = x - 7 + idx * 5
                    self._line(d, [(xx, cy + 4), (xx, cy + 4 - height)], color, 1.5)
            elif name == "conversations":
                self._rrect(d, x - 8, cy - 7, x + 7, cy + 5, 3,
                            outline=color, width=1.1)
                self._line(d, [(x - 2, cy + 5), (x - 5, cy + 8)], color, 1.1)
            else:
                for idx, yy in enumerate((-5, 0, 5)):
                    self._line(d, [(x - 7, cy + yy), (x + 7, cy + yy)], color, 1.2)
                    self._rrect(d, x - 1 + idx * 4 - 4, cy + yy - 1.5,
                                x + 1 + idx * 4 - 4, cy + yy + 1.5, 1.5, fill=color)
            self._text(d, x, y0 + 39, label, 8, color, bold=active, anchor="mm")
            self._hit(x - 30, y0, x + 30, h, "tab:" + name)

    def _balance(self):
        data = self.snapshot or {}
        relay = data.get("relay") or {}
        coupons = data.get("coupons") or []
        aliyun = data.get("aliyun") or {}
        if relay and relay.get("remaining") is not None:
            currency = relay.get("currency") or "USD"
            symbol = "¥" if currency == "CNY" else "$"
            total = _num(relay.get("remaining"))
            granted = _num(relay.get("granted"), 0)
            return {
                "symbol": symbol,
                "total": total,
                "granted": granted,
                "label": "API 额度",
                "detail": relay.get("provider") or "中转站",
                "expiry": "",
                "ratio": (total / granted) if granted else None,
            }
        if coupons:
            total = sum(_num(item.get("balance")) for item in coupons)
            nominal = sum(_num(item.get("amount")) for item in coupons)
            expiries = [item.get("expiry") for item in coupons if item.get("expiry")]
            expiry = min(expiries) if expiries else ""
            student = any("学生" in (item.get("name") or "") for item in coupons)
            return {
                "symbol": "¥",
                "total": total,
                "granted": nominal,
                "label": "可用余额",
                "detail": ("学生券" if student else "代金券") + (
                    " · %d 张" % len(coupons) if len(coupons) > 1 else ""
                ),
                "expiry": expiry,
                "ratio": (total / nominal) if nominal else None,
            }
        if aliyun:
            total = _num(aliyun.get("available"), _num(aliyun.get("cash")))
            return {
                "symbol": "¥",
                "total": total,
                "granted": 0,
                "label": "账户余额",
                "detail": "阿里云",
                "expiry": "",
                "ratio": None,
            }
        return {
            "symbol": "¥",
            "total": None,
            "granted": 0,
            "label": "可用余额",
            "detail": "尚未连接",
            "expiry": "",
            "ratio": None,
        }

    def _usage_parts(self):
        usage = (self.snapshot or {}).get("usage") or {}
        current = usage.get("current") or {}
        today = usage.get("today") or {}
        week = usage.get("week") or {}
        model = current.get("model") or ""
        if not model:
            for item in usage.get("recent_turns") or []:
                model = item.get("model") or ""
                if model:
                    break
        pricing = usage.get("pricing") or {}
        multiplier = _num(pricing.get("multiplier"), 1.0)
        return current, today, week, model, multiplier

    def _status_color(self):
        if self.last_error:
            return BAD
        if self.fetching:
            return GOLD
        return MINT

    def _compact_status_color(self):
        if self.last_error:
            return "#C96B70"
        if self.fetching:
            return "#A88959"
        return "#89918F"

    def _draw_overview(self, d, w, h):
        bal = self._balance()
        current, today, week, model, multiplier = self._usage_parts()
        current_tokens = (current.get("tokens") or {})
        today_tokens = (today.get("tokens") or {})
        x = 18

        self._text(d, x, 79, bal["label"], 10, MUTED)
        status_color = self._status_color()
        d.ellipse(
            [int((w - 71) * SCALE), int(75 * SCALE), int((w - 65) * SCALE), int(81 * SCALE)],
            fill=status_color,
        )
        self._text(d, w - 58, 79, "LIVE", 7, status_color, bold=True)

        amount = "%s%s" % (bal["symbol"], _money(bal["total"]))
        self._text(d, x, 109, amount, 34, TEXT, bold=True, numeric=True)
        self._text(d, x, 139, bal["detail"], 9, MUTED)
        expiry_days = _days_left(bal["expiry"]) if bal.get("expiry") else None
        expiry_text = ("剩 %d 天" % expiry_days) if expiry_days is not None and expiry_days >= 0 else ""
        if expiry_text:
            self._text(d, w - 18, 139, expiry_text, 8, DIM, anchor="rm")
            self._line(d, [(w - 76, 139), (w - 22, 139)], BORDER, 0.6)

        ratio = bal.get("ratio")
        self._rrect(d, x, 158, w - 18, 161, 1.5, fill=PANEL)
        if ratio is not None:
            fill_w = max(4, (w - 36) * max(0.0, min(1.0, ratio)))
            self._rrect(d, x, 158, x + fill_w, 161, 1.5, fill=CORAL)
            self._text(d, x, 171, "%d%% 可用" % round(max(0.0, min(1.0, ratio)) * 100),
                       8, DIM)
        else:
            self._text(d, x, 171, "余额变化将在这里显示", 8, DIM)
        history = (self.snapshot or {}).get("balance_history") or {}
        observed = _num(history.get("day_delta"), 0.0)
        observation = ("观测 -¥%.2f" % abs(observed)) if observed < -0.004 else ""
        pricing = ((self.snapshot or {}).get("usage") or {}).get("pricing") or {}
        group = pricing.get("profile_name") or ""
        right_hint = (group + " · " if group else "") + "倍率 %g×" % multiplier
        if pricing.get("profile_ambiguous"):
            right_hint = "请选择计价分组"
        elif observation and not group:
            right_hint = observation + " · " + right_hint
        self._text(d, w - 18, 171, self._fit_text(right_hint, w - 120, 8),
                   8, DIM, anchor="rm")

        self._line(d, [(18, 192), (w - 18, 192)], BORDER, 0.7)
        self._line(d, [(w / 2.0, 204), (w / 2.0, 250)], BORDER, 0.7)
        left_x = 20
        right_x = w / 2.0 + 14
        self._text(d, left_x, 207, "本对话 · 估", 8, MUTED)
        current_cost = ("¥" + _money(current.get("cost"), 3)
                        if current.get("cost_known", False) else "未设价")
        self._text(d, left_x, 228, current_cost, 17 if current.get("cost_known", False) else 14,
                   TEXT, bold=True, numeric=current.get("cost_known", False))
        self._text(d, left_x, 247, "%s tokens · %s 轮" % (
            _short_tokens(current_tokens.get("total_tokens")), current.get("turns") or 0
        ), 7.5, DIM)
        self._text(d, right_x, 207, "今日 · 估", 8, MUTED)
        today_prefix = "" if today.get("cost_known", True) else "≥"
        self._text(d, right_x, 228, today_prefix + "¥" + _money(today.get("cost"), 2),
                   19, TEXT, bold=True, numeric=True)
        self._text(d, right_x, 247, "%s tokens · %s 轮" % (
            _short_tokens(today_tokens.get("total_tokens")), today.get("turns") or 0
        ), 7.5, DIM)

        self._text(d, 18, 278, "近 7 天", 10, TEXT, bold=True)
        week_prefix = "" if week.get("cost_known", True) else "≥"
        self._text(d, w - 18, 278, "%s tokens  ·  %s¥%s" % (
            _short_tokens((week.get("tokens") or {}).get("total_tokens")),
            week_prefix,
            _money(week.get("cost"), 2),
        ), 7.5, MUTED, anchor="rm")
        series = ((self.snapshot or {}).get("usage") or {}).get("series") or []
        self._draw_chart(d, 18, 296, w - 36, 94, series)

        self._line(d, [(18, 410), (w - 18, 410)], BORDER, 0.7)
        pricing = ((self.snapshot or {}).get("usage") or {}).get("pricing") or {}
        provider_label = (pricing.get("catalog") or {}).get("label") or ""
        self._text(d, 18, 427,
                   self._fit_text("当前模型" + (" · " + provider_label if provider_label else ""),
                                  200, 8),
                   8, MUTED)
        model_name = server.model_display_name(model) if model else "等待首轮对话"
        self._text(d, 18, 447, self._fit_text(model_name, 235, 13, bold=True),
                   13, TEXT, bold=True)
        configured = (pricing.get("models") or {}).get(model)
        official = pricing.get("current_entry") if pricing.get("provider") else None
        if pricing.get("current_source") == "idle":
            price_label, price_color = "闲时价", GOLD
        elif configured:
            price_label, price_color = "标准价", MINT
        elif pricing.get("provider") == "custom":
            price_label, price_color = "自定义", GOLD
        elif official:
            price_label, price_color = "官方价", MINT
        elif model in server.REFERENCE_PRICES:
            price_label, price_color = "参考价", MUTED
        else:
            price_label, price_color = "未设价", MUTED
        chip_w = 62
        self._rrect(d, w - 18 - chip_w, 433, w - 18, 451, 7,
                    fill=PANEL_2, outline=BORDER, width=0.6)
        self._text(d, w - 18 - chip_w / 2.0, 442, price_label, 7.5,
                   price_color, anchor="mm")
        transition = pricing.get("idle_transition") or {}
        if transition:
            to_idle = transition.get("to") == "idle"
            hint = "距%s %s" % (
                "闲时" if to_idle else "忙时",
                _duration_short(transition.get("seconds")),
            )
            self._text(d, w - 18, 426, self._fit_text(hint, 140, 8.5),
                       8.5, GOLD if to_idle else DIM, anchor="rm")

        parts = (
            ("输入", current_tokens.get("input_tokens")),
            ("缓存", current_tokens.get("cached_input_tokens")),
            ("输出", current_tokens.get("output_tokens")),
        )
        for idx, (label, value) in enumerate(parts):
            px = 18 + idx * 119
            self._text(d, px, 470, label, 7.5, DIM)
            self._text(d, px + 29, 470, _short_tokens(value), 8.5, MUTED, numeric=True)

        self._line(d, [(18, 489), (w - 18, 489)], BORDER, 0.7)
        self._text(d, 18, 505, "最近轮次", 9, TEXT, bold=True)
        self._text(d, w - 18, 505, "费用均为估算", 7, DIM, anchor="rm")
        rows = ((self.snapshot or {}).get("usage") or {}).get("recent_turns") or []
        for idx, item in enumerate(rows[:2]):
            y = 528 + idx * 28
            at = item.get("at") or ""
            try:
                time_text = datetime.fromisoformat(at).strftime("%H:%M")
            except ValueError:
                time_text = "--:--"
            self._text(d, 18, y, time_text, 8, MUTED, numeric=True)
            label = _short_model(item.get("model"))
            if item.get("price_source") == "idle":
                label += " · 闲"
            self._text(d, 55, y, self._fit_text(label, 145, 8.5), 8.5, TEXT)
            usage = item.get("usage") or {}
            self._text(d, 250, y, _short_tokens(usage.get("total_tokens")) + " tok",
                       7.5, DIM, anchor="rm")
            cost_text = ("¥" + _money(item.get("cost"), 3)
                         if item.get("cost") is not None else "未设价")
            self._text(d, w - 18, y, cost_text, 8.5,
                       CORAL if idx == 0 else MUTED,
                       numeric=item.get("cost") is not None, anchor="rm")

    def _draw_chart(self, d, x, y, width, height, series):
        values = [max(0, int(_num(item.get("total_tokens"), 0))) for item in series[-7:]]
        while len(values) < 7:
            values.insert(0, 0)
        labels = (series[-7:] if series else [])
        maximum = max(values) if values else 0
        scaled = [math.sqrt(value) for value in values]
        scaled_max = max(scaled) if scaled else 0.0
        for grid in (0.0, 0.5, 1.0):
            yy = y + height - 17 - grid * (height - 25)
            self._line(d, [(x, yy), (x + width, yy)], "#222A31", 0.5)
        bar_w = 27
        step = width / 7.0
        for idx, value in enumerate(values):
            cx = x + step * idx + step / 2.0
            bx1 = cx - bar_w / 2.0
            bx2 = cx + bar_w / 2.0
            track_top = y + 4
            track_bottom = y + height - 16
            self._rrect(d, bx1, track_top, bx2, track_bottom, 4, fill="#1B2127")
            fraction = (scaled[idx] / scaled_max) if scaled_max else 0.0
            bar_h = max(4, fraction * (track_bottom - track_top))
            color = CORAL if idx == 6 else ("#64717B" if idx % 2 else "#4A565F")
            self._rrect(d, bx1, track_bottom - bar_h, bx2, track_bottom, 4, fill=color)
            if idx == 6 and value:
                self._text(d, cx, track_bottom - bar_h - 7, _short_tokens(value),
                           6.8, CORAL, numeric=True, anchor="mm")
            label = "今" if idx == 6 else ((labels[idx].get("weekday") if idx < len(labels) else "")
                                           if labels else "")
            self._text(d, cx, y + height - 4, label, 7,
                       CORAL if idx == 6 else DIM, bold=idx == 6, anchor="mm")

    def _draw_usage(self, d, w, h):
        current, today, week, model, multiplier = self._usage_parts()
        today_usage = today.get("tokens") or {}
        week_usage = week.get("tokens") or {}
        self._text(d, 18, 77, "用量", 17, TEXT, bold=True)
        self._text(d, 62, 79, "近 7 天 · %s 轮" % ((self.snapshot or {}).get("usage", {}).get("week", {}).get("turns") or 0),
                   8, DIM)
        self._text(d, 18, 108, "今日估算", 8, MUTED)
        today_prefix = "" if today.get("cost_known", True) else "≥"
        self._text(d, 18, 132, today_prefix + "¥" + _money(today.get("cost"), 3), 25, TEXT,
                   bold=True, numeric=True)
        self._text(d, 18, 155, "%s tokens · %s 轮" % (
            _short_tokens(today_usage.get("total_tokens")), today.get("turns") or 0
        ), 8, MUTED)
        self._line(d, [(w / 2.0, 101), (w / 2.0, 158)], BORDER, 0.7)
        self._text(d, w / 2.0 + 16, 108, "本周估算", 8, MUTED)
        week_prefix = "" if week.get("cost_known", True) else "≥"
        self._text(d, w / 2.0 + 16, 132, week_prefix + "¥" + _money(week.get("cost"), 3),
                   25, TEXT, bold=True, numeric=True)
        self._text(d, w / 2.0 + 16, 155, "%s tokens · %s 轮" % (
            _short_tokens(week_usage.get("total_tokens")), week.get("turns") or 0
        ), 8, MUTED)
        self._line(d, [(18, 174), (w - 18, 174)], BORDER, 0.7)

        self._text(d, 18, 194, "每日消耗", 10, TEXT, bold=True)
        self._text(d, w - 18, 194, "柱高按 token 开方缩放", 7, DIM, anchor="rm")
        series = (self.snapshot.get("usage") or {}).get("series") or []
        self._draw_chart(d, 18, 214, w - 36, 126, series)

        self._line(d, [(18, 356), (w - 18, 356)], BORDER, 0.7)
        self._text(d, 18, 374, "模型分布", 10, TEXT, bold=True)
        models = (self.snapshot.get("usage") or {}).get("models") or []
        total_tokens = max(1, sum(_num((item.get("tokens") or {}).get("total_tokens"))
                                  for item in models))
        colors = (CORAL, MINT, GOLD, BLUE)
        for idx, item in enumerate(models[:3]):
            y = 397 + idx * 33
            tokens = _num((item.get("tokens") or {}).get("total_tokens"))
            share = tokens / total_tokens
            label = self._fit_text(item.get("model_name") or item.get("model"), 205, 8.5)
            self._text(d, 18, y, label, 8.5, TEXT)
            cost_text = ("¥" + _money(item.get("cost"), 3)
                         if item.get("cost_known", True) else "未设价")
            self._text(d, w - 18, y, "%d%%  ·  %s" % (
                round(share * 100), cost_text
            ), 7.5, MUTED, anchor="rm")
            self._rrect(d, 18, y + 9, w - 18, y + 12, 1.5, fill=PANEL)
            self._rrect(d, 18, y + 9, 18 + (w - 36) * share, y + 12, 1.5,
                        fill=colors[idx % len(colors)])

        self._line(d, [(18, 506), (w - 18, 506)], BORDER, 0.7)
        self._text(d, 18, 524, "最近轮次", 10, TEXT, bold=True)
        self._text(d, w - 18, 524, "估费 × %.2f" % multiplier, 7.5, DIM, anchor="rm")
        rows = (self.snapshot.get("usage") or {}).get("recent_turns") or []
        for idx, item in enumerate(rows[:2]):
            y = 548 + idx * 34
            at = item.get("at") or ""
            try:
                time_text = datetime.fromisoformat(at).strftime("%m/%d %H:%M")
            except ValueError:
                time_text = "--"
            title = self._fit_text(item.get("title") or "未命名对话", 270, 8)
            self._text(d, 18, y, title, 8, TEXT)
            cost_text = ("¥" + _money(item.get("cost"), 3)
                         if item.get("cost") is not None else "未设价")
            self._text(d, w - 18, y, cost_text, 8, CORAL if item.get("cost") is not None else MUTED,
                       numeric=item.get("cost") is not None, anchor="rm")
            meta = "%s  ·  %s  ·  %s tok" % (
                time_text,
                _short_model(item.get("model")),
                _short_tokens((item.get("usage") or {}).get("total_tokens")),
            )
            if item.get("price_source") == "idle":
                meta += "  ·  闲时"
            if item.get("profile_name"):
                meta += "  ·  %s ×%g" % (item["profile_name"], item.get("multiplier", 1))
            self._text(d, 18, y + 14, self._fit_text(meta, w - 36, 7.5), 7.5, MUTED)

    def _draw_conversations(self, d, w, h):
        usage = (self.snapshot or {}).get("usage") or {}
        threads = usage.get("threads") or []
        current = usage.get("current") or {}
        pricing = usage.get("pricing") or {}
        multiplier = _num(pricing.get("multiplier"), 1.0)
        self._text(d, 18, 77, "对话账本", 17, TEXT, bold=True)
        self._text(d, 94, 79, "最近 45 天 · %d 段" % len(threads), 8, DIM)
        self._text(d, 18, 106, "历史计价快照 · 逐轮估费",
                   7.2, MUTED)
        self._text(d, w - 18, 106, "× %.2f" % multiplier, 8, CORAL,
                   bold=True, numeric=True, anchor="rm")
        self._line(d, [(18, 120), (w - 18, 120)], BORDER, 0.7)

        if not threads:
            self._text(d, w / 2.0, 300, "还没有可读取的对话记录", 9, MUTED, anchor="mm")
            return
        for idx, item in enumerate(threads[:7]):
            y = 135 + idx * 63
            is_current = item.get("id") and item.get("id") == current.get("id")
            if is_current:
                self._rrect(d, 12, y - 12, w - 12, y + 46, 7,
                            fill="#1B2025", outline="#332824", width=0.7)
                self._rrect(d, 12, y - 12, 15, y + 46, 1.5, fill=CORAL)
            d.ellipse(
                [int(20 * SCALE), int((y + 3) * SCALE),
                 int(26 * SCALE), int((y + 9) * SCALE)],
                fill=CORAL if is_current else "#4B555D",
            )
            title = self._fit_text(item.get("title") or "未命名对话", 270, 9, bold=True)
            self._text(d, 34, y + 2, title, 9, TEXT if is_current else "#D7D8D4", bold=True)
            token_count = (item.get("tokens") or {}).get("total_tokens") or 0
            model = _short_model(item.get("model"))
            meta = "%s  ·  %s tokens  ·  %s 轮" % (
                self._fit_text(model, 135, 7.2),
                _short_tokens(token_count),
                item.get("turns") or 0,
            )
            self._text(d, 34, y + 19, meta, 7.2, MUTED)
            try:
                stamp = datetime.fromisoformat(item.get("updated_at") or "")
                date_text = stamp.strftime("%m/%d %H:%M")
            except ValueError:
                date_text = "--"
            self._text(d, 34, y + 33, date_text, 6.8, DIM, numeric=True)
            if item.get("cost_known", True):
                cost_text = "¥" + _money(item.get("cost"), 3)
                cost_color = CORAL if is_current else TEXT
            else:
                cost_text = "未设价"
                cost_color = MUTED
            self._text(d, w - 22, y + 5, cost_text, 12, cost_color,
                       bold=is_current, numeric=item.get("cost_known", True), anchor="rm")
            self._text(d, w - 22, y + 24, _short_tokens(token_count) + " tok",
                       7.2, MUTED, numeric=True, anchor="rm")

    def _entry(self, key, x, y, width, value, secret=False):
        entry = tk.Entry(
            self.root,
            bg=PANEL_2,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=CORAL,
            font=(FONT_UI, 10),
            show="●" if secret else "",
        )
        entry.insert(0, "" if value is None else str(value))
        entry.place(x=x, y=y, width=width, height=27)
        self._entries[key] = entry
        return entry

    def _clear_entries(self):
        for entry in self._entries.values():
            try:
                entry.destroy()
            except tk.TclError:
                pass
        self._entries = {}

    def _load_idle_editor(self, idle):
        windows = idle.get("windows")
        self.idle_windows = []
        if isinstance(windows, list):
            for window in windows:
                if not isinstance(window, dict):
                    continue
                start, end = window.get("start"), window.get("end")
                if server._clock_minutes(start) is not None \
                        and server._clock_minutes(end) is not None:
                    self.idle_windows.append([str(start), str(end)])
        elif idle.get("start") and idle.get("end"):
            self.idle_windows.append([str(idle["start"]), str(idle["end"])])
        self.idle_weekend = bool(idle.get("weekend_all_day"))
        self.idle_days = bool(idle.get("days"))

    def _idle_summary(self):
        parts = []
        compact = len(self.idle_windows) > 1
        for start, end in self.idle_windows:
            end = "24:00" if end == "00:00" else end
            if compact:
                start = start[:2] if start.endswith(":00") else start
                end = end[:2] if end.endswith(":00") else end
            parts.append("%s-%s" % (start, end))
        if self.idle_weekend:
            parts.append("周末")
        if getattr(self, "idle_days", False):
            parts.append("节假日")
        return " · ".join(parts) or "尚未设置时段"

    def _settings_values(self):
        cfg = read_config()
        relay = dict(cfg.get("relay") or {})
        aliyun = dict(cfg.get("aliyun") or {})
        volcengine = dict(cfg.get("volcengine") or {})
        pricing = server._price_config(cfg)
        provider_key = pricing.get("provider") or ""
        credentials = (
            aliyun if provider_key == "dashscope"
            else volcengine if provider_key == "ark"
            else {}
        )
        _, _, _, model, _ = self._usage_parts()
        configured = (pricing.get("models") or {}).get(model) or {}
        official = (pricing.get("official") or {}).get(model) or {}
        entry = official or configured
        idle = entry.get("idle") or {}
        official_idle = official.get("idle") or {}
        profile = pricing.get("profile") or {}
        idle_key = (model, provider_key, pricing.get("profile_key"), bool(official_idle),
                    (pricing.get("catalog") or {}).get("fetched_at"))
        if self._idle_enabled_loaded_for != idle_key:
            self.idle_enabled = True if official_idle else bool(idle.get("enabled", False))
            self._load_idle_editor(idle)
            self._idle_enabled_loaded_for = idle_key
        reference = server.REFERENCE_PRICES.get(model) or {}
        effective = server.relay_config()
        base = effective.get("base_url") or ""
        key = effective.get("api_key") or ""
        values = {
            "relay_base": base,
            "relay_key": key,
            "access_id": credentials.get("access_key_id") or "",
            "access_secret": credentials.get("access_key_secret") or "",
            "credential_provider": provider_key if credentials or provider_key in ("dashscope", "ark") else "",
            "multiplier": pricing.get("multiplier", 1.0),
            "price_input": entry.get("input", reference.get("input", "")),
            "price_cached": entry.get("cached", reference.get("cached", "")),
            "price_output": entry.get("output", reference.get("output", "")),
            "idle_input": idle.get("input", ""),
            "idle_cached": idle.get("cached", ""),
            "idle_output": idle.get("output", ""),
            "provider": pricing.get("catalog") or {},
            "provider_key": provider_key,
            "official": bool(official),
            "official_idle": bool(official_idle),
            "profile_key": pricing.get("profile_key") or "",
            "profile_ambiguous": pricing.get("profile_ambiguous", False),
            "profile_name": profile.get("name") or (
                "请选择分组" if pricing.get("profile_ambiguous") else server._url_host(base)
            ) or "未命名 API",
            "refresh_sec": int(self.refresh_sec),
            "balance_refresh_sec": int(self.balance_refresh_sec),
        }
        return values, model

    def _sync_entries(self):
        if (self.compact or self.view != "settings" or self.idle_editor_open
                or self._provider_menu or getattr(self, "_model_prices", None)):
            return
        values, model = self._settings_values()
        key = (model, values.get("provider_key"), values.get("profile_key"),
               values.get("profile_name"), values.get("relay_base"),
               server._pricing_key_fingerprint(values.get("relay_base"), values.get("relay_key")),
               (values.get("provider") or {}).get("fetched_at"))
        if self._entries and getattr(self, "_entries_key", None) == key:
            return
        self._clear_entries()
        self._entries_key = key
        if values.get("provider_key") == "custom":
            self._entry("profile_name", 145, 67, WIDTH - 251, values.get("profile_name", ""))
        credential_provider = values.get("credential_provider")
        show_credentials = credential_provider in ("dashscope", "ark")
        dy = 0 if show_credentials else (-22 if values.get("provider_key") == "custom" else -42)
        self._entry("relay_base", 18, 111, WIDTH - 36, values["relay_base"])
        self._entry("relay_key", 18, 161, WIDTH - 36, values["relay_key"], secret=True)
        if show_credentials:
            self._entry("access_id", 18, 211, 168, values["access_id"])
            self._entry("access_secret", 204, 211, WIDTH - 222,
                        values["access_secret"], secret=True)
        for idx, key in enumerate(("multiplier", "price_input", "price_cached", "price_output")):
            self._entry(key, 18 + idx * 88, 285 + dy, 78, values[key])
        for idx, key in enumerate(("idle_input", "idle_cached", "idle_output")):
            self._entry(key, 18 + idx * 119, 406 + dy, 100, values[key])
        self._entry("refresh_sec", 18, 479 + dy, 168, values["refresh_sec"])
        self._entry("balance_refresh_sec", 204, 479 + dy, WIDTH - 222,
                    values["balance_refresh_sec"])

    def _draw_settings(self, d, w, h):
        values, model = self._settings_values()
        catalog = values.get("provider") or {}
        credential_provider = values.get("credential_provider")
        show_credentials = credential_provider in ("dashscope", "ark")
        dy = 0 if show_credentials else (-22 if values.get("provider_key") == "custom" else -42)
        self._text(d, 18, 77, "设置", 17, TEXT, bold=True)
        is_custom = values.get("provider_key") == "custom"
        if is_custom:
            self._text(d, 62, 79, "分组名称", 7.5, DIM)
        else:
            self._text(d, 62, 79, self._fit_text(server.model_display_name(model), 250, 8),
                       8, DIM)
        self._rrect(d, w - 88, 65, w - 18, 91, 8, fill=PANEL_2,
                    outline=BORDER, width=0.6)
        self._text(d, w - 53, 78, "+ 新建", 7.5, CORAL, bold=True, anchor="mm")
        self._hit(w - 88, 65, w - 18, 91, "profile_new")
        self._text(d, 18, 93, "数据源", 8, CORAL, bold=True)
        self._text(d, 18, 105, "API 地址", 7.5, DIM)
        self._text(d, 18, 155, "API Key", 7.5, DIM)
        if show_credentials:
            label = "阿里云" if credential_provider == "dashscope" else "火山"
            self._text(d, 18, 205, "%s AK（查余额用）" % label, 7.5, DIM)
            self._text(d, 204, 205, "%s SK（查余额用）" % label, 7.5, DIM)
        if is_custom:
            self._text(d, 18, 204, self._fit_text("当前模型 · " + (model or "尚未识别"),
                                               w - 36, 9), 9, TEXT)

        self._text(d, 18, 248 + dy, "计费", 8, CORAL, bold=True)
        if is_custom and values.get("profile_key"):
            self._rrect(d, w - 208, 240 + dy, w - 120, 258 + dy, 8,
                        fill=PANEL_2, outline=BORDER, width=0.6)
            self._text(d, w - 164, 249 + dy, "模型价格", 7.5, TEXT,
                       bold=True, anchor="mm")
            self._hit(w - 208, 240 + dy, w - 120, 258 + dy, "model_prices")
        for x1, x2, label, color, name in (
            (w - 112, w - 62, "切换", MUTED, "price_provider"),
            (w - 56, w - 8, "同步", CORAL, "price_sync"),
        ):
            self._rrect(d, x1, 240 + dy, x2, 258 + dy, 9,
                        fill=PANEL_2, outline=BORDER, width=0.6)
            self._text(d, (x1 + x2) / 2, 249 + dy, label, 7.5, color,
                       bold=True, anchor="mm")
            self._hit(x1, 240 + dy, x2, 258 + dy, name)
        self._text(d, 18, 260 + dy, "单价 ¥ / 百万 token · 分组倍率", 7, DIM)
        for idx, label in enumerate(("倍率", "输入", "缓存", "输出")):
            self._text(d, 20 + idx * 88, 275 + dy, label, 7, DIM)
        stamp = ""
        try:
            stamp = datetime.fromisoformat(catalog.get("fetched_at") or "").strftime("%m-%d %H:%M")
        except ValueError:
            pass
        if values.get("provider_key") == "custom":
            source_text = "%s · 自定义价" % values.get("profile_name")
            source_color = GOLD
        elif catalog.get("label"):
            source_text = "%s 官方价%s" % (catalog["label"], (" · " + stamp) if stamp else "")
            source_color = MINT
        else:
            source_text, source_color = "未识别平台 · 点此选择", CORAL
        self._text(d, 18, 322 + dy, self._fit_text(source_text, w - 36, 7.5),
                   7.5, source_color)
        self._hit(18, 310 + dy, w - 18, 332 + dy, "price_provider")

        self._text(d, 18, 338 + dy, "闲时价", 8, CORAL, bold=True)
        if values.get("provider_key") == "custom":
            idle_note = "自定义时段"
        elif values.get("official_idle"):
            idle_note = "官网有闲时价"
        elif values.get("official"):
            idle_note = "官网无闲时价"
        elif self.idle_windows:
            idle_note = "手动时段"
        else:
            idle_note = ""
        if idle_note:
            self._text(d, 62, 338 + dy, self._fit_text(idle_note, 228, 7),
                       7, GOLD if values.get("official_idle") else DIM)
        self._draw_mini_switch(d, 300, 338 + dy, "启用", self.idle_enabled, "idle_toggle")
        self._rrect(d, 18, 351 + dy, w - 18, 383 + dy, 7,
                    fill=PANEL_2, outline=BORDER, width=0.6)
        idle_summary = self._idle_summary()
        if idle_summary == "尚未设置时段" and values.get("official") \
                and not values.get("official_idle"):
            idle_summary = "官网无闲时价 · 可手动设置"
        self._text(d, 30, 367 + dy, self._fit_text(idle_summary, w - 104, 8),
                   8, MUTED)
        self._text(d, w - 30, 367 + dy, "编辑", 8, CORAL, bold=True, anchor="rm")
        self._hit(18, 351 + dy, w - 18, 383 + dy, "idle_edit")
        for x, label in ((20, "输入"), (139, "缓存"), (258, "输出")):
            self._text(d, x, 397 + dy, label, 7, DIM)
        self._text(d, 18, 444 + dy, "点上方时段编辑，可添加多段并设置周末全天", 7, DIM)

        self._text(d, 18, 459 + dy, "刷新与交互", 8, CORAL, bold=True)
        self._text(d, 18, 471 + dy, "用量刷新 (秒)", 7, DIM)
        self._text(d, 204, 471 + dy, "余额刷新 (秒)", 7, DIM)

        self._draw_switch_row(d, 18, 519 + dy, "跟随 ChatGPT 窗口", self.follow, "follow")
        self._draw_switch_row(d, 204, 519 + dy, "背景纹理", self.texture, "texture")

        self._rrect(d, 18, 546 + dy, 126, 580 + dy, 7,
                    fill=PANEL_2, outline=BORDER, width=0.6)
        self._text(d, 72, 563 + dy, "测试连接", 8.5, TEXT, bold=True, anchor="mm")
        self._hit(18, 546 + dy, 126, 580 + dy, "test")
        self._rrect(d, 136, 546 + dy, 244, 580 + dy, 7, fill=CORAL)
        self._text(d, 190, 563 + dy, "保存设置", 8.5, "#150D0B", bold=True, anchor="mm")
        self._hit(136, 546 + dy, 244, 580 + dy, "save")
        if self._settings_status:
            self._text(d, 18, 594 + dy, self._fit_text(self._settings_status, w - 36, 7.5),
                       7.5, self._settings_status_color)

    def _draw_idle_editor(self, d, w, h):
        self._text(d, 18, 77, "闲时时段", 17, TEXT, bold=True)
        self._text(d, w - 18, 79, "返回设置", 8, CORAL, bold=True, anchor="rm")
        self._hit(w - 92, 64, w - 10, 94, "idle_back")
        self._text(d, 18, 99, "点开时间直接选择，可添加多段；结束 00:00 视为当天结束",
                   7, DIM)

        if not self.idle_windows:
            self._text(d, w / 2.0, 145, "还没有闲时时段", 9, MUTED, anchor="mm")
        for idx, (start, end) in enumerate(self.idle_windows[:6]):
            y = 120 + idx * 40
            self._rrect(d, 18, y, 108, y + 30, 7, fill=PANEL_2,
                        outline=BORDER, width=0.6)
            self._text(d, 63, y + 15, start, 10, TEXT, numeric=True, anchor="mm")
            self._hit(18, y, 108, y + 30, "idle_time:%d:start" % idx)
            self._text(d, 118, y + 15, "至", 7.5, DIM, anchor="mm")
            self._rrect(d, 128, y, 218, y + 30, 7, fill=PANEL_2,
                        outline=BORDER, width=0.6)
            end_label = "24:00" if end == "00:00" else end
            self._text(d, 173, y + 15, end_label, 10, TEXT, numeric=True, anchor="mm")
            self._hit(128, y, 218, y + 30, "idle_time:%d:end" % idx)
            self._rrect(d, w - 50, y, w - 18, y + 30, 7, fill=PANEL_2,
                        outline=BORDER, width=0.6)
            center_x, center_y = w - 34, y + 15
            self._line(d, [(center_x - 5, center_y - 5), (center_x + 5, center_y + 5)],
                       MUTED, 1.4)
            self._line(d, [(center_x + 5, center_y - 5), (center_x - 5, center_y + 5)],
                       MUTED, 1.4)
            self._hit(w - 52, y - 2, w - 16, y + 32, "idle_del:%d" % idx)

        add_y = 190 if not self.idle_windows else 120 + min(6, len(self.idle_windows)) * 40
        if len(self.idle_windows) < 6:
            self._rrect(d, 18, add_y, 136, add_y + 32, 7, fill="#201B19",
                        outline="#594039", width=0.7)
            icon_x, icon_y = 42, add_y + 16
            self._line(d, [(icon_x - 5, icon_y), (icon_x + 5, icon_y)], CORAL, 1.4)
            self._line(d, [(icon_x, icon_y - 5), (icon_x, icon_y + 5)], CORAL, 1.4)
            self._text(d, 106, add_y + 16, "添加时段", 8, CORAL, bold=True, anchor="mm")
            self._hit(18, add_y, 136, add_y + 32, "idle_add")

        weekend_y = add_y + 54
        self._draw_switch_row(d, 18, weekend_y, "周末全天闲时", self.idle_weekend,
                              "idle_weekend")
        self._text(d, 18, weekend_y + 28, "周六、周日全天都按闲时单价计算",
                   7, DIM)

    def _draw_time_picker(self, d, w, h):
        index, kind = self._time_pick
        self._rrect(d, 22, 88, w - 22, 462, 12, fill="#12171C",
                    outline="#46515A", width=1.0)
        title = "选择开始时间" if kind == "start" else "选择结束时间"
        self._text(d, 38, 112, title, 10, TEXT, bold=True)
        chosen = "%02d:%02d" % (self._time_hour, self._time_minute)
        self._text(d, w - 38, 112, chosen, 13, GOLD, bold=True, numeric=True, anchor="rm")
        self._text(d, 38, 142, "小时", 7.5, MUTED)
        for value in range(24):
            col, row = value % 6, value // 6
            x = 38 + col * 52
            y = 154 + row * 31
            selected = value == self._time_hour
            self._rrect(d, x, y, x + 44, y + 25, 6,
                        fill=CORAL if selected else PANEL_2,
                        outline=None if selected else BORDER, width=0.6)
            self._text(d, x + 22, y + 12.5, "%02d" % value, 8.5,
                       "#150D0B" if selected else TEXT, numeric=True, anchor="mm")
            self._hit(x, y, x + 44, y + 25, "tp_h:%d" % value)

        self._text(d, 38, 296, "分钟", 7.5, MUTED)
        for value in range(0, 60, 5):
            col, row = (value // 5) % 6, (value // 5) // 6
            x = 38 + col * 52
            y = 308 + row * 31
            selected = value == self._time_minute
            self._rrect(d, x, y, x + 44, y + 25, 6,
                        fill=CORAL if selected else PANEL_2,
                        outline=None if selected else BORDER, width=0.6)
            self._text(d, x + 22, y + 12.5, "%02d" % value, 8.5,
                       "#150D0B" if selected else TEXT, numeric=True, anchor="mm")
            self._hit(x, y, x + 44, y + 25, "tp_m:%d" % value)

        self._rrect(d, 38, 404, 166, 438, 7, fill=PANEL_2, outline=BORDER, width=0.6)
        self._text(d, 102, 421, "取消", 8.5, MUTED, anchor="mm")
        self._hit(38, 404, 166, 438, "tp_cancel")
        self._rrect(d, 224, 404, w - 38, 438, 7, fill=CORAL)
        self._text(d, (224 + w - 38) / 2.0, 421, "确定", 8.5, "#150D0B",
                   bold=True, anchor="mm")
        self._hit(224, 404, w - 38, 438, "tp_ok")

    def _draw_provider_menu(self, d, w, h):
        self._hit(0, 0, w, h, "provider_close")
        self._rrect(d, 22, 88, w - 22, 480, 12, fill="#12171C",
                    outline="#46515A", width=1.0)
        self._text(d, 38, 112, "计价平台", 10, TEXT, bold=True)
        self._text(d, 38, 132, "自动识别，或手动使用自定义价", 7, DIM)
        cfg = read_config()
        pricing = cfg.get("pricing") or {}
        current = str(pricing.get("provider") or "")
        current_profile = str(pricing.get("profile") or "")
        profiles = pricing.get("profiles") or {}
        options = [
            ("", "自动识别"),
            ("dashscope", "阿里云百炼"),
            ("deepseek", "DeepSeek"),
            ("ark", "火山方舟"),
        ]
        options.extend(
            ("custom:" + host, profile.get("name") or host)
            for host, profile in profiles.items()
            if isinstance(profile, dict)
        )
        options.append(("custom:new", "＋ 新建计价分组"))
        pages = max(1, math.ceil(len(options) / 6))
        self._provider_page = max(0, min(getattr(self, "_provider_page", 0), pages - 1))
        effective = server._price_config(cfg)
        current = effective.get("provider") or ""
        current_profile = effective.get("profile_key") or ""
        for idx, (key, label) in enumerate(options[self._provider_page * 6:(self._provider_page + 1) * 6]):
            y = 144 + idx * 42
            custom_profile = key.startswith("custom:") and key != "custom:new"
            active = key == current or (
                current == "custom"
                and key == "custom:" + current_profile
            )
            self._rrect(d, 34, y, w - 34, y + 36, 8,
                        fill="#241B18" if active else PANEL_2,
                        outline=CORAL if active else BORDER,
                        width=1.0 if active else 0.6)
            self._text(d, 48, y + (11 if custom_profile else 18),
                       self._fit_text(label, 220 if custom_profile else 210, 9),
                       9, TEXT if active else MUTED, bold=active)
            if custom_profile:
                profile_key = key.split(":", 1)[1]
                profile = profiles[profile_key]
                detail = "%s · ×%g" % (
                    server._url_host(profile.get("base_url") or profile_key),
                    _num(profile.get("multiplier"), 1),
                )
                if active:
                    detail = "使用中 · " + detail
                self._text(d, 48, y + 27, self._fit_text(detail, 220, 6.5),
                           6.5, CORAL if active else DIM)
            elif active:
                self._text(d, w - 48, y + 18, "使用中", 7, CORAL,
                           bold=True, anchor="rm")
            self._hit(34, y, w - 34, y + 36, "provider_pick:" + key)
            if custom_profile:
                copy_x = w - 80
                self._rrect(d, copy_x - 6, y + 11, copy_x + 3, y + 21,
                            1, outline=MUTED, width=0.8)
                self._rrect(d, copy_x - 2, y + 15, copy_x + 7, y + 25,
                            1, fill=PANEL_2, outline=MUTED, width=0.8)
                self._hit(copy_x - 14, y + 4, copy_x + 14, y + 32,
                          "profile_copy:" + profile_key)
                trash_x, trash_y = w - 48, y + 18
                self._rrect(d, trash_x - 13, trash_y - 13, trash_x + 13, trash_y + 13,
                            7, fill="#201B19", outline="#4A302C", width=0.6)
                self._line(d, [(trash_x - 4, trash_y - 4), (trash_x + 4, trash_y + 4)],
                           CORAL, 1.2)
                self._line(d, [(trash_x + 4, trash_y - 4), (trash_x - 4, trash_y + 4)],
                           CORAL, 1.2)
                self._hit(trash_x - 14, trash_y - 14, trash_x + 14, trash_y + 14,
                          "profile_delete:" + key.split(":", 1)[1])
        if pages > 1:
            for x, direction, target in ((118, -1, self._provider_page - 1),
                                         (w - 118, 1, self._provider_page + 1)):
                color = MUTED if 0 <= target < pages else BORDER
                self._line(d, [(x - direction * 3, 403), (x + direction * 3, 408),
                               (x - direction * 3, 413)], color, 1.2)
                if 0 <= target < pages:
                    self._hit(x - 16, 395, x + 16, 423, "provider_page:%d" % target)
            self._text(d, w / 2, 408, "%d / %d" % (self._provider_page + 1, pages),
                       7.5, MUTED, anchor="mm")
        self._rrect(d, 135, 434, w - 135, 466, 7, fill=PANEL_2,
                    outline=BORDER, width=0.6)
        self._text(d, w / 2.0, 450, "取消", 8.5, MUTED, anchor="mm")
        self._hit(135, 434, w - 135, 466, "provider_close")

    def _model_price_profile(self):
        state = getattr(self, "_model_prices", None) or {}
        profile = ((read_config().get("pricing") or {}).get("profiles") or {}).get(state.get("profile_id"))
        return profile if isinstance(profile, dict) else None

    def _draw_model_prices(self, d, w, h):
        state = self._model_prices
        model = state.get("model") or ""
        if not model:
            pricing = server._price_config(read_config())
            profile_id = pricing.get("profile_key") or ""
            if profile_id != state["profile_id"]:
                state.update(profile_id=profile_id, page=0)
                self._settings_status = ""
        profile = self._model_price_profile()
        self._text(d, 18, 77, "编辑单价" if model else "模型价格", 17, TEXT, bold=True)
        self._text(d, w - 18, 79, "返回列表" if model else "返回设置", 8, CORAL,
                   bold=True, anchor="rm")
        self._hit(w - 106, 64, w - 10, 94, "model_prices_back")
        if profile is None:
            self._text(d, 18, 124, "该计价分组已删除" if model else "请先在设置中选择自定义计价分组",
                       11, BAD)
            return
        name = profile.get("name") or state["profile_id"]
        multiplier = _num(profile.get("multiplier"), 1)
        self._text(d, 18, 107, self._fit_text(name, w - 120, 10, bold=True),
                   10, TEXT, bold=True)
        self._text(d, w - 18, 107, "×%g" % multiplier, 10, GOLD, numeric=True, anchor="rm")
        host = server._url_host(profile.get("base_url") or state["profile_id"])
        self._text(d, 18, 128, self._fit_text(host, w - 36, 8), 8, MUTED)
        models = profile.get("models") or {}
        if model:
            entry = models.get(model)
            if not isinstance(entry, dict):
                self._text(d, 18, 174, "该模型已不存在", 11, BAD)
                return
            title = self._fit_text(model, w - 36, 11, bold=True)
            self._text(d, 18, 157, title, 11, TEXT, bold=True)
            if title != model:
                self._text(d, 18, 175, self._fit_text(model[len(title) - 1:], w - 36, 9),
                           9, MUTED)
            self._text(d, 18, 196, "标准单价 · ¥ / 百万 token · 倍率前", 8, CORAL)
            for x, label in ((18, "输入"), (139, "缓存"), (260, "输出")):
                self._text(d, x, 220, label, 9, MUTED)
            idle = entry.get("idle")
            if isinstance(idle, dict):
                self._text(d, 18, 285, "闲时单价", 10, GOLD, bold=True)
                self._text(d, w - 18, 285, "已启用" if idle.get("enabled", True) else "已停用",
                           8, MUTED, anchor="rm")
                for x, label in ((18, "输入"), (139, "缓存"), (260, "输出")):
                    self._text(d, x, 309, label, 9, MUTED)
                windows = ["%s-%s" % (window.get("start", ""), window.get("end", ""))
                           for window in idle.get("windows") or [] if isinstance(window, dict)]
                if not windows and idle.get("start") and idle.get("end"):
                    windows.append("%s-%s" % (idle["start"], idle["end"]))
                if idle.get("weekend_all_day"):
                    windows.append("周末全天")
                if idle.get("days"):
                    windows.append("指定日期")
                self._text(d, 18, 371, self._fit_text(" · ".join(windows), w - 36, 8), 8, MUTED)
            self._rrect(d, 18, 426, w - 18, 466, 7, fill=CORAL)
            self._text(d, w / 2, 446, "保存单价", 10, "#150D0B", bold=True, anchor="mm")
            self._hit(18, 426, w - 18, 466, "model_price_save")
            self._rrect(d, 18, 484, w - 18, 518, 7, fill=PANEL_2,
                        outline=BAD, width=0.6)
            self._text(d, w / 2, 501, "删除此模型定价", 10, BAD, anchor="mm")
            self._hit(18, 484, w - 18, 518, "model_price_delete")
        else:
            rows = sorted(((key, value) for key, value in models.items() if isinstance(value, dict)),
                          key=lambda item: item[0].casefold())
            self._text(d, 18, 150, "单价 ¥ / 百万 token · 倍率前", 8, MUTED)
            self._text(d, w - 18, 150, "%d 个模型" % len(rows), 8, MUTED, anchor="rm")
            self._line(d, [(18, 165), (w - 18, 165)], BORDER, 0.7)
            pages = max(1, math.ceil(len(rows) / 6))
            state["page"] = max(0, min(state.get("page", 0), pages - 1))
            if not rows:
                self._text(d, w / 2, 274, "暂无已保存模型", 11, MUTED, anchor="mm")
            for index, (key, entry) in enumerate(rows[state["page"] * 6:(state["page"] + 1) * 6]):
                y = 174 + index * 62
                self._text(d, 18, y + 11, self._fit_text(key, w - 66, 10, bold=True),
                           10, TEXT, bold=True)
                for row, price_entry in enumerate((entry, entry.get("idle"))):
                    if not isinstance(price_entry, dict):
                        continue
                    for x, label, price_key in ((18, "输入", "input"), (139, "缓存", "cached"),
                                                 (260, "输出", "output")):
                        value = server._num(price_entry.get(price_key))
                        price = "%g" % value if value is not None and math.isfinite(value) and value >= 0 else "--"
                        prefix = ("闲 " if price_entry.get("enabled", True) else "闲(停) ") if row else ""
                        text = prefix + label + " " + price
                        self._text(d, x, y + 30 + row * 16, self._fit_text(text, 111, 8),
                                   8, GOLD if row else MUTED)
                self._line(d, [(w - 28, y + 7), (w - 23, y + 12), (w - 28, y + 17)], MUTED, 1)
                self._line(d, [(18, y + 59), (w - 18, y + 59)], BORDER, 0.6)
                self._hit(12, y, w - 12, y + 58, "model_price_edit:" + key)
            if pages > 1:
                for x, direction, target in ((118, -1, state["page"] - 1), (w - 118, 1, state["page"] + 1)):
                    color = MUTED if 0 <= target < pages else BORDER
                    self._line(d, [(x - direction * 3, 558), (x + direction * 3, 563),
                                   (x - direction * 3, 568)], color, 1.2)
                    if 0 <= target < pages:
                        self._hit(x - 18, 550, x + 18, 579, "model_prices_page:%d" % target)
                self._text(d, w / 2, 563, "%d / %d" % (state["page"] + 1, pages),
                           8, MUTED, anchor="mm")
        if self._settings_status:
            self._text(d, 18, 550 if model else 593,
                       self._fit_text(self._settings_status, w - 36, 8),
                       8, self._settings_status_color)

    def _sync_model_price_entries(self):
        state = self._model_prices
        model = state.get("model") or ""
        entry = ((self._model_price_profile() or {}).get("models") or {}).get(model)
        if not model or not isinstance(entry, dict):
            self._clear_entries()
            return
        key = ("model_prices", state["profile_id"], model)
        if self._entries and getattr(self, "_entries_key", None) == key:
            return
        self._clear_entries()
        self._entries_key = key
        for x, price_key in ((18, "input"), (139, "cached"), (260, "output")):
            self._entry("model_" + price_key, x, 232, 112, entry.get(price_key, ""))
            if isinstance(entry.get("idle"), dict):
                self._entry("model_idle_" + price_key, x, 321, 112, entry["idle"].get(price_key, ""))

    def _save_model_prices(self):
        state = self._model_prices
        cfg = read_config()
        profiles = (cfg.get("pricing") or {}).get("profiles") or {}
        profile = profiles.get(state["profile_id"])
        model = state.get("model") or ""
        entry = ((profile or {}).get("models") or {}).get(model)
        if not isinstance(profile, dict) or not isinstance(entry, dict):
            self._settings_status = "分组或模型已删除，请返回列表"
            self._settings_status_color = BAD
            self._render()
            return
        updated = copy.deepcopy(entry)
        try:
            for prefix, target in (("model_", updated), ("model_idle_", updated.get("idle"))):
                if not isinstance(target, dict):
                    continue
                if prefix == "model_idle_" and not target.get("enabled", True) and all(
                    target.get(key) is None and not self._entry_value(prefix + key)
                    for key in ("input", "cached", "output")
                ):
                    continue
                for key in ("input", "cached", "output"):
                    value = float(self._entry_value(prefix + key))
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                    target[key] = value
        except ValueError:
            self._settings_status = "三项单价都需填写非负数字，免费请填 0"
            self._settings_status_color = BAD
            self._render()
            return
        profile["models"][model] = updated
        if self._commit_pricing(cfg, "已保存单价：%s" % model):
            state["model"] = ""
            self._clear_entries()
            self._render()

    def _delete_model_price(self):
        state = self._model_prices
        profile_id, model = state["profile_id"], state.get("model") or ""
        profile = self._model_price_profile()
        if not model or not isinstance(((profile or {}).get("models") or {}).get(model), dict):
            return
        if not messagebox.askyesno(
            "删除模型定价",
            "确定删除分组“%s”中模型“%s”的定价吗？\n"
            "该模型的标准价和闲时配置会一起移除，其他分组与 API Key 不受影响。"
            % (profile.get("name") or profile_id, model),
            icon="warning", default=messagebox.NO, parent=self.root,
        ):
            self._render()
            return
        # The modal runs a Tk event loop; reload to preserve changes made while it was open.
        cfg = read_config()
        profile = ((cfg.get("pricing") or {}).get("profiles") or {}).get(profile_id)
        models = (profile or {}).get("models") or {}
        if not isinstance(models.get(model), dict):
            self._settings_status = "分组或模型已删除，请返回列表"
            self._settings_status_color = BAD
            self._render()
            return
        del models[model]
        if self._commit_pricing(cfg, "已删除定价：%s" % model):
            state["model"] = ""
            self._clear_entries()
            self._render()

    def _draw_switch_row(self, d, x, y, label, enabled, name):
        self._text(d, x, y, label, 8, MUTED)
        track_color = MINT if enabled else PANEL_2
        self._rrect(d, x + 127, y - 8, x + 161, y + 8, 8, fill=track_color,
                    outline=BORDER if not enabled else None, width=0.6)
        knob_x = x + 153 if enabled else x + 135
        d.ellipse(
            [int((knob_x - 6) * SCALE), int((y - 6) * SCALE),
             int((knob_x + 6) * SCALE), int((y + 6) * SCALE)],
            fill=TEXT if enabled else MUTED,
        )
        self._hit(x, y - 13, x + 166, y + 13, name)

    def _draw_mini_switch(self, d, x, y, label, enabled, name):
        self._text(d, x, y, label, 7.5, MUTED)
        self._rrect(d, x + 36, y - 8, x + 70, y + 8, 8,
                    fill=MINT if enabled else PANEL_2,
                    outline=BORDER if not enabled else None, width=0.6)
        knob_x = x + 62 if enabled else x + 44
        d.ellipse(
            [int((knob_x - 6) * SCALE), int((y - 6) * SCALE),
             int((knob_x + 6) * SCALE), int((y + 6) * SCALE)],
            fill=TEXT if enabled else MUTED,
        )
        self._hit(x - 2, y - 10, x + 72, y + 10, name)

    def _draw_compact(self, d, w, h):
        bal = self._balance()
        pricing = ((self.snapshot or {}).get("usage") or {}).get("pricing") or {}
        source = pricing.get("current_source")
        entry = pricing.get("current_entry") or {}
        if source == "idle":
            mode, mode_color = "闲价", GOLD
        elif (entry.get("idle") or {}).get("enabled", False):
            mode, mode_color = "忙价", MUTED
        elif source in ("configured", "reference", "official"):
            mode, mode_color = "标准", MUTED
        else:
            mode, mode_color = "未设", DIM
        if pricing.get("profile_ambiguous"):
            mode, mode_color = "选组", GOLD
        group = pricing.get("profile_name") or ""
        cy = h / 2.0
        amount = "%s%s" % (bal["symbol"], _money(bal["total"]))
        amount = self._fit_text(amount, w - 46, 10.5, numeric=True)
        self._text(d, 8, 9 if group else cy, mode, 8.5, mode_color)
        self._text(d, w - 8, 9 if group else cy, amount, 10.5,
                   "#E4E2DC", numeric=True, anchor="rm")
        if group:
            ratio = "×%g" % _num(pricing.get("multiplier"), 1)
            ratio = self._fit_text(ratio, w - 50, 9, numeric=True)
            ratio_width = _font(9 * SCALE, numeric=True).getlength(ratio) / SCALE
            self._text(d, 8, 23, self._fit_text(group, w - 21 - ratio_width, 9, bold=True),
                       9, "#DCE2E6", bold=True)
            self._text(d, w - 8, 23, ratio, 9, "#BCC7CF", numeric=True, anchor="rm")
        self._hit(0, 0, w, h, "expand")

    # --------------------------------------------------------------- rendering
    def _render(self):
        try:
            width, height = self.size
            self._draw_image(width, height, None)
            if not self.compact and self.view == "settings" and getattr(self, "_model_prices", None):
                self._sync_model_price_entries()
            elif (not self.compact and self.view == "settings"
                    and not self.idle_editor_open and not self._provider_menu):
                self._sync_entries()
            else:
                self._clear_entries()
        except Exception:  # noqa: BLE001
            log_exc(traceback.format_exc())

    def _apply_geometry(self, x, y):
        width, height = self.size
        self.root.geometry("%dx%d+%d+%d" % (width, height, int(x), int(y)))
        self.canvas.configure(width=width, height=height)
        self._geom = (int(x), int(y))

    def _apply_alpha(self):
        try:
            self.root.attributes("-alpha", 0.94 if self.compact else 1.0)
        except tk.TclError:
            pass

    def _schedule_position_save(self):
        if self._position_save_job is None:
            self._position_save_job = self.root.after(350, self._save_position_now)

    def _save_position_now(self):
        self._position_save_job = None
        if not self._dragging:
            self._persist_ui()

    def set_compact(self):
        if self.compact:
            return
        expanded_x, expanded_y = self.position
        target = self.compact_pos or [
            expanded_x + WIDTH - COMPACT_WIDTH,
            expanded_y,
        ]
        self.compact = True
        self.size = (COMPACT_WIDTH, COMPACT_HEIGHT)
        self.idle_editor_open = False
        self._provider_menu = False
        self._model_prices = None
        self._time_pick = None
        self._apply_alpha()
        self._clear_entries()
        self.root.geometry("%dx%d" % self.size)
        x, y = self._clamp(target[0], target[1], find_host_window())
        self.position = [x, y]
        self.compact_pos = list(self.position)
        self._apply_geometry(x, y)
        self._render()
        self._persist_ui()

    def set_expanded(self):
        if not self.compact:
            return
        self.compact_pos = list(self.position)
        target_x = self.position[0] + COMPACT_WIDTH - WIDTH
        target_y = self.position[1]
        self.compact = False
        self.size = (WIDTH, HEIGHT)
        self._apply_alpha()
        self.root.geometry("%dx%d" % self.size)
        x, y = self._clamp(target_x, target_y, find_host_window())
        self.position = [x, y]
        self._apply_geometry(x, y)
        self._render()
        self._persist_ui()

    def set_view(self, view):
        if view not in ("overview", "conversations", "usage", "settings"):
            return
        self.view = view
        self.idle_editor_open = False
        self._provider_menu = False
        self._model_prices = None
        self._time_pick = None
        self._clear_entries()
        self._render()
        self._persist_ui()

    # --------------------------------------------------------------- interaction
    def _bind(self):
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Leave>", lambda _e: self.canvas.configure(cursor=""))

    def _build_menu(self):
        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg=PANEL,
            fg=TEXT,
            bd=0,
            activebackground=CORAL,
            activeforeground="#160D0B",
        )
        self.follow_var = tk.BooleanVar(value=self.follow)
        self.menu.add_command(label="立即刷新", command=lambda: self.refresh(force_balance=True))
        self.menu.add_command(label="设置", command=lambda: self.set_view("settings"))
        self.menu.add_command(label="小窗 / 全窗", command=self.toggle_compact)
        self.menu.add_checkbutton(
            label="跟随 ChatGPT 窗口", variable=self.follow_var, command=self._toggle_follow
        )
        self.menu.add_command(label="重新加载界面", command=self._reload)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _toggle_follow(self):
        self.follow = bool(self.follow_var.get())
        self._persist_ui()
        self._render()

    def toggle_compact(self):
        if self.compact:
            self.set_expanded()
        else:
            self.set_compact()

    def _hit_at(self, x, y):
        for x1, y1, x2, y2, name in reversed(self._hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return name
        return ""

    def _on_hover(self, event):
        hit = self._hit_at(event.x, event.y)
        self.canvas.configure(cursor="hand2" if hit else "")

    def _on_press(self, event):
        hit = self._hit_at(event.x, event.y)
        self._press = (event.x_root, event.y_root, event.x, event.y, hit, time.monotonic())
        self._drag_win = (self.root.winfo_x(), self.root.winfo_y())
        self._dragging = False
        if self._hold_job:
            try:
                self.root.after_cancel(self._hold_job)
            except tk.TclError:
                pass
        self._hold_job = self.root.after(240, self._begin_drag)

    def _begin_drag(self):
        self._hold_job = None
        if not self._press or self._dragging:
            return
        self._dragging = True
        self._drag_origin = (self._press[0], self._press[1])
        if self.follow:
            self.follow = False
            self.follow_var.set(False)
        self.canvas.configure(cursor="fleur")

    def _on_motion(self, event):
        if not self._dragging:
            if not self._press:
                return
            if math.hypot(event.x_root - self._press[0], event.y_root - self._press[1]) < 4:
                return
            if self._hold_job:
                try:
                    self.root.after_cancel(self._hold_job)
                except tk.TclError:
                    pass
                self._hold_job = None
            self._begin_drag()
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        x, y = self._clamp(self._drag_win[0] + dx, self._drag_win[1] + dy,
                           find_host_window())
        self.position = [x, y]
        self._apply_geometry(x, y)

    def _on_release(self, event):
        if self._hold_job:
            try:
                self.root.after_cancel(self._hold_job)
            except tk.TclError:
                pass
            self._hold_job = None
        if self._dragging:
            self._dragging = False
            self.canvas.configure(cursor="")
            self.position = list(self._clamp(self.root.winfo_x(), self.root.winfo_y(),
                                             find_host_window()))
            self._apply_geometry(*self.position)
            if self.compact:
                self.compact_pos = list(self.position)
            self._persist_ui()
        elif self._press:
            _rx, _ry, px, py, hit, _started = self._press
            if hit and hit == self._hit_at(event.x, event.y):
                self._invoke(hit)
        self._press = None

    def _commit_pricing(self, cfg, message):
        try:
            write_config(cfg)
        except OSError as exc:
            self._settings_status = "写入失败：%s" % exc
            self._settings_status_color = BAD
            self._render()
            return False
        self._provider_menu = False
        self._entries_key = None
        self._idle_enabled_loaded_for = None
        self._pending_base = ""
        self._clear_entries()
        self._settings_status = message
        self._settings_status_color = MINT
        snapshot = getattr(self, "snapshot", None)
        if snapshot and snapshot.get("usage"):
            pricing = server._price_config(cfg)
            model = (snapshot["usage"].get("current") or {}).get("model") or ""
            snapshot["usage"]["pricing"] = server._pricing_summary(
                pricing, model, datetime.now().astimezone()
            )
        self._render()
        self.refresh()
        return True

    def _create_profile(self, source_key=""):
        name = simpledialog.askstring(
            "复制计价分组" if source_key else "新建计价分组",
            "分组名称，例如：示例 API · 优惠组",
            parent=self.root,
        )
        if not name or not name.strip():
            self._render()
            return
        cfg = read_config()
        pricing = dict(cfg.get("pricing") or {})
        profiles = dict(pricing.get("profiles") or {})
        source = profiles.get(source_key) or {}
        if source_key and not source:
            self._settings_status = "该分组已不存在，请重新选择"
            self._settings_status_color = BAD
            self._render()
            return
        base = (source.get("base_url")
                or self._entry_value("relay_base") or getattr(self, "_pending_base", "")
                or server._active_base_url(cfg)).rstrip("/")
        if not server._url_host(base):
            self._settings_status = "请先填写 API 地址"
            self._settings_status_color = BAD
            self._render()
            return
        profile_key = server.uuid.uuid4().hex
        profile = copy.deepcopy(source) if source_key else {"models": {}, "multiplier": 1.0}
        profile.update(name=name.strip(), base_url=base)
        profiles[profile_key] = profile
        pricing["profiles"] = profiles
        active_base = server._active_base_url(cfg)
        api_key = self._entry_value("relay_key") or server._active_api_key(cfg)
        server._bind_pricing_profile(
            pricing, profile_key, base,
            api_key if server._url_host(base) == server._url_host(active_base) else "",
        )
        cfg["pricing"] = pricing
        self._commit_pricing(cfg, "已%s分组：%s" % ("复制" if source_key else "新建", name.strip()))

    def _invoke(self, hit):
        if hit == "refresh":
            if self.busy:
                return
            self.refresh(force_balance=True)
        elif hit == "collapse":
            self.set_compact()
        elif hit == "expand":
            self.set_expanded()
        elif hit.startswith("tab:"):
            self.set_view(hit.split(":", 1)[1])
        elif hit == "follow":
            self.follow = not self.follow
            self.follow_var.set(self.follow)
            self._persist_ui()
            self._render()
        elif hit == "texture":
            self.texture = not self.texture
            self._persist_ui()
            self._render()
        elif hit == "idle_toggle":
            self.idle_enabled = not self.idle_enabled
            self._render()
        elif hit == "model_prices":
            pricing = server._price_config(read_config())
            if pricing.get("provider") != "custom" or not pricing.get("profile_key"):
                return
            self._model_prices = {"profile_id": pricing["profile_key"], "page": 0, "model": ""}
            self._settings_status = ""
            self._clear_entries()
            self._render()
        elif hit == "model_prices_back" and getattr(self, "_model_prices", None):
            if self._model_prices.get("model"):
                self._model_prices["model"] = ""
            else:
                self._model_prices = None
            self._settings_status = ""
            self._clear_entries()
            self._render()
        elif hit.startswith("model_prices_page:") and getattr(self, "_model_prices", None):
            self._model_prices["page"] = int(hit.split(":", 1)[1])
            self._render()
        elif hit.startswith("model_price_edit:") and getattr(self, "_model_prices", None):
            model = hit.split(":", 1)[1]
            entry = ((self._model_price_profile() or {}).get("models") or {}).get(model)
            if isinstance(entry, dict):
                self._model_prices["model"] = model
                self._settings_status = ""
                self._clear_entries()
                self._render()
        elif hit == "model_price_save" and getattr(self, "_model_prices", None):
            self._save_model_prices()
        elif hit == "model_price_delete" and getattr(self, "_model_prices", None):
            self._delete_model_price()
        elif hit == "price_sync":
            cfg = read_config()
            pricing = dict(cfg.get("pricing") or {})
            if pricing.get("provider") == "custom":
                self._settings_status = "自定义价：修改单价后点「保存设置」"
                self._settings_status_color = GOLD
                self._render()
                return
            base = server._active_base_url(cfg)
            exact = server.pricing_catalog.detect(base)
            hint = exact or server.pricing_catalog.detect_hint(base)
            label = (server.pricing_catalog.PROVIDERS.get(
                pricing.get("provider") or "") or {}).get("label") or ""
            if hint and (exact or not pricing.get("provider")):
                pricing["provider"] = hint
                pricing["provider_base"] = base
                cfg["pricing"] = pricing
                try:
                    write_config(cfg)
                except OSError:
                    pass
                label = (server.pricing_catalog.PROVIDERS.get(hint) or {}).get("label") or ""
            started = server.pricing_catalog.ensure_fresh(force=True)
            if label:
                self._settings_status = "正在同步 %s 官方价…" % label
            else:
                self._settings_status = "未识别当前 API 平台，请点「切换」选择"
            self._settings_status_color = MUTED
            if not getattr(self, "_price_watch", False):
                self._price_watch = True
                self.root.after(500, self._watch_price_sync)
            self._render()
        elif hit == "price_provider":
            self._pending_base = self._entry_value("relay_base") \
                if "relay_base" in self._entries else ""
            self._clear_entries()
            self._provider_page = 0
            self._provider_menu = True
            self._render()
        elif hit.startswith("provider_page:"):
            self._provider_page = int(hit.split(":", 1)[1])
            self._render()
        elif hit == "provider_close":
            self._provider_menu = False
            self._render()
        elif hit.startswith("profile_delete:"):
            profile_key = hit.split(":", 1)[1]
            cfg = read_config()
            pricing = dict(cfg.get("pricing") or {})
            profiles = dict(pricing.get("profiles") or {})
            profile = profiles.get(profile_key)
            if not isinstance(profile, dict):
                self._provider_menu = False
                self._render()
                return
            name = profile.get("name") or profile_key
            if not messagebox.askyesno(
                "删除计价分组",
                "确定删除“%s”及其保存的价格吗？\nAPI Key 不会删除。" % name,
                icon="warning",
                parent=self.root,
            ):
                self._render()
                return
            active = server._price_config(cfg).get("profile_key")
            profiles.pop(profile_key, None)
            pricing["profiles"] = profiles
            pricing["profile_bindings"] = {
                key: value for key, value in (pricing.get("profile_bindings") or {}).items()
                if value != profile_key
            }
            if active == profile_key or pricing.get("profile") == profile_key:
                base = server._active_base_url(cfg)
                replacement = server._pricing_profile_key(pricing, base, server._active_api_key(cfg))
                pricing["profile"] = replacement
                if replacement:
                    server._bind_pricing_profile(pricing, replacement, base, server._active_api_key(cfg))
                else:
                    pricing["provider"] = "custom" if server._pricing_profiles(pricing, base) else ""
                    pricing["models"] = {}
                    pricing["multiplier"] = 1.0
            cfg["pricing"] = pricing
            self._commit_pricing(cfg, "已删除分组：%s" % name)
        elif hit == "profile_new":
            self._create_profile()
        elif hit.startswith("profile_copy:"):
            self._create_profile(hit.split(":", 1)[1])
        elif hit.startswith("provider_pick:"):
            key = hit.split(":", 1)[1]
            if key in ("new", "custom:new"):
                self._create_profile()
                return
            cfg = read_config()
            pricing = dict(cfg.get("pricing") or {})
            if key.startswith("custom:"):
                profile_key = key.split(":", 1)[1]
                profile = (pricing.get("profiles") or {}).get(profile_key)
                if not isinstance(profile, dict):
                    self._settings_status = "该分组已不存在，请重新选择"
                    self._settings_status_color = BAD
                    self._render()
                    return
                base = profile.get("base_url") or profile_key
                api_key = server._active_api_key(cfg) if server._url_host(base) == server._url_host(
                    server._active_base_url(cfg)
                ) else ""
                server._bind_pricing_profile(pricing, profile_key, base, api_key)
                key = "custom"
            elif key:
                pricing["provider"] = key
                pricing["provider_base"] = server._active_base_url(cfg)
            else:
                pricing.pop("provider", None)
                pricing.pop("provider_base", None)
            cfg["pricing"] = pricing
            label = (server.pricing_catalog.PROVIDERS.get(key) or {}).get("label")
            if key == "custom":
                label = profile.get("name") or profile_key
            self._commit_pricing(cfg, "已选择：%s" % (label or "自动识别"))
        elif hit == "idle_edit":
            self._clear_entries()
            self.idle_editor_open = True
            self._time_pick = None
            self._render()
        elif hit == "idle_back":
            self.idle_editor_open = False
            self._time_pick = None
            self._render()
        elif hit == "idle_add" and len(self.idle_windows) < 6:
            self.idle_windows.append(["00:00", "08:00"])
            self._render()
        elif hit.startswith("idle_del:"):
            idx = int(hit.split(":")[1])
            if 0 <= idx < len(self.idle_windows):
                self.idle_windows.pop(idx)
            self._render()
        elif hit == "idle_weekend":
            self.idle_weekend = not self.idle_weekend
            self._render()
        elif hit.startswith("idle_time:"):
            _, raw_idx, kind = hit.split(":")
            idx = int(raw_idx)
            value = self.idle_windows[idx][0 if kind == "start" else 1]
            hour, minute = (int(part) for part in value.split(":", 1))
            self._time_pick = (idx, kind)
            self._time_hour = 0 if hour >= 24 else hour
            self._time_minute = minute
            self._render()
        elif hit.startswith("tp_h:"):
            self._time_hour = int(hit.split(":")[1])
            self._render()
        elif hit.startswith("tp_m:"):
            self._time_minute = int(hit.split(":")[1])
            self._render()
        elif hit == "tp_cancel":
            self._time_pick = None
            self._render()
        elif hit == "tp_ok" and self._time_pick:
            idx, kind = self._time_pick
            slot = 0 if kind == "start" else 1
            self.idle_windows[idx][slot] = "%02d:%02d" % (
                self._time_hour, self._time_minute
            )
            self._time_pick = None
            self._render()
        elif hit == "save":
            self.save_settings()
        elif hit == "test":
            self.test_settings()

    def _clamp(self, x, y, rect):
        if not rect:
            return int(x), int(y)
        left, top, right, bottom = rect
        width, height = self.size
        min_x = left + EDGE
        max_x = right - width - EDGE
        min_y = top + EDGE
        max_y = bottom - height - EDGE
        if max_x < min_x:
            min_x = max_x = left + EDGE
        if max_y < min_y:
            min_y = max_y = top + EDGE
        return int(min(max(x, min_x), max_x)), int(min(max(y, min_y), max_y))

    def _default_position(self, rect):
        left, top, right, _bottom = rect
        return [right - self.size[0] - MARGIN, top + 82]

    def _menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # --------------------------------------------------------------- window
    def _show_at(self, x, y):
        key = (int(x), int(y))
        if not self.visible:
            self._apply_geometry(x, y)
            self.root.deiconify()
            self.visible = True
            self._set_timer_resolution(True)
            self._place_above_host()
            return
        if key != self._geom:
            self._apply_geometry(x, y)

    def _place_above_host(self, host_hwnd=None):
        host_hwnd = host_hwnd or self._host_hwnd
        if not host_hwnd or not self.visible:
            return
        try:
            own_hwnd = user32.GetAncestor(self.root.winfo_id(), 2)
            if not own_hwnd:
                return
            insert_after = user32.GetWindow(host_hwnd, 3)  # GW_HWNDPREV
            while insert_after:
                if insert_after == own_hwnd:
                    return
                style = int(user32.GetWindowLongPtrW(insert_after, -20) or 0)
                if not style & 0x8:  # WS_EX_TOPMOST
                    break
                insert_after = user32.GetWindow(insert_after, 3)
            user32.SetWindowPos(
                own_hwnd, insert_after or 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0010,  # NOSIZE | NOMOVE | NOACTIVATE
            )
        except (OSError, tk.TclError):
            pass

    def _hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False
            self._set_timer_resolution(False)

    def _set_timer_resolution(self, high):
        if winmm is None or high == self._high_timer:
            return
        try:
            if high:
                winmm.timeBeginPeriod(1)
            else:
                winmm.timeEndPeriod(1)
            self._high_timer = high
        except OSError:
            pass

    def tick(self):
        try:
            now = time.monotonic()
            if self.plugin_config and now - self._last_plugin_check >= 2.0:
                self._last_plugin_check = now
                enabled = plugin_enabled(self.plugin_config)
                self._disabled_checks = self._disabled_checks + 1 if enabled is False else 0
                if self._disabled_checks >= 2:
                    self.quit()
                    return
            hwnd = self._host_hwnd
            if hwnd and not host_alive(hwnd):
                hwnd = self._host_hwnd = None
            if hwnd is None or now - self._host_scan_at > 2.0:
                self._host_scan_at = now
                hwnd = self._host_hwnd = find_host_hwnd() or hwnd
            rect = _rect(hwnd) if hwnd else None
            if rect is None:
                self._hide()
            else:
                left, top, right, _bottom = rect
                moved = False
                if self._host_origin and not self.follow and not self._dragging:
                    dx = left - self._host_origin[0]
                    dy = top - self._host_origin[1]
                    if dx or dy:
                        self.position[0] += dx
                        self.position[1] += dy
                        if self.compact:
                            self.compact_pos = list(self.position)
                        moved = True
                self._host_origin = (left, top)
                if self.follow and not self._dragging:
                    x, y = self._default_position(rect)
                else:
                    x, y = self.position
                x, y = self._clamp(x, y, rect)
                if not self.compact or self.follow:
                    self.position = [x, y]
                if self.compact and self.follow:
                    self.compact_pos = [x, y]
                if moved:
                    self._schedule_position_save()
                pid = _foreground_pid()
                if pid != self._front_pid:
                    self._front_pid = pid
                    self._front_name = _process_name(pid).lower()
                if now - self._last_z_order > 0.12:
                    self._last_z_order = now
                    self._place_above_host(hwnd)
                self._show_at(x, y)
                self._set_timer_resolution(True)
        except Exception:  # noqa: BLE001
            log_exc(traceback.format_exc())
        self._tick_count += 1
        elapsed = time.monotonic() - self._tick_window
        if elapsed >= 2.0:
            self._tick_hz = self._tick_count / elapsed
            self._tick_count = 0
            self._tick_window = time.monotonic()
        self.root.after(12 if self.visible else 16, self.tick)

    # --------------------------------------------------------------- data
    def _periodic_refresh(self):
        if not self.busy:
            self.refresh()
        self.root.after(max(1000, int(self.refresh_sec * 1000)), self._periodic_refresh)

    def refresh(self, force_balance=False):
        if self.busy:
            return
        self.busy = True
        self.fetching = True
        self._render()
        threading.Thread(target=self._fetch, args=(force_balance,), daemon=True).start()

    def _fetch(self, force_balance=False):
        try:
            data = server.widget_snapshot(
                max_age=self.balance_refresh_sec,
                force=bool(force_balance),
            )
            self.root.after(0, lambda: self._apply(data, ""))
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda: self._apply(None, str(exc)))

    def _apply(self, data, error):
        self.busy = False
        self.fetching = False
        if data is not None:
            if data.get("usage"):
                model = (data["usage"].get("current") or {}).get("model") or ""
                data["usage"]["pricing"] = server._pricing_summary(
                    server._price_config(read_config()), model, datetime.now().astimezone()
                )
            self.snapshot = data
            self.last_data = data
            self.last_error = ""
        else:
            self.last_error = error or "刷新失败"
        self._render()
        write_state(self._state())
        if data is None:
            self._set_refresh_status("失败")

    def _set_refresh_status(self, text):
        if self._refresh_job:
            try:
                self.root.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
        self._refresh_status = text
        self._render()
        self._refresh_job = self.root.after(1600, self._clear_refresh_status)

    def _clear_refresh_status(self):
        self._refresh_job = None
        self._refresh_status = ""
        self._render()

    # --------------------------------------------------------------- settings
    def _entry_value(self, key, default=""):
        entry = self._entries.get(key)
        if entry is None:
            return default
        return entry.get().strip()

    def apply_config(self, config):
        self.config = config
        self.ui = dict(config.get("ui") or {})
        self.follow = bool(self.ui.get("follow", True))
        self.refresh_sec = max(1.0, _num(self.ui.get("refresh_sec"), self.refresh_sec))
        self.balance_refresh_sec = max(
            3.0, _num(self.ui.get("balance_refresh_sec"), self.balance_refresh_sec)
        )
        self.texture = bool(self.ui.get("texture", True))
        self.follow_var.set(self.follow)
        self.refresh(force_balance=True)
        self._render()

    def save_settings(self):
        cfg = read_config()
        values, model = self._settings_values()
        relay = dict(cfg.get("relay") or {})
        relay.update({
            "base_url": self._entry_value("relay_base"),
            "api_key": self._entry_value("relay_key"),
            "inherit": False,
        })
        cfg["relay"] = relay
        pricing = dict(cfg.get("pricing") or {})
        profile_key = ""
        if values.get("provider_key") == "custom" and relay["base_url"]:
            profile_key = values.get("profile_key") or server._pricing_profile_key(
                pricing, relay["base_url"], relay["api_key"]
            )
            if not profile_key and server._pricing_profiles(pricing, relay["base_url"]):
                self._settings_status = "同一 API 有多个分组，请先点「切换」选择"
                self._settings_status_color = GOLD
                self._render()
                return
            profile_key = profile_key or server.uuid.uuid4().hex
            profiles = dict(pricing.get("profiles") or {})
            profile = dict(profiles.get(profile_key) or {})
            profile["name"] = self._entry_value("profile_name").strip() or profile.get("name") or profile_key
            profile["base_url"] = relay["base_url"]
            profiles[profile_key] = profile
            pricing["profiles"] = profiles
            server._bind_pricing_profile(pricing, profile_key, relay["base_url"], relay["api_key"])
            cfg["pricing"] = pricing
        if "access_id" in self._entries:
            credentials = {
                "access_key_id": self._entry_value("access_id"),
                "access_key_secret": self._entry_value("access_secret"),
            }
            if (cfg.get("pricing") or {}).get("provider") == "ark":
                cfg["volcengine"] = credentials
            else:
                cfg["aliyun"] = credentials
        try:
            multiplier = float(self._entry_value("multiplier", "1") or 1)
            input_price = float(self._entry_value("price_input") or 0)
            cached_price = float(self._entry_value("price_cached") or 0)
            output_price = float(self._entry_value("price_output") or 0)
            if any(not math.isfinite(value) or value < 0
                   for value in (multiplier, input_price, cached_price, output_price)):
                raise ValueError
            idle_windows = []
            for start_text, end_text in self.idle_windows:
                start = server._clock_minutes(start_text)
                end = server._clock_minutes(end_text)
                if start is None or end is None or start >= 24 * 60 or start == end:
                    raise ValueError
                idle_windows.append({"start": start_text, "end": end_text})
            idle_weekend = bool(self.idle_weekend)
            idle_input_text = self._entry_value("idle_input")
            idle_cached_text = self._entry_value("idle_cached")
            idle_output_text = self._entry_value("idle_output")
            idle_prices = (
                idle_input_text,
                idle_cached_text,
                idle_output_text,
            )
            has_schedule = bool(idle_windows or idle_weekend)
            if any(idle_prices) or self.idle_enabled:
                if not has_schedule or not all(idle_prices):
                    raise ValueError
                idle_input = float(idle_input_text)
                idle_cached = float(idle_cached_text)
                idle_output = float(idle_output_text)
                if any(not math.isfinite(value) or value < 0
                       for value in (idle_input, idle_cached, idle_output)):
                    raise ValueError
            else:
                idle_input = idle_cached = idle_output = None
            refresh_sec = max(1, int(float(self._entry_value("refresh_sec", "2") or 2)))
            balance_refresh_sec = max(
                3, int(float(self._entry_value("balance_refresh_sec", "8") or 8))
            )
        except ValueError:
            self._settings_status = "开启时段计价需填时段和三项单价，其余数字也要正确"
            self._settings_status_color = BAD
            self._render()
            return
        pricing = dict(cfg.get("pricing") or {})
        profile = (
            dict((pricing.get("profiles") or {}).get(profile_key) or {})
            if profile_key else {}
        )
        models = dict(profile.get("models") or {}) if profile_key else dict(pricing.get("models") or {})
        has_idle = bool(idle_windows or idle_weekend or any(idle_prices))
        if model and (any(self._entry_value(key) for key in
                         ("price_input", "price_cached", "price_output")) or has_idle):
            configured = dict(models.get(model) or {})
            configured.update({
                "input": input_price,
                "cached": cached_price,
                "output": output_price,
            })
            if has_idle:
                configured["idle"] = {
                    "enabled": bool(self.idle_enabled),
                    "windows": idle_windows,
                    "weekend_all_day": bool(idle_weekend),
                    "input": idle_input,
                    "cached": idle_cached,
                    "output": idle_output,
                }
            else:
                configured.pop("idle", None)
            models[model] = configured
        cfg["pricing"] = pricing
        if profile_key:
            profile["models"] = models
            profile["multiplier"] = multiplier
            profiles = dict(pricing.get("profiles") or {})
            profiles[profile_key] = profile
            cfg["pricing"]["profiles"] = profiles
            cfg["pricing"]["profile"] = profile_key
        else:
            cfg["pricing"].update(multiplier=multiplier, models=models)
        ui = dict(cfg.get("ui") or {})
        ui["follow"] = self.follow
        ui["texture"] = self.texture
        ui["refresh_sec"] = refresh_sec
        ui["balance_refresh_sec"] = balance_refresh_sec
        ui["compact"] = self.compact
        ui["view"] = self.view
        ui["pos"] = list(self.position)
        ui["compact_pos"] = list(self.compact_pos)
        cfg["ui"] = ui
        try:
            write_config(cfg)
        except OSError as exc:
            self._settings_status = "写入失败：%s" % exc
            self._settings_status_color = BAD
            self._render()
            return
        if values.get("provider_key") == "custom":
            self._settings_status = "已保存到配置档：%s" % self._entry_value("profile_name")
        elif values.get("official"):
            self._settings_status = "已保存，价格用官方价"
        else:
            self._settings_status = "已保存，正在刷新"
        self._settings_status_color = MINT
        self.apply_config(cfg)
        self.root.after(1800, self._clear_settings_status)

    def _clear_settings_status(self):
        self._settings_status = ""
        if not self.compact and self.view == "settings":
            self._render()

    def _watch_price_sync(self):
        state = server.pricing_catalog.sync_state()
        if state["busy"]:
            self.root.after(700, self._watch_price_sync)
            return
        self._price_watch = False
        stamp = ""
        if state.get("at"):
            stamp = datetime.fromtimestamp(state["at"]).strftime(" · %H:%M")
        values, _model = self._settings_values()
        label = (values.get("provider") or {}).get("label") or ""
        failures = [
            item for item in state.get("error") or []
            if not label or item.startswith(label)
        ]
        if failures:
            self._settings_status = "同步完成，个别平台保留旧价" + stamp
            self._settings_status_color = GOLD
        elif not values.get("official"):
            self._settings_status = "官方价表未收录该模型，已保留自定义单价" + stamp
            self._settings_status_color = GOLD
        else:
            self._settings_status = "官方价已更新" + stamp
            self._settings_status_color = MINT
        self.refresh(force_balance=True)
        self._render()
        self.root.after(4000, self._clear_settings_status)

    def test_settings(self):
        base = self._entry_value("relay_base").rstrip("/")
        self._settings_status = "正在测试…"
        self._settings_status_color = GOLD
        self._render()

        def run():
            try:
                _text, data = server.report_balance()
                total = server._balance_total(data)
            except Exception:  # noqa: BLE001
                total = None
            if total is not None:
                message = "账单接口正常：¥%s" % _money(total)
                color = MINT
            elif base:
                root = base[:-3] if base.endswith("/v1") else base
                origin = "/".join(root.split("/")[:3])
                probe = {
                    "base_url": base,
                    "api_key": self._entry_value("relay_key"),
                    "root": root,
                    "origin": origin,
                    "name": "relay",
                    "endpoint": "",
                }
                try:
                    result, _diag = server.probe_relay(probe)
                    message = "余额接口正常：¥%s" % _money(result.get("remaining"))
                    color = MINT
                except Exception as exc:  # noqa: BLE001
                    message = "连接失败：%s" % str(exc)[:72]
                    color = BAD
            else:
                message = "未查到可用余额，检查 API Key 或 AccessKey"
                color = BAD
            self.root.after(0, lambda: self._test_done(message, color))

        threading.Thread(target=run, daemon=True).start()

    def _test_done(self, message, color):
        self._settings_status = message
        self._settings_status_color = color
        self.refresh(force_balance=True)
        self._render()

    def _persist_ui(self):
        cfg = read_config()
        ui = dict(cfg.get("ui") or {})
        ui["pos"] = list(self.position)
        ui["compact_pos"] = list(self.compact_pos)
        ui["follow"] = self.follow
        ui["refresh_sec"] = int(self.refresh_sec)
        ui["balance_refresh_sec"] = int(self.balance_refresh_sec)
        ui["compact"] = self.compact
        ui["view"] = self.view
        ui["texture"] = self.texture
        cfg["ui"] = ui
        try:
            write_config(cfg)
            self.config = cfg
        except OSError:
            pass

    def show_error(self):
        if not self.last_error:
            messagebox.showinfo("Token Lens", "暂时没有错误信息。", parent=self.root)
            return
        messagebox.showerror("刷新失败", self.last_error[:1500], parent=self.root)

    def quit(self):
        self._set_timer_resolution(False)
        self._persist_ui()
        write_state({"running": False, "pid": os.getpid()})
        try:
            self._srv.close()
        except OSError:
            pass
        self.root.destroy()


def acquire_control_socket():
    sock = socket.socket()
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", CONTROL_PORT))
        sock.listen(5)
        return sock
    except OSError:
        sock.close()
        return None


def _ask_running(command, timeout=2.0):
    try:
        with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=timeout) as sock:
            sock.sendall(command.encode("utf-8"))
            return sock.recv(64).decode("utf-8", "replace").strip().lower()
    except OSError:
        return ""


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass
    control = acquire_control_socket()
    if control is None:
        if _ask_running("ping") == "pong":
            return 0
        _ask_running("quit")
        deadline = time.time() + 8
        while time.time() < deadline and control is None:
            time.sleep(0.25)
            control = acquire_control_socket()
        if control is None:
            return 0
    plugin_config = None
    server.pricing_catalog.ensure_fresh()
    try:
        idx = sys.argv.index("--plugin-config")
        plugin_config = sys.argv[idx + 1]
    except (ValueError, IndexError):
        pass
    try:
        Widget(control, plugin_config=plugin_config).root.mainloop()
    except Exception:  # noqa: BLE001
        log_exc(traceback.format_exc())
        return 1
    finally:
        write_state({"running": False, "pid": os.getpid()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
