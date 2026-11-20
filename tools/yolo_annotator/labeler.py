"""Windows desktop labeler for the sunnypilot YOLO dataset.

Tkinter + Pillow only (no torch / no GPU): open a batch, browse and filter
images, draw / move / resize / delete boxes, pick one of the taxonomy classes,
review the automatic pre-labels and sync the result back to the Linux inbox in
the background.

Run with ``--dataset <batch dir>`` (or drop a ``dataset_dir.txt`` next to this
file).  ``--selftest`` builds the whole UI headless, exercises zoom / draw /
undo and exits with status 0 -- used for the Linux (xvfb) and Windows
verification runs.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Sequence

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageGrab, ImageTk

import dataset as ds
import demo
import geometry
import sync as sync_mod
from undo import UndoStack

APP_TITLE = "sunnypilot YOLO 标注器"
APP_VERSION = sync_mod.APP_VERSION
APP_DIR = Path(__file__).resolve().parent

#: Default batch used by run_labeler.bat / --selftest when nothing is given.
DEFAULT_DATASET = r"C:\Users\lsr\sunnypilot-yolo\dataset\batch_001"

#: How many images keep their undo history in memory.
UNDO_HISTORY_IMAGES = 200

BG = "#1e1f22"
BG_PANEL = "#26272b"
BG_LIST = "#1b1c1f"
FG = "#d8d8d8"
FG_DIM = "#8b8d90"
COLOR_OK = "#7bd88f"
COLOR_WARN = "#e5c07b"
COLOR_ERR = "#e06c75"
COLOR_INFO = "#9cdcfe"

HELP_TEXT = f"""\
{APP_TITLE} — 快捷键与用法
========================================

【浏览图片】
  A / ←            上一张（自动保存当前修改）
  D / →            下一张（自动保存）
  左侧列表          点击跳转；上方“筛选”可只看未复核 / 有自动两轮 / 高优先级
  搜索框            按 id、route、优先级、小时过滤

【画框与改框】
  左键拖拽空白处    画新框（用当前类别）
  左键点框          选中
  左键拖动框内      移动框
  拖动 8 个手柄     缩放框（四角 + 四边）
  双击框            弹出类别菜单（改类 / 删除）
  Del / Backspace   删除选中框

【类别】（数字快捷键，选中框时改类，未选中时设为新框默认类别）
  1-9, 0            第 1-10 类
  Shift+1 … Shift+0 第 11-20 类
  F1 / F2           第 21 / 22 类
  右侧调色板         也可以直接点击

【视图】
  滚轮              以光标为中心缩放
  + / -             放大 / 缩小
  F                 适应窗口
  空格 + 拖拽       平移（右键 / 中键拖拽也可以）

【复核】
  N                 标记“无目标”并复核（清空本图所有框后保存）
  R                 切换本图“已复核”状态
  Ctrl+S            保存当前图
  Ctrl+Z            撤销（每张图独立历史）
  Ctrl+Y            重做（Ctrl+Shift+Z 亦可）
  H                 显示本帮助

【进度与同步】
  顶部显示 已复核 / 总数、同步状态；保存后 3 秒自动上传（debounce），
  退出时与每 60 秒兜底上传；失败会指数退避重试。
  右上“立即同步”按钮可强制上传；“统计”查看逐类计数。

【自动预标】
  虚线框 + 置信度 = 自动预标（未改动）；实线框 = 手工框。
  选中 / 移动 / 改类 / 删除后即转为手工框（保存时写入 labels/）。

【文件位置】
  标签: <batch>/labels/<id>.txt   复核状态: <batch>/review_state.json
  同步配置: 本程序目录下的 sync.json（缺失则仅本地保存）
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def hotkey_for_index(index: int) -> str:
    """Human readable shortcut for the *index*-th class (0 based)."""
    if index < 9:
        return str(index + 1)
    if index == 9:
        return "0"
    if 10 <= index <= 18:
        return f"Shift+{index - 9}"
    if index == 19:
        return "Shift+0"
    if index == 20:
        return "F1"
    if index == 21:
        return "F2"
    return ""


SHIFTED_DIGITS = {
    "exclam": 1, "at": 2, "numbersign": 3, "dollar": 4, "percent": 5,
    "asciicircum": 6, "ampersand": 7, "asterisk": 8, "parenleft": 9,
    "parenright": 0,
}


def class_index_from_key(keysym: str, shift: bool) -> int | None:
    """Map a key press to a class index (0 based) or ``None``."""
    keysym = str(keysym)
    if len(keysym) == 1 and keysym.isdigit():
        digit = int(keysym)
        if shift:
            return 19 if digit == 0 else 9 + digit
        return 9 if digit == 0 else digit - 1
    if keysym.startswith("KP_") and keysym[3:].isdigit():
        digit = int(keysym[3:])
        if shift:
            return 19 if digit == 0 else 9 + digit
        return 9 if digit == 0 else digit - 1
    if keysym in SHIFTED_DIGITS:
        digit = SHIFTED_DIGITS[keysym]
        return 19 if digit == 0 else 9 + digit
    if keysym == "F1":
        return 20
    if keysym == "F2":
        return 21
    return None


def contrast_text(color: Sequence[int]) -> str:
    """Black or white text, whichever reads better on *color*."""
    r, g, b = (float(c) for c in color[:3])
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#101010" if luminance > 150 else "#ffffff"


def print_safe(message: str) -> None:
    """``print`` that never raises on an unencodable console/redirect.

    Windows consoles and redirected streams can use cp936 (or pytest capture
    objects can), which cannot encode characters such as the em dash.  Fall
    back to ASCII escapes instead of crashing the selftest / sync check.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            print(message.encode("ascii", "backslashreplace").decode("ascii"))
        except Exception:
            pass


def _write_log(message: str, filename: str = "labeler.log") -> None:
    """Append a line to ``labeler.log`` next to this file (Windows has no console)."""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(APP_DIR / filename, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message.rstrip()}\n")
    except OSError:
        pass


@dataclass
class DragState:
    """State of the current mouse drag on the canvas."""

    mode: str  # "new" | "move" | "resize" | "pan"
    start_x: float = 0.0
    start_y: float = 0.0
    cur_x: float = 0.0
    cur_y: float = 0.0
    origin_box: geometry.Box | None = None  # image-space box at drag start
    index: int | None = None
    handle: str | None = None
    moved: bool = False
    button: int = 1


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
class LabelerApp:
    """The whole GUI.  One instance owns one batch."""

    def __init__(self, root: tk.Tk, dataset_dir: str | os.PathLike,
                 no_sync: bool = False, selftest: bool = False) -> None:
        self.root = root
        self.dataset = ds.Dataset.open(dataset_dir)
        self.sync: sync_mod.SyncWorker | None = None
        self.selftest = selftest
        self._alive = True
        self._render_scheduled = False
        self._suppress_list_event = False
        self._status_reset_job: str | None = None
        self._search_job: str | None = None

        self.filtered: list[ds.ImageEntry] = list(self.dataset.images)
        self.index = 0
        self.current: ds.ImageEntry | None = None
        self.pil_image: Image.Image | None = None
        self.img_w = 0
        self.img_h = 0
        self.labels: list[ds.Label] = []
        self.selected: int | None = None
        self.current_class: int = self.dataset.taxonomy.ids[0] if self.dataset.taxonomy.classes else 0
        self.view = geometry.View()
        self.dirty = False
        self.drag: DragState | None = None
        self._space_down = False
        self._photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._undo: collections.OrderedDict[str, UndoStack] = collections.OrderedDict()
        self._view_initialised = False
        self._user_interacted = False
        self._palette_buttons: dict[int, tk.Button] = {}
        self._box_font: tkfont.Font | None = None
        self._caption_font: tkfont.Font | None = None

        self._build_fonts()
        self._build_ui()
        self._bind_events()
        self._init_sync(no_sync=no_sync)
        self._open_first_image()
        self._update_progress()
        self.root.after(150, self._first_layout)
        self.root.after(500, self._poll_sync)
        self.root.after(20000, self._autosave_tick)
        self.root.after(300, self._flush_warnings)

    # -- fonts ------------------------------------------------------------
    def _build_fonts(self) -> None:
        base = tkfont.nametofont("TkDefaultFont")
        family = base.actual("family")
        self._font_small = base.copy()
        self._font_small.configure(size=8)
        self._font_normal = base.copy()
        self._font_bold = base.copy()
        self._font_bold.configure(weight="bold")
        self._box_font = base.copy()
        self._box_font.configure(family=family, size=8, weight="bold")
        self._caption_font = base.copy()
        self._caption_font.configure(family=family, size=8)

    # -- UI ---------------------------------------------------------------
    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.geometry(self._default_geometry())
        root.minsize(840, 560)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
        style.configure("TButton", padding=(6, 2))
        style.configure("TCombobox", padding=2)

        root.grid_rowconfigure(1, weight=1)
        root.grid_columnconfigure(1, weight=1)

        # ---- top bar ----
        top = ttk.Frame(root, padding=(8, 5, 8, 2))
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        top.grid_columnconfigure(3, weight=1)

        self._dataset_var = tk.StringVar(value=f"批次 {self.dataset.batch} · {self.dataset.root}")
        ttk.Label(top, textvariable=self._dataset_var, style="Dim.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w")
        self._progress_var = tk.StringVar(value="已复核 0/0")
        ttk.Label(top, textvariable=self._progress_var,
                  font=self._font_bold).grid(row=1, column=0, sticky="w", pady=(3, 0))
        ttk.Button(top, text="保存 Ctrl+S", command=self.save_current).grid(
            row=1, column=1, padx=(10, 4), pady=(3, 0))
        ttk.Button(top, text="立即同步", command=self.sync_now).grid(
            row=1, column=2, padx=4, pady=(3, 0))
        ttk.Button(top, text="统计", command=self.show_stats).grid(
            row=1, column=3, padx=4, pady=(3, 0), sticky="w")
        ttk.Button(top, text="帮助 H", command=self.show_help).grid(
            row=1, column=4, padx=4, pady=(3, 0), sticky="e")
        self._sync_var = tk.StringVar(value="同步: 初始化…")
        self._sync_label = ttk.Label(top, textvariable=self._sync_var, style="Dim.TLabel")
        self._sync_label.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))

        # ---- left panel ----
        left = tk.Frame(root, bg=BG_PANEL, width=248)
        left.grid(row=1, column=0, sticky="nsw", padx=(8, 4), pady=4)
        left.grid_propagate(False)
        left.grid_rowconfigure(3, weight=1)
        left.grid_columnconfigure(0, weight=1)

        tk.Label(left, text="筛选", bg=BG_PANEL, fg=FG_DIM, anchor="w",
                 font=self._font_small).grid(row=0, column=0, sticky="w", padx=6, pady=(6, 0))
        self._filter_labels = [label for label, _ in ds.FILTER_CHOICES]
        self._filter_keys = [key for _, key in ds.FILTER_CHOICES]
        self._filter_var = tk.StringVar(value=self._filter_labels[0])
        combo = ttk.Combobox(left, values=self._filter_labels, textvariable=self._filter_var,
                             state="readonly", width=18)
        combo.grid(row=1, column=0, sticky="ew", padx=6, pady=(1, 3))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_filter())

        self._search_var = tk.StringVar()
        search = ttk.Entry(left, textvariable=self._search_var, width=18)
        search.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
        self._search_entry = search
        search.bind("<KeyRelease>", self._on_search_key)
        search.bind("<Return>", lambda _e: self.apply_filter())
        search.bind("<Escape>", lambda _e: (self._search_var.set(""), self.apply_filter(),
                                            self.canvas.focus_set()))

        list_frame = tk.Frame(left, bg=BG_PANEL)
        list_frame.grid(row=3, column=0, sticky="nsew", padx=(6, 4), pady=(0, 2))
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_frame, bg=BG_LIST, fg=FG, selectbackground="#3a5f8a",
                                  highlightthickness=0, borderwidth=0, activestyle="none",
                                  font=self._font_small, exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        self._list_count_var = tk.StringVar(value="")
        tk.Label(left, textvariable=self._list_count_var, bg=BG_PANEL, fg=FG_DIM,
                 anchor="w", font=self._font_small).grid(row=4, column=0, sticky="ew",
                                                         padx=6, pady=(0, 6))

        # ---- canvas ----
        self.canvas = tk.Canvas(root, bg="#0d0d0f", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.grid(row=1, column=1, sticky="nsew")
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")

        # ---- right panel ----
        right = tk.Frame(root, bg=BG_PANEL, width=250)
        right.grid(row=1, column=2, sticky="nse", padx=(4, 8), pady=4)
        right.grid_propagate(False)
        right.grid_columnconfigure(0, weight=1)
        tk.Label(right, text="类别（数字键 / 点击）", bg=BG_PANEL, fg=FG_DIM,
                 anchor="w", font=self._font_small).grid(row=0, column=0, sticky="ew",
                                                         padx=6, pady=(6, 2))
        palette = tk.Frame(right, bg=BG_PANEL)
        palette.grid(row=1, column=0, sticky="ew", padx=4)
        for col in (0, 1):
            palette.grid_columnconfigure(col, weight=1, uniform="palette")
        for index, class_def in enumerate(self.dataset.taxonomy.classes):
            hotkey = hotkey_for_index(index)
            text = f"{hotkey} {class_def.label}" if hotkey else class_def.label
            button = tk.Button(palette, text=text, font=self._font_small,
                               bg=class_def.color_hex, fg=contrast_text(class_def.color),
                               activebackground=class_def.color_hex,
                               activeforeground=contrast_text(class_def.color),
                               relief="raised", bd=1, padx=1, pady=0,
                               command=lambda cid=class_def.id: self.set_class(cid))
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=1, pady=1)
            self._palette_buttons[class_def.id] = button

        info = tk.Frame(right, bg=BG_PANEL)
        info.grid(row=2, column=0, sticky="ew", padx=6, pady=(8, 2))
        tk.Label(info, text="选中框", bg=BG_PANEL, fg=FG_DIM, anchor="w",
                 font=self._font_small).pack(anchor="w")
        self._selection_var = tk.StringVar(value="未选中框")
        tk.Label(info, textvariable=self._selection_var, bg=BG_PANEL, fg=FG,
                 anchor="w", justify="left", wraplength=232,
                 font=self._font_small).pack(anchor="w")
        self._auto_var = tk.StringVar(value="")
        tk.Label(info, textvariable=self._auto_var, bg=BG_PANEL, fg=COLOR_INFO,
                 anchor="w", justify="left", wraplength=232,
                 font=self._font_small).pack(anchor="w", pady=(2, 0))
        legend = ("虚线 = 自动预标（含置信度）\n实线 = 手工框\n\n" +
                  "空白拖拽画框 · 拖动框内移动\n拖 8 个手柄缩放 · Del 删除\n" +
                  "双击框改类别")
        tk.Label(right, text=legend, bg=BG_PANEL, fg=FG_DIM, anchor="w",
                 justify="left", font=self._font_small).grid(row=3, column=0, sticky="ew",
                                                             padx=6, pady=(8, 6))

        # ---- bottom status ----
        bottom = tk.Frame(root, bg=BG)
        bottom.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 5))
        bottom.grid_columnconfigure(0, weight=1)
        self._status_var = tk.StringVar(value="就绪")
        self._status_label = tk.Label(bottom, textvariable=self._status_var, bg=BG, fg=FG,
                                      anchor="w", font=self._font_small)
        self._status_label.grid(row=0, column=0, sticky="ew")
        self._view_var = tk.StringVar(value="")
        tk.Label(bottom, textvariable=self._view_var, bg=BG, fg=FG_DIM, anchor="e",
                 font=self._font_small).grid(row=0, column=1, sticky="e")
        self._hint_var = tk.StringVar(
            value="A/D 翻页 · Ctrl+S 保存 · 滚轮缩放 · 空格+拖拽平移 · H 帮助")
        tk.Label(bottom, textvariable=self._hint_var, bg=BG, fg=FG_DIM, anchor="w",
                 font=self._font_small).grid(row=1, column=0, columnspan=2, sticky="ew")

    def _default_geometry(self) -> str:
        """Window size that always fits the (possibly DPI scaled) screen."""
        screen_w = 1024
        screen_h = 768
        try:
            screen_w = int(self.root.winfo_screenwidth())
            screen_h = int(self.root.winfo_screenheight())
        except Exception:
            pass
        width = min(1280, max(760, screen_w - 60))
        height = min(900, max(560, screen_h - 90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        return f"{width}x{height}+{x}+{y}"

    # -- events -----------------------------------------------------------
    def _bind_events(self) -> None:
        root = self.root
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<KeyPress>", self._on_key_press)
        root.bind("<KeyRelease>", self._on_key_release)

        canvas = self.canvas
        canvas.bind("<ButtonPress-1>", self._on_left_press)
        canvas.bind("<B1-Motion>", self._on_left_motion)
        canvas.bind("<ButtonRelease-1>", self._on_left_release)
        canvas.bind("<Double-Button-1>", self._on_double_click)
        canvas.bind("<ButtonPress-2>", lambda e: self._start_pan(e))
        canvas.bind("<B2-Motion>", self._on_pan_motion)
        canvas.bind("<ButtonRelease-2>", self._on_pan_release)
        canvas.bind("<ButtonPress-3>", self._on_right_press)
        canvas.bind("<B3-Motion>", self._on_right_motion)
        canvas.bind("<ButtonRelease-3>", self._on_right_release)
        canvas.bind("<Motion>", self._on_hover)
        canvas.bind("<Configure>", self._on_canvas_configure)
        canvas.bind("<Leave>", lambda _e: self.canvas.configure(cursor="crosshair"))
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            canvas.bind(sequence, self._on_wheel, add="+")

    # -- first layout -----------------------------------------------------
    def _first_layout(self) -> None:
        if not self._alive or self._user_interacted:
            return
        self.view = geometry.View.fit(self.img_w, self.img_h, *self._canvas_size())
        self._view_initialised = True
        self._request_render()

    # -- dataset / image loading ------------------------------------------
    def _open_first_image(self) -> None:
        self._refresh_list()
        if not self.filtered:
            self._set_status("当前批次没有图片", color=COLOR_ERR)
            return
        self._load_image(0, save_current=False)

    def _load_image(self, index: int, save_current: bool = True) -> None:
        if not self.filtered:
            return
        if save_current:
            self.save_current(quiet=True)
        index = int(max(0, min(index, len(self.filtered) - 1)))
        entry = self.filtered[index]
        path = self.dataset.image_path(entry)
        try:
            image = Image.open(path)
            image.load()
            self.pil_image = image.convert("RGB")
        except Exception as exc:
            self.pil_image = Image.new("RGB", (entry.width or 640,
                                               entry.height or 480), (30, 30, 30))
            self._set_status(f"图片读取失败: {path} ({exc})", color=COLOR_ERR)
        self.img_w, self.img_h = self.pil_image.size
        entry.width = entry.width or self.img_w
        entry.height = entry.height or self.img_h

        label_path = self.dataset.label_path(entry.id)
        labels = self.dataset.load_labels(entry.id)
        if not label_path.is_file():
            # No label file yet: prefill from the automatic pre-labels.
            auto = self.dataset.load_auto_labels(entry.id)
            labels = [label.copy() for label in auto]
        self.labels = self.dataset.annotate_auto_flags(entry.id, labels)
        self.current = entry
        self.index = index
        self.selected = None
        self.dirty = False
        self.drag = None
        self.view = geometry.View.fit(self.img_w, self.img_h,
                                      self._canvas_size()[0], self._canvas_size()[1])
        self._view_initialised = True
        if entry.id not in self._undo:
            self._undo[entry.id] = UndoStack(list(self.labels), limit=80)
            while len(self._undo) > UNDO_HISTORY_IMAGES:
                self._undo.popitem(last=False)
        self._undo.move_to_end(entry.id)
        self._update_title()
        self._select_listbox(index)
        self._update_palette()
        self._request_render()
        self._update_progress()

    def _canvas_size(self) -> tuple[int, int]:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 20:
            width = 800
        if height < 20:
            height = 600
        return int(width), int(height)

    def _boxes_canvas(self) -> list[geometry.Box]:
        return [self.view.box_to_canvas(label.as_abs(self.img_w, self.img_h))
                for label in self.labels]

    # -- rendering --------------------------------------------------------
    def _request_render(self) -> None:
        if self._render_scheduled or not self._alive:
            return
        self._render_scheduled = True
        self.root.after_idle(self._render_now)

    def _render_now(self) -> None:
        self._render_scheduled = False
        if not self._alive:
            return
        self._draw_image()
        self._draw_boxes()
        self._update_view_label()

    def _draw_image(self) -> None:
        if self.pil_image is None or self._image_item is None:
            return
        canvas_w, canvas_h = self._canvas_size()
        left, top, right, bottom = self.view.visible_source_rect(
            canvas_w, canvas_h, self.img_w, self.img_h)
        if right <= left or bottom <= top:
            self._photo = None
            self.canvas.itemconfigure(self._image_item, image="")
            return
        crop = self.pil_image.crop((left, top, right, bottom))
        target_w = max(1, int(round((right - left) * self.view.zoom)))
        target_h = max(1, int(round((bottom - top) * self.view.zoom)))
        if crop.size != (target_w, target_h):
            crop = crop.resize((target_w, target_h), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(crop)
        self.canvas.itemconfigure(self._image_item, image=self._photo)
        self.canvas.coords(self._image_item,
                           self.view.ox + left * self.view.zoom,
                           self.view.oy + top * self.view.zoom)

    def _draw_boxes(self) -> None:
        self.canvas.delete("box")
        if self.pil_image is None:
            return
        boxes = self._boxes_canvas()
        taxonomy = self.dataset.taxonomy
        for index, label in enumerate(self.labels):
            box = boxes[index]
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                continue
            color = taxonomy.color_hex(label.cls)
            is_selected = index == self.selected
            dash = (4, 3) if label.auto else None
            width = 3 if is_selected else 2
            self.canvas.create_rectangle(*box, outline=color, width=width, dash=dash,
                                         tags=("box",))
            caption = self._box_caption(index, label)
            text_x = box[0] + 2
            text_y = box[1] - 2
            above = True
            if text_y < 16:  # keep the caption inside the canvas
                text_y = box[1] + 2
                above = False
            item = self.canvas.create_text(text_x, text_y, anchor="sw" if above else "nw",
                                           text=caption, fill="#ffffff",
                                           font=self._box_font, tags=("box",))
            bbox = self.canvas.bbox(item)
            if bbox:
                rect = self.canvas.create_rectangle(bbox[0] - 2, bbox[1] - 1,
                                                    bbox[2] + 2, bbox[3] + 1,
                                                    fill=color, outline="", tags=("box",))
                self.canvas.tag_lower(rect, item)
            if is_selected:
                self._draw_handles(box)

        # Preview of a box being drawn.
        drag = self.drag
        if drag is not None and drag.mode == "new" and drag.moved:
            x1, y1 = self._clamp_to_image(drag.start_x, drag.start_y)
            x2, y2 = self._clamp_to_image(drag.cur_x, drag.cur_y)
            preview = geometry.normalize_box(self.view.box_to_canvas((x1, y1, x2, y2)))
            self.canvas.create_rectangle(*preview, outline="#ffffff", width=2,
                                         dash=(3, 2), tags=("box",))

    def _draw_handles(self, box_canvas: geometry.Box) -> None:
        size = geometry.HANDLE_SIZE_PX
        for _name, (x, y) in geometry.handle_positions(box_canvas).items():
            self.canvas.create_rectangle(x - size, y - size, x + size, y + size,
                                         fill="#ffffff", outline="#101010", width=1,
                                         tags=("box",))

    def _box_caption(self, index: int, label: ds.Label) -> str:
        text = f"{index + 1} {self.dataset.taxonomy.display(label.cls)}"
        if label.auto and label.conf is not None:
            text += f" {label.conf:.2f}"
        return text

    def _update_view_label(self) -> None:
        if self.pil_image is None:
            self._view_var.set("")
            return
        self._view_var.set(f"缩放 {self.view.zoom * 100:.0f}% · {self.img_w}×{self.img_h}" +
                           f" · {len(self.labels)} 框")

    # -- status / progress ------------------------------------------------
    def _set_status(self, message: str, color: str = FG, timeout: float = 6.0) -> None:
        if not self._alive:
            return
        self._status_var.set(message)
        self._status_label.configure(foreground=color)
        if self._status_reset_job is not None:
            try:
                self.root.after_cancel(self._status_reset_job)
            except Exception:
                pass
            self._status_reset_job = None
        if timeout > 0:
            self._status_reset_job = self.root.after(
                int(timeout * 1000), lambda: self._status_label.configure(foreground=FG))

    def _update_progress(self) -> None:
        progress = self.dataset.progress()
        self._progress_var.set(
            f"已复核 {progress['reviewed']}/{progress['total']}" +
            f" ({progress['reviewed_pct']:.1f}%) · 已改 {progress['edited']}")
        self._list_count_var.set(f"{len(self.filtered)} 张 / 共 {progress['total']} 张")
        self._update_title()

    def _update_title(self) -> None:
        progress = self.dataset.progress()
        current = self.current.id if self.current else "-"
        self.root.title(f"{APP_TITLE} · {self.dataset.batch} · {current} · " +
                        f"{progress['reviewed']}/{progress['total']}")

    def _flush_warnings(self) -> None:
        if not self._alive:
            return
        if self.dataset.warnings:
            summary = "；".join(self.dataset.warnings[:3])
            more = f"（另有 {len(self.dataset.warnings) - 3} 条）" \
                if len(self.dataset.warnings) > 3 else ""
            self._set_status(f"数据警告: {summary}{more}", color=COLOR_WARN, timeout=12)
            self.dataset.warnings.clear()

    # -- list -------------------------------------------------------------
    def _refresh_list(self) -> None:
        self._suppress_list_event = True
        self.listbox.delete(0, "end")
        for entry in self.filtered:
            state = self.dataset.review_state.get(entry.id)
            self.listbox.insert("end", entry.summary(state.reviewed, state.edited))
            self.listbox.itemconfigure("end", foreground=COLOR_OK if state.reviewed
                                       else (COLOR_WARN if state.edited else FG))
        self._suppress_list_event = False
        self._list_count_var.set(f"{len(self.filtered)} 张")

    def _select_listbox(self, index: int) -> None:
        if not (0 <= index < self.listbox.size()):
            return
        self._suppress_list_event = True
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._suppress_list_event = False

    def _update_list_row(self, entry: ds.ImageEntry) -> None:
        try:
            index = next(i for i, item in enumerate(self.filtered) if item.id == entry.id)
        except StopIteration:
            return
        state = self.dataset.review_state.get(entry.id)
        self.listbox.delete(index)
        self.listbox.insert(index, entry.summary(state.reviewed, state.edited))
        self.listbox.itemconfigure(index, foreground=COLOR_OK if state.reviewed
                                   else (COLOR_WARN if state.edited else FG))
        self._select_listbox(index)

    def _on_list_select(self, _event=None) -> None:
        if self._suppress_list_event:
            return
        selection = self.listbox.curselection()
        if not selection:
            return
        index = int(selection[0])
        if index != self.index:
            self._load_image(index, save_current=True)

    def _on_search_key(self, event) -> None:
        if event.keysym in ("Escape", "Return"):
            return
        if self._search_job is not None:
            try:
                self.root.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.root.after(300, self.apply_filter)

    def apply_filter(self) -> None:
        self._search_job = None
        label = self._filter_var.get()
        kind = self._filter_keys[self._filter_labels.index(label)] \
            if label in self._filter_labels else ds.FILTER_ALL
        query = self._search_var.get().strip()
        self.filtered = self.dataset.filter_images(kind, query)
        self._refresh_list()
        if not self.filtered:
            self._set_status("当前筛选没有图片", color=COLOR_WARN)
            return
        current_id = self.current.id if self.current else None
        target = 0
        if current_id is not None:
            for i, entry in enumerate(self.filtered):
                if entry.id == current_id:
                    target = i
                    break
        if self.current is not None and self.filtered[target].id == self.current.id:
            self.index = target
            self._select_listbox(target)
        else:
            self._load_image(target, save_current=True)
        self._set_status(f"筛选: {label} · {len(self.filtered)} 张", color=COLOR_INFO)

    # -- palette ----------------------------------------------------------
    def set_class(self, class_id: int, apply_to_selection: bool = True) -> None:
        if self.dataset.taxonomy.get(class_id) is None:
            return
        self.current_class = class_id
        label = self.selected
        if apply_to_selection and label is not None and 0 <= label < len(self.labels):
            if self.labels[label].cls != class_id:
                self.labels[label].cls = class_id
                self.labels[label].auto = False
                self.labels[label].conf = None
                self.dirty = True
                self._push_undo()
                self._request_render()
        self._update_palette()
        self._set_status(f"当前类别: {self.dataset.taxonomy.display(class_id)}",
                         color=COLOR_INFO, timeout=2.0)

    def _update_palette(self) -> None:
        for class_id, button in self._palette_buttons.items():
            active = class_id == self.current_class
            button.configure(relief="sunken" if active else "raised",
                             bd=2 if active else 1)
        selected = self.selected
        if selected is not None and 0 <= selected < len(self.labels):
            label = self.labels[selected]
            class_def = self.dataset.taxonomy.get(label.cls)
            name = class_def.label if class_def else f"未知 {label.cls}"
            kind = "自动预标" if label.auto else "手工"
            conf = f" · conf {label.conf:.2f}" if label.auto and label.conf is not None else ""
            self._selection_var.set(
                f"#{selected + 1} {name}（{kind}{conf}）\n" +
                f"cx {label.cx:.4f} cy {label.cy:.4f}\nw {label.w:.4f} h {label.h:.4f}")
            self._auto_var.set("删除: Del · 改类: 数字键 / 双击")
        else:
            self._selection_var.set("未选中框\n左键点框选中，空白处拖拽新建")
            self._auto_var.set(f"新框类别: {self.dataset.taxonomy.display(self.current_class)}")

    # -- undo -------------------------------------------------------------
    def _undo_stack(self) -> UndoStack | None:
        if self.current is None:
            return None
        image_id = self.current.id
        stack = self._undo.get(image_id)
        if stack is None:
            stack = UndoStack(list(self.labels), limit=80)
            self._undo[image_id] = stack
            while len(self._undo) > UNDO_HISTORY_IMAGES:
                self._undo.popitem(last=False)
        self._undo.move_to_end(image_id)
        return stack

    def _reset_undo(self) -> None:
        if self.current is not None:
            self._undo[self.current.id] = UndoStack(list(self.labels), limit=80)
            self._undo.move_to_end(self.current.id)

    def _push_undo(self) -> None:
        # Never seed a stack with the already-modified list: ``_load_image``
        # creates it before the first edit.
        if self.current is None:
            return
        stack = self._undo.get(self.current.id)
        if stack is None:
            stack = UndoStack([], limit=80)
            self._undo[self.current.id] = stack
        stack.push(self.labels)

    def _clamp_selection(self, index: int | None) -> int | None:
        if index is None or not self.labels:
            return None
        return int(max(0, min(index, len(self.labels) - 1)))

    def undo(self) -> None:
        stack = self._undo_stack()
        if stack is None:
            return
        state = stack.undo()
        if state is None:
            self._set_status("没有可撤销的操作", color=COLOR_WARN, timeout=2)
            return
        self.labels = list(state)
        self.selected = self._clamp_selection(self.selected)
        self.dirty = True
        self._request_render()
        self._update_palette()
        self._set_status("已撤销", color=COLOR_INFO, timeout=2)

    def redo(self) -> None:
        stack = self._undo_stack()
        if stack is None:
            return
        state = stack.redo()
        if state is None:
            self._set_status("没有可重做的操作", color=COLOR_WARN, timeout=2)
            return
        self.labels = list(state)
        self.selected = self._clamp_selection(self.selected)
        self.dirty = True
        self._request_render()
        self._update_palette()
        self._set_status("已重做", color=COLOR_INFO, timeout=2)

    # -- mouse ------------------------------------------------------------
    def _canvas_xy(self, event) -> tuple[float, float]:
        return (self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def _clamp_to_image(self, x: float, y: float) -> tuple[float, float]:
        return (geometry.clamp(x, 0.0, self.img_w), geometry.clamp(y, 0.0, self.img_h))

    def _on_left_press(self, event) -> None:
        self.canvas.focus_set()
        if self.img_w <= 0:
            return
        x, y = self._canvas_xy(event)
        if self._space_down:
            self.drag = DragState("pan", start_x=x, start_y=y, cur_x=x, cur_y=y)
            self.canvas.configure(cursor="fleur")
            return
        boxes = self._boxes_canvas()
        index, handle = geometry.hit_test(boxes, x, y, self.selected,
                                          geometry.HANDLE_RADIUS_PX)
        if index is not None and handle is not None:
            self.selected = index
            self.drag = DragState("resize", start_x=x, start_y=y, cur_x=x, cur_y=y,
                                  origin_box=self.labels[index].as_abs(self.img_w, self.img_h),
                                  index=index, handle=handle)
        elif index is not None:
            self.selected = index
            self.drag = DragState("move", start_x=x, start_y=y, cur_x=x, cur_y=y,
                                  origin_box=self.labels[index].as_abs(self.img_w, self.img_h),
                                  index=index)
        else:
            self.selected = None
            self.drag = DragState("new", start_x=x, start_y=y, cur_x=x, cur_y=y)
        self._update_palette()
        self._request_render()

    def _on_left_motion(self, event) -> None:
        drag = self.drag
        if drag is None:
            self._on_hover(event)
            return
        x, y = self._canvas_xy(event)
        drag.cur_x, drag.cur_y = x, y
        drag.moved = drag.moved or abs(x - drag.start_x) > 1 or abs(y - drag.start_y) > 1
        if drag.mode == "pan":
            self._user_interacted = True
            self.view = geometry.clamp_view(self.view.panned(x - drag.start_x,
                                                             y - drag.start_y),
                                            self.img_w, self.img_h, *self._canvas_size())
            drag.start_x, drag.start_y = x, y
        elif drag.mode == "move" and drag.index is not None and drag.origin_box:
            dx = (x - drag.start_x) / self.view.zoom
            dy = (y - drag.start_y) / self.view.zoom
            box = geometry.move_box(drag.origin_box, dx, dy,
                                    bounds=(self.img_w, self.img_h))
            self.labels[drag.index] = ds.Label.from_abs_xyxy(
                self.labels[drag.index].cls, *box, self.img_w, self.img_h)
            self.labels[drag.index].auto = False
            self.labels[drag.index].conf = None
        elif drag.mode == "resize" and drag.index is not None and drag.origin_box:
            dx = (x - drag.start_x) / self.view.zoom
            dy = (y - drag.start_y) / self.view.zoom
            box = geometry.resize_box(drag.origin_box, drag.handle or "se", dx, dy,
                                      geometry.MIN_BOX_PX)
            box = geometry.clamp_box(box, self.img_w, self.img_h, geometry.MIN_BOX_PX)
            self.labels[drag.index] = ds.Label.from_abs_xyxy(
                self.labels[drag.index].cls, *box, self.img_w, self.img_h)
            self.labels[drag.index].auto = False
            self.labels[drag.index].conf = None
        self._request_render()

    def _on_left_release(self, event) -> None:
        drag = self.drag
        self.drag = None
        if drag is None:
            return
        if drag.mode == "pan":
            self.canvas.configure(cursor="crosshair")
            self._request_render()
            return
        if drag.mode == "new":
            x1, y1 = self._clamp_to_image(*self.view.to_image(drag.start_x, drag.start_y))
            x2, y2 = self._clamp_to_image(*self.view.to_image(drag.cur_x, drag.cur_y))
            min_image_px = max(geometry.MIN_BOX_PX, 3.0 / max(self.view.zoom, 0.01))
            box = geometry.box_from_drag(x1, y1, x2, y2, min_image_px)
            if box is not None:
                label = ds.Label.from_abs_xyxy(self.current_class, *box,
                                               self.img_w, self.img_h)
                label.auto = False
                self.labels.append(label)
                self.selected = len(self.labels) - 1
                self.dirty = True
                self._push_undo()
                self._set_status(
                    f"新建 {self.dataset.taxonomy.display(label.cls)} 框 " +
                    f"（{int(box[2] - box[0])}×{int(box[3] - box[1])} px）",
                    color=COLOR_INFO, timeout=3)
        elif drag.mode in ("move", "resize"):
            if drag.moved:
                self.dirty = True
                self._push_undo()
        self._update_palette()
        self._request_render()

    def _start_pan(self, event) -> None:
        x, y = self._canvas_xy(event)
        self.drag = DragState("pan", start_x=x, start_y=y, cur_x=x, cur_y=y, button=2)
        self.canvas.configure(cursor="fleur")

    def _on_pan_motion(self, event) -> None:
        drag = self.drag
        if drag is None or drag.mode != "pan":
            return
        x, y = self._canvas_xy(event)
        self._user_interacted = True
        self.view = geometry.clamp_view(self.view.panned(x - drag.start_x,
                                                         y - drag.start_y),
                                        self.img_w, self.img_h, *self._canvas_size())
        drag.start_x, drag.start_y = x, y
        self._request_render()

    def _on_pan_release(self, _event) -> None:
        if self.drag is not None and self.drag.mode == "pan":
            self.drag = None
            self.canvas.configure(cursor="crosshair")
            self._request_render()

    def _on_right_press(self, event) -> None:
        x, y = self._canvas_xy(event)
        self.drag = DragState("pan", start_x=x, start_y=y, cur_x=x, cur_y=y, button=3)

    def _on_right_motion(self, event) -> None:
        drag = self.drag
        if drag is None or drag.mode != "pan":
            return
        x, y = self._canvas_xy(event)
        if abs(x - drag.start_x) > 1 or abs(y - drag.start_y) > 1:
            drag.moved = True
        self._user_interacted = True
        self.view = geometry.clamp_view(self.view.panned(x - drag.start_x,
                                                         y - drag.start_y),
                                        self.img_w, self.img_h, *self._canvas_size())
        drag.start_x, drag.start_y = x, y
        if drag.moved:
            self.canvas.configure(cursor="fleur")
        self._request_render()

    def _on_right_release(self, event) -> None:
        drag = self.drag
        self.drag = None
        self.canvas.configure(cursor="crosshair")
        if drag is not None and not drag.moved:
            self._show_box_menu(event.x_root, event.y_root)
        self._request_render()

    def _on_hover(self, event) -> None:
        if self.img_w <= 0:
            return
        if self._space_down:
            self.canvas.configure(cursor="fleur")
            return
        x, y = self._canvas_xy(event)
        boxes = self._boxes_canvas()
        index, handle = geometry.hit_test(boxes, x, y, self.selected,
                                          geometry.HANDLE_RADIUS_PX)
        if index is None:
            self.canvas.configure(cursor="crosshair")
        elif handle is not None:
            self.canvas.configure(cursor=geometry.cursor_for_handle(handle))
        else:
            self.canvas.configure(cursor="hand2")

    def _on_wheel(self, event) -> None:
        if self.img_w <= 0:
            return
        if getattr(event, "num", None) == 4:
            delta = 1
        elif getattr(event, "num", None) == 5:
            delta = -1
        else:
            delta = 1 if getattr(event, "delta", 0) > 0 else -1
        x, y = self._canvas_xy(event)
        self.zoom_at(x, y, 1.2 if delta > 0 else 1 / 1.2)

    def zoom_at(self, x: float, y: float, factor: float) -> None:
        self._user_interacted = True
        self.view = geometry.clamp_view(
            self.view.zoomed_at(x, y, factor), self.img_w, self.img_h,
            *self._canvas_size())
        self._request_render()

    def fit_view(self) -> None:
        if self.img_w <= 0:
            return
        self.view = geometry.View.fit(self.img_w, self.img_h, *self._canvas_size())
        self._request_render()

    def _on_canvas_configure(self, _event) -> None:
        if not self._view_initialised:
            if self.img_w > 0:
                self.view = geometry.View.fit(self.img_w, self.img_h,
                                              *self._canvas_size())
                self._view_initialised = True
        else:
            self.view = geometry.clamp_view(self.view, self.img_w, self.img_h,
                                            *self._canvas_size())
        self._request_render()

    # -- box menu ---------------------------------------------------------
    def _on_double_click(self, event) -> None:
        x, y = self._canvas_xy(event)
        index, _handle = geometry.hit_test(self._boxes_canvas(), x, y, self.selected,
                                           geometry.HANDLE_RADIUS_PX)
        if index is None:
            return
        self.selected = index
        self._show_box_menu(event.x_root, event.y_root)

    def _show_box_menu(self, root_x: int, root_y: int) -> None:
        if self.selected is None:
            return
        menu = tk.Menu(self.root, tearoff=0)
        for class_def in self.dataset.taxonomy.classes:
            menu.add_command(label=f"{class_def.id} {class_def.label}",
                             command=lambda cid=class_def.id: self.set_class(cid))
        menu.add_separator()
        menu.add_command(label="删除此框 (Del)", command=self.delete_selected)
        menu.add_command(label="取消选中 (Esc)", command=self.clear_selection)
        try:
            menu.tk_popup(root_x, root_y)
        finally:
            menu.grab_release()

    # -- editing operations -----------------------------------------------
    def delete_selected(self) -> None:
        if self.selected is None or not (0 <= self.selected < len(self.labels)):
            self._set_status("没有选中框", color=COLOR_WARN, timeout=2)
            return
        index = self.selected
        label = self.labels.pop(index)
        self.selected = None
        self.dirty = True
        self._push_undo()
        self._update_palette()
        self._request_render()
        self._set_status(f"已删除框 #{index + 1} " +
                         f"({self.dataset.taxonomy.display(label.cls)})",
                         color=COLOR_INFO, timeout=2)

    def clear_selection(self) -> None:
        self.selected = None
        self._update_palette()
        self._request_render()

    # -- keyboard ---------------------------------------------------------
    def _is_typing(self) -> bool:
        focus = self.root.focus_get()
        return isinstance(focus, (tk.Entry, ttk.Entry, tk.Text))

    def _on_key_press(self, event) -> None:
        keysym = event.keysym
        if keysym == "space":
            self._space_down = True
            self.canvas.configure(cursor="fleur")
            return "break"
        if self._is_typing():
            return
        state = int(getattr(event, "state", 0) or 0)
        ctrl = bool(state & 0x0004)
        shift = bool(state & 0x0001)
        if ctrl:
            if keysym.lower() == "s":
                self.save_current()
                return "break"
            if keysym.lower() == "z":
                if shift:
                    self.redo()
                else:
                    self.undo()
                return "break"
            if keysym.lower() == "y":
                self.redo()
                return "break"
            if keysym.lower() == "o":
                self.pick_dataset()
                return "break"
            return
        if keysym in ("Left", "a", "A"):
            self.goto_index(self.index - 1)
            return "break"
        if keysym in ("Right", "d", "D"):
            self.goto_index(self.index + 1)
            return "break"
        if keysym in ("Delete", "BackSpace"):
            self.delete_selected()
            return "break"
        if keysym == "Escape":
            self.clear_selection()
            return "break"
        if keysym in ("n", "N"):
            self.mark_no_target()
            return "break"
        if keysym in ("r", "R"):
            self.toggle_reviewed()
            return "break"
        if keysym in ("h", "H", "question"):
            self.show_help()
            return "break"
        if keysym in ("f", "F"):
            self.fit_view()
            return "break"
        if keysym in ("plus", "equal", "KP_Add"):
            self.zoom_at(*self._canvas_center(), 1.25)
            return "break"
        if keysym in ("minus", "KP_Subtract"):
            self.zoom_at(*self._canvas_center(), 1 / 1.25)
            return "break"
        if keysym == "Page_Down":
            self.goto_index(self.index + 10)
            return "break"
        if keysym == "Page_Up":
            self.goto_index(self.index - 10)
            return "break"
        index = class_index_from_key(keysym, shift)
        if index is not None and index < len(self.dataset.taxonomy.classes):
            self.set_class(self.dataset.taxonomy.classes[index].id)
            return "break"

    def _on_key_release(self, event) -> None:
        if event.keysym == "space":
            self._space_down = False
            if self.drag is None:
                self.canvas.configure(cursor="crosshair")

    def _canvas_center(self) -> tuple[float, float]:
        width, height = self._canvas_size()
        return width / 2.0, height / 2.0

    # -- navigation -------------------------------------------------------
    def goto_index(self, index: int) -> None:
        if not self.filtered:
            return
        index = int(max(0, min(index, len(self.filtered) - 1)))
        if self.current is not None and index == self.index:
            return
        self._load_image(index, save_current=True)

    def next_image(self) -> None:
        self.goto_index(self.index + 1)

    def prev_image(self) -> None:
        self.goto_index(self.index - 1)

    # -- save / review ----------------------------------------------------
    def save_current(self, quiet: bool = False, reviewed: bool | None = None,
                     force: bool = False) -> bool:
        if self.current is None:
            return False
        if not self.dirty and not force and reviewed is None:
            if not quiet:
                self._set_status("没有需要保存的修改", color=FG_DIM, timeout=2)
            return True
        try:
            result = self.dataset.save_labels(self.current.id, self.labels,
                                             reviewed=reviewed)
        except Exception as exc:
            _write_log(f"save failed: {exc}\n{traceback.format_exc()}")
            self._set_status(f"保存失败: {exc}", color=COLOR_ERR, timeout=10)
            if not quiet:
                messagebox.showerror(APP_TITLE, f"保存失败:\n{exc}", parent=self.root)
            return False
        self.dirty = False
        self._update_list_row(self.current)
        self._update_progress()
        if self.sync is not None:
            self.sync.mark_dirty([self.current.id])
        if not quiet:
            self._set_status(f"已保存 {self.current.id}（{result['count']} 个框" +
                             f"{'，已复核' if result['reviewed'] else ''}）",
                             color=COLOR_OK, timeout=3)
        return True

    def mark_no_target(self) -> None:
        if self.current is None:
            return
        self.labels = []
        self.selected = None
        self.dirty = True
        self._push_undo()
        self._request_render()
        self._update_palette()
        self.save_current(force=True, reviewed=True, quiet=True)
        self._set_status(f"已标记 {self.current.id} 为“无目标”并复核",
                         color=COLOR_OK, timeout=3)

    def toggle_reviewed(self) -> None:
        if self.current is None:
            return
        state = self.dataset.review_state.get(self.current.id)
        new_value = not state.reviewed
        self.save_current(force=True, reviewed=new_value, quiet=True)
        self._set_status(f"{self.current.id} → {'已复核' if new_value else '未复核'}",
                         color=COLOR_OK if new_value else COLOR_WARN, timeout=3)

    # -- dialogs ----------------------------------------------------------
    def show_help(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("帮助 / 快捷键")
        window.configure(bg=BG)
        window.geometry("760x620")
        text = tk.Text(window, wrap="word", bg=BG_LIST, fg=FG, insertbackground=FG,
                       font=self._font_normal, padx=12, pady=10, relief="flat")
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Button(window, text="关闭 (Esc)", command=window.destroy).pack(pady=(0, 8))
        window.bind("<Escape>", lambda _e: window.destroy())

    def show_stats(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("统计")
        window.configure(bg=BG)
        window.geometry("560x600")
        text = tk.Text(window, wrap="word", bg=BG_LIST, fg=FG, font=self._font_small,
                       padx=12, pady=10, relief="flat")
        text.pack(fill="both", expand=True, padx=8, pady=8)
        counts = self.dataset.class_counts()
        auto_counts = self.dataset.auto_class_counts()
        progress = self.dataset.progress()
        lines = [
            f"批次: {self.dataset.batch}",
            f"目录: {self.dataset.root}",
            f"图片: {progress['total']}  已复核: {progress['reviewed']}" +
            f" ({progress['reviewed_pct']:.1f}%)  未复核: {progress['unreviewed']}",
            f"手工修改过: {progress['edited']}",
            f"split: {self.dataset.split_counts}",
            f"priority 分布: {self.dataset.priority_counts()}",
            "",
            f"{'id':>3}  {'类别':<16} {'当前框':>6} {'自动预标':>7}  中文",
            "-" * 56,
        ]
        for class_def in self.dataset.taxonomy.classes:
            current = counts.get(str(class_def.id), 0)
            auto = auto_counts.get(str(class_def.id), 0)
            lines.append(f"{class_def.id:>3}  {class_def.name:<16} {current:>6} " +
                         f"{auto:>7}  {class_def.zh}")
        total_labels = sum(counts.get(str(c.id), 0) for c in self.dataset.taxonomy.classes)
        total_auto = sum(auto_counts.get(str(c.id), 0) for c in self.dataset.taxonomy.classes)
        lines += ["-" * 56,
                  f"当前标签框总数: {total_labels}（自动预标: {total_auto}）"]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=(0, 8))

    def pick_dataset(self) -> None:
        directory = filedialog.askdirectory(title="选择数据集批次目录",
                                            initialdir=str(self.dataset.root))
        if not directory:
            return
        self._set_status(f"请重启标注器以加载: {directory}", color=COLOR_WARN)

    # -- sync -------------------------------------------------------------
    def _init_sync(self, no_sync: bool = False) -> None:
        self.sync: sync_mod.SyncWorker | None = None
        config, config_path = sync_mod.SyncConfig.find(APP_DIR)
        self.sync_config_path = config_path
        if no_sync:
            config = sync_mod.SyncConfig()
            _write_log("同步已通过 --no-sync 禁用")
        self.sync_config = config
        worker = sync_mod.SyncWorker(
            config=config,
            dataset_root=self.dataset.root,
            batch=self.dataset.batch,
            counts_provider=self.dataset.manifest_counts,
            logger=self._make_sync_logger(),
            app_version=APP_VERSION,
        )
        if no_sync:
            worker.disable("已禁用（--no-sync）")
        worker.start()
        self.sync = worker
        if not worker.enabled:
            _write_log(f"同步禁用: {worker.disabled_reason}")
        elif config_path is None:
            _write_log("警告: 未找到 sync.json，仅本地保存")
        self._sync_var.set(worker.status().label())

    @staticmethod
    def _make_sync_logger():
        def _log(message: str) -> None:
            _write_log(f"[sync] {message}", filename="sync.log")
        return _log

    def sync_now(self) -> None:
        if self.sync is None:
            return
        status = self.sync.status()
        if status.state == "disabled":
            messagebox.showinfo(APP_TITLE,
                                f"同步不可用：{status.message}\n\n" +
                                "请检查程序目录下的 sync.json 与 paramiko 安装。",
                                parent=self.root)
            return
        self.sync.request_sync()
        self._set_status("已请求同步…", color=COLOR_INFO, timeout=2)

    def _poll_sync(self) -> None:
        if not self._alive:
            return
        if self.sync is not None:
            status = self.sync.status()
            self._sync_var.set(status.label())
            color = {
                "disabled": FG_DIM, "idle": COLOR_INFO, "pending": COLOR_WARN,
                "syncing": COLOR_INFO, "ok": COLOR_OK, "error": COLOR_ERR,
            }.get(status.state, FG)
            self._sync_label.configure(foreground=color)
        self.root.after(500, self._poll_sync)

    # -- timers / shutdown ------------------------------------------------
    def _autosave_tick(self) -> None:
        if not self._alive:
            return
        if self.dirty:
            self.save_current(quiet=True)
            self._set_status("已自动保存", color=COLOR_OK, timeout=2)
        self.root.after(20000, self._autosave_tick)

    def on_close(self) -> None:
        if not self._alive:
            return
        try:
            if self.dirty:
                self.save_current(quiet=True)
        except Exception:
            _write_log(f"close save failed\n{traceback.format_exc()}")
        self._alive = False
        if self.sync is not None:
            try:
                self._sync_var.set("正在上传未同步的标签…")
                self.root.update_idletasks()
                self.sync.shutdown(flush=True, timeout=8.0)
            except Exception:
                _write_log(f"sync shutdown failed\n{traceback.format_exc()}")
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------
def _grab_window_win32(root: tk.Tk) -> Image.Image | None:
    """Capture a Tk window on Windows with ``PrintWindow``.

    ``ImageGrab`` grabs the *screen*, which both requires a visible desktop
    and would include whatever window happens to be on top.  ``PrintWindow``
    asks the window to render itself into a memory DC, so it works for
    obscured windows and never captures other applications.  Returns ``None``
    when the platform is not Windows or when nothing was rendered (e.g. an
    SSH session 0 window, which is never composited).
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                        ("bmiColors", wintypes.DWORD * 3)]

        hwnd = int(root.wm_frame(), 16)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 50 or height < 50:
            return None
        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        previous = gdi32.SelectObject(mem_dc, bitmap)
        try:
            rendered = user32.PrintWindow(hwnd, mem_dc, 2)  # PW_RENDERFULLCONTENT
            if not rendered:  # older Windows: plain BitBlt of the window DC
                gdi32.BitBlt(mem_dc, 0, 0, width, height, window_dc, 0, 0,
                             0x00CC0020)
            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # top-down rows
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0  # BI_RGB
            info = BITMAPINFO()
            info.bmiHeader = header
            buffer = ctypes.create_string_buffer(width * height * 4)
            copied = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer,
                                     ctypes.byref(info), 0)
        finally:
            gdi32.SelectObject(mem_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, window_dc)
        if not copied:
            return None
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX",
                                 0, 1)
        if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
            return None  # nothing was rendered
        return image.copy()
    except Exception:
        return None


def capture_window(root: tk.Tk, path: str | os.PathLike) -> str:
    """Grab the labeler window to *path* (PNG); returns the path written.

    Order of attempts: Win32 ``PrintWindow`` (Windows, captures this window
    only) -> ``ImageGrab`` (X11 with ``xdisplay``, then bbox, then full
    screen).  Callers must handle ``RuntimeError`` by falling back to
    :func:`compose_preview`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root.update_idletasks()
    root.update()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    width, height = root.winfo_width(), root.winfo_height()
    grabbed: Image.Image | None = _grab_window_win32(root)
    errors: list[str] = []
    if grabbed is None and sys.platform.startswith("linux"):
        try:
            grabbed = ImageGrab.grab(bbox=(x, y, x + width, y + height),
                                     xdisplay=os.environ.get("DISPLAY"))
        except Exception as exc:  # fall back to the full screen
            errors.append(f"xdisplay: {exc}")
    if grabbed is None:
        try:
            grabbed = ImageGrab.grab(bbox=(x, y, x + width, y + height))
        except Exception as exc:
            errors.append(f"bbox: {exc}")
            try:
                grabbed = ImageGrab.grab()
            except Exception as exc2:
                errors.append(f"full: {exc2}")
    if grabbed is None:
        raise RuntimeError("截图失败: " + " | ".join(errors))
    grabbed.save(path)
    return str(path)


def compose_preview(app: LabelerApp, path: str | os.PathLike) -> str:
    """Fallback "screenshot": render image + boxes with Pillow.

    Used when the platform refuses a screen grab; it is *not* a screenshot of
    the Tk window, so callers must say so in their report.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if app.pil_image is None:
        raise RuntimeError("没有可渲染的图片")
    base = app.pil_image.copy()
    draw = ImageDraw.Draw(base)
    for index, label in enumerate(app.labels):
        box = label.as_abs(app.img_w, app.img_h)
        color = app.dataset.taxonomy.color(label.cls)
        draw.rectangle(box, outline=color, width=3)
        draw.text((box[0] + 4, max(0, box[1] - 12)),
                  f"{index + 1} {app.dataset.taxonomy.display(label.cls)}",
                  fill=(255, 255, 255))
    base.save(path)
    return str(path)


# ---------------------------------------------------------------------------
# Self test (headless smoke test used on Linux/xvfb and Windows)
# ---------------------------------------------------------------------------
def run_selftest(dataset_dir: str | None = None,
                 screenshot: str | None = None,
                 keep_open: bool = False) -> int:
    """Build the UI, exercise the main interactions, print SELFTEST OK/FAILED."""
    tmp_dir: str | None = None
    steps: list[str] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        marker = "OK" if ok else "FAIL"
        suffix = f" - {detail}" if detail else ""
        print_safe(f"[selftest] {marker}  {name}{suffix}")
        steps.append(name)
        if not ok:
            raise AssertionError(f"{name} {detail}".strip())

    root = tk.Tk()
    try:
        root.geometry("1000x720+0+0")
        resolved = ds.resolve_dataset_dir(dataset_dir, APP_DIR, DEFAULT_DATASET)
        if resolved is None or not (Path(resolved) / "dataset.json").is_file():
            tmp_dir = tempfile.mkdtemp(prefix="labeler_selftest_")
            resolved = demo.make_demo_batch(Path(tmp_dir) / "batch_selftest", n=5)
            print_safe(f"[selftest] 未找到数据集，已生成演示批次: {resolved}")
        app = LabelerApp(root, resolved, no_sync=True, selftest=True)
        root.update()
        root.update_idletasks()
        app._first_layout()  # deterministic instead of racing the 150 ms timer
        root.update()

        # The selftest really saves / clears the first image, so snapshot it and
        # restore afterwards: running this on a real batch must not modify it.
        first_id = app.current.id
        first_label_path = app.dataset.label_path(first_id)
        first_label_original = first_label_path.read_bytes() \
            if first_label_path.is_file() else None
        review_state_path = app.dataset.review_state_path()
        review_state_original = review_state_path.read_bytes() \
            if review_state_path.is_file() else None

        step("数据集加载", len(app.dataset.images) > 0,
             f"{len(app.dataset.images)} 张，{len(app.dataset.taxonomy)} 类")
        step("首图加载", app.current is not None and app.pil_image is not None,
             f"{app.current.id if app.current else '-'} {app.img_w}x{app.img_h}")
        app._render_now()
        step("首帧渲染", app._photo is not None, "PhotoImage 已生成")

        zoom_before = app.view.zoom
        app.zoom_at(*app._canvas_center(), 1.6)
        root.update()
        step("缩放", app.view.zoom > zoom_before * 1.2,
             f"{zoom_before:.3f} -> {app.view.zoom:.3f}")
        app.fit_view()
        root.update()

        # Draw a box using the real event handlers.
        before = len(app.labels)
        app._on_left_press(SimpleNamespace(x=180, y=160))
        app._on_left_motion(SimpleNamespace(x=320, y=280))
        app._on_left_release(SimpleNamespace(x=320, y=280))
        root.update()
        step("画框", len(app.labels) == before + 1 and app.selected is not None,
             f"{before} -> {len(app.labels)} 框")
        new_box = app.labels[app.selected].as_abs(app.img_w, app.img_h)
        step("框坐标有效", new_box[2] > new_box[0] and new_box[3] > new_box[1],
             f"{tuple(round(v, 1) for v in new_box)}")

        app.undo()
        root.update()
        step("撤销", len(app.labels) == before, f"{len(app.labels)} 框")
        app.redo()
        root.update()
        step("重做", len(app.labels) == before + 1, f"{len(app.labels)} 框")

        app.selected = len(app.labels) - 1
        other_class = app.dataset.taxonomy.classes[-1].id
        app.set_class(other_class)
        root.update()
        step("改类别", app.labels[app.selected].cls == other_class,
             f"cls={app.labels[app.selected].cls}")

        # Resize via a handle of the selected box.
        app.selected = 0
        app._request_render()
        root.update()
        box_canvas = app._boxes_canvas()[0]
        handle_x, handle_y = geometry.handle_positions(box_canvas)["se"]
        original = app.labels[0].as_abs(app.img_w, app.img_h)
        app._on_left_press(SimpleNamespace(x=handle_x, y=handle_y))
        app._on_left_motion(SimpleNamespace(x=handle_x + 25, y=handle_y + 18))
        app._on_left_release(SimpleNamespace(x=handle_x + 25, y=handle_y + 18))
        root.update()
        resized = app.labels[0].as_abs(app.img_w, app.img_h)
        step("手柄缩放", resized[2] > original[2] and resized[3] > original[3],
             f"{tuple(round(v, 1) for v in original)} -> " +
             f"{tuple(round(v, 1) for v in resized)}")

        saved = app.save_current(force=True)
        root.update()
        label_file = app.dataset.label_path(app.current.id)
        step("保存标签", saved and label_file.is_file(),
             f"{app.dataset.label_path(app.current.id).relative_to(app.dataset.root)}")

        app.mark_no_target()
        root.update()
        step("N 无目标", len(app.labels) == 0
             and app.dataset.review_state.get(app.current.id).reviewed,
             "空 labels = 负样本")
        step("负样本落盘", app.dataset.label_path(app.current.id).read_text(
            encoding="utf-8").strip() == "")

        first_id_saved = app.current.id
        app.next_image()
        root.update()
        step("翻页", app.current is not None and app.current.id != first_id_saved,
             f"{first_id_saved} -> {app.current.id}")

        app.apply_filter()
        root.update()
        step("筛选", len(app.filtered) == len(app.dataset.images),
             f"{len(app.filtered)} 张")
        step("进度统计", app.dataset.progress()["total"] == len(app.dataset.images),
             str(app.dataset.progress()))

        # Restore exactly what the selftest touched: a selftest must be a
        # side-effect free check, even when pointed at a real batch.
        if first_label_original is None:
            if first_label_path.is_file():
                first_label_path.unlink()
        else:
            first_label_path.write_bytes(first_label_original)
        if review_state_original is None:
            if review_state_path.is_file():
                review_state_path.unlink()
        else:
            review_state_path.write_bytes(review_state_original)
        print_safe("[selftest] 已恢复首图原始标签与 review_state（不修改数据集）")

        if screenshot:
            root.update()
            try:
                written = capture_window(root, screenshot)
                print_safe(f"[selftest] 截图已保存: {written}")
            except Exception as exc:
                print_safe(f"[selftest] 截图失败({exc})，改用离线合成预览")
                written = compose_preview(app, screenshot)
                print_safe(f"[selftest] 合成预览已保存: {written}")
            step("截图", Path(screenshot).is_file(), str(screenshot))

        app.on_close()
        print_safe(f"[selftest] SELFTEST OK - {len(steps)} 项检查通过，批次 {resolved}")
        return 0
    except Exception as exc:
        print_safe(f"[selftest] SELFTEST FAILED: {exc}")
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    finally:
        if tmp_dir is not None and not keep_open:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{APP_TITLE} {APP_VERSION} — 轻量 YOLO 标注器")
    parser.add_argument("--dataset", help="数据集批次目录（含 dataset.json）")
    parser.add_argument("--selftest", action="store_true",
                        help="无头自检：建窗、渲染、缩放、画框、撤销、保存，然后退出")
    parser.add_argument("--screenshot", help="把窗口截图保存为 PNG 后退出")
    parser.add_argument("--no-sync", action="store_true", help="禁用后台同步（仅本地保存）")
    parser.add_argument("--pull", metavar="REMOTE_ZIP",
                        help="从同步目录下载 <REMOTE_ZIP> 并解压（实验性）")
    parser.add_argument("--sync-check", action="store_true",
                        help="只做一次真实上传测试（首图标签 + review_state + manifest）后退出")
    parser.add_argument("--dest", help="--pull 的解压目标目录")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.pull:
        config, path = sync_mod.SyncConfig.find(APP_DIR)
        if path is None:
            print_safe("未找到 sync.json，无法拉取批次")
            return 2
        try:
            extracted = sync_mod.pull_batch(config, args.pull,
                                            args.dest or str(APP_DIR.parent))
        except Exception as exc:
            print_safe(f"拉取失败: {exc}")
            return 1
        print_safe(f"已解压到: {extracted}")
        return 0

    if args.sync_check:
        resolved = ds.resolve_dataset_dir(args.dataset, APP_DIR, DEFAULT_DATASET)
        if resolved is None or not (Path(resolved) / "dataset.json").is_file():
            print_safe(f"未找到数据集: {resolved}")
            return 2
        try:
            dataset = ds.Dataset.open(resolved)
        except ds.DatasetError as exc:
            print_safe(f"数据集打开失败: {exc}")
            return 2
        config, config_path = sync_mod.SyncConfig.find(APP_DIR)
        print_safe(f"sync.json: {config_path}")
        print_safe(f"remote_dir: {config.resolve_remote_dir(dataset.batch)}")
        worker = sync_mod.SyncWorker(
            config, dataset.root, dataset.batch,
            counts_provider=dataset.manifest_counts,
            logger=lambda message: print(f"[sync] {message}"))
        if not worker.enabled:
            print_safe(f"同步不可用: {worker.disabled_reason}")
            return 2
        first_id = dataset.images[0].id
        worker.mark_dirty([first_id])
        ok = worker.shutdown(flush=True, timeout=20.0)
        print(f"status: {worker.status().label()}")
        print("SYNC CHECK OK" if ok else "SYNC CHECK FAILED")
        return 0 if ok else 1

    if args.selftest or args.screenshot:
        return run_selftest(args.dataset, args.screenshot)

    resolved = ds.resolve_dataset_dir(args.dataset, APP_DIR, DEFAULT_DATASET)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print_safe(f"无法创建 Tk 窗口: {exc}")
        return 3
    try:
        if resolved is None or not (Path(resolved) / "dataset.json").is_file():
            message = ("未找到数据集。\n\n" +
                       "请用 --dataset 指定批次目录，或在程序目录放一个 " +
                       "dataset_dir.txt（内容是批次目录路径）。\n\n" +
                       f"默认尝试: {DEFAULT_DATASET}")
            _write_log(f"dataset not found: {resolved}")
            messagebox.showerror(APP_TITLE, message)
            root.destroy()
            return 2
        LabelerApp(root, resolved, no_sync=args.no_sync)
    except ds.DatasetError as exc:
        _write_log(f"dataset error: {exc}\n{traceback.format_exc()}")
        messagebox.showerror(APP_TITLE, str(exc))
        root.destroy()
        return 2
    except Exception as exc:
        _write_log(f"startup failed: {exc}\n{traceback.format_exc()}")
        messagebox.showerror(APP_TITLE, f"启动失败:\n{exc}\n\n详见 labeler.log")
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
