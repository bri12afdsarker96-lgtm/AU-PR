from __future__ import annotations

import os
import threading
import traceback
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .auto_cut import DEFAULT_KEYWORDS, STRATEGIES, AutoCutSettings, recommend_auto_cut_settings, run_strategy_auto_edit
from .batch_archive import build_batch_archive
from .capcut_export import export_capcut_draft_package
from .capability_check import format_capability_report, write_capability_report
from .component_download import download_component
from .dub_sync import dub_assemble
from .editing_engine import run_auto_edit
from .episode_pipeline import episode_rework
from .inventory import ASSET_TARGETS, count_matching_files_in_subdirs, import_assets, import_material_folder, write_inventory
from .models import (
    DIRECTORY_SPECS,
    DIR_A,
    DIR_ASSEMBLED,
    DIR_AUTO_EDIT_INPUT,
    DIR_BATCH_ARCHIVE,
    DIR_CONFIG,
    DIR_DUB_ASSEMBLED,
    DIR_EPISODE_REWORK,
    DIR_LOGS,
    DIR_READY,
    DIR_REVIEW,
    DIR_SCENE_DETECT,
    VIDEO_EXTENSIONS,
)
from .plugin_adapters import download_video, install_whisper_runtime
from .plugins import plugin_statuses, vendor_root, version_root, write_vendor_manifest
from .production_line import produce
from .project import create_project, iter_media, load_config, save_config
from .model_registry import model_library_statuses, whisper_models_dir
from .publish_assistant import (
    AssistantFinishSummary,
    AssistantSession,
)
from .production_state import (
    latest_auto_edit_selected_dir,
    workflow_counts,
)
from .scene_detect import detect_scenes
from .transcription import whisper_environment
from .ui import theme
from .ui import widgets as ui
from .visual_package import DEFAULT_TEMPLATE
from .video_dedup import select_high_similarity_sources
from .video_engine import render_high_similarity_replacements, render_project


APP_NAME = "水星剪辑"
APP_TAGLINE = "自动剪辑 · 二创包装 · 发布回流"
OPERATION_MANUAL_NAME = "运营操作手册_v2026.07.09.1.md"

SIDEBAR_BG = theme.PANEL
SIDEBAR_ACTIVE = theme.HOVER
MAIN_BG = theme.BG
CARD_BG = theme.PANEL
SOFT_BG = theme.HOVER
BORDER = theme.BORDER
TEXT = theme.TEXT
MUTED = theme.MUTED
ACCENT = theme.PRIMARY
ACCENT_SOFT = theme.HOVER
OK = theme.SUCCESS
WARN = theme.WARNING
ERROR = theme.DANGER
FONT = theme.FONT


class TaskCancelled(Exception):
    """用户点了「停止任务」：后台任务在检查点抛出本异常，走取消收尾而非报错弹窗。"""


class WorkbenchApp(tk.Tk):
    CORE_NAV_ITEMS = [
        ("material", "📁 项目与素材", "建项目、入库状态、目录表"),
        ("dub", "🎙 配音对齐成片", "配音包对齐分镜·选画面尺寸·出片"),
        ("remix", "🎬 矩阵生产", "自动剪辑·账号数·去重·一键生产"),
    ]
    ADVANCED_NAV_ITEMS = [
        ("plugins", "🧰 工具箱自检", "所有下载·依赖·组件·路径·自检"),
    ]

    PAGE_META = {
        "material": ("项目与素材总览", "新建项目、查看入库状态与完整目录表；各类素材在对应模块里导入。"),
        "project": ("项目与素材总览", "新建项目、查看入库状态与完整目录表；各类素材在对应模块里导入。"),
        "dub": ("配音对齐成片", "配音包与分镜视频按句对齐，选画面尺寸合成成片，可导出剪映草稿二次精修。"),
        "remix": ("矩阵生产", "自动剪辑分镜 → 参数/预设 → 一键生产 → 产物/复盘。"),
        "plugins": ("高级工具箱", "集中管理能力组件、素材下载、运行路径和环境状态。"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1400x980")
        self.minsize(1280, 900)
        self.resizable(True, True)
        self.configure(bg=MAIN_BG)
        self._set_window_icon()

        self.current_page = "material"
        self.nav_buttons: dict[str, tk.Button] = {}
        self.advanced_nav_frame: tk.Frame | None = None
        self.advanced_toggle_button: tk.Button | None = None
        self.advanced_expanded = False
        self.edit_advanced_expanded = False
        self.reference_plugins_expanded = False
        self.architecture_plugins_expanded = False
        self.support_expanded = False
        self.remix_page_initialized = False
        self._dub_paths_project_root = ""
        self._intermediate_video_paths: list[Path] = []
        self.publish_assistant_session: AssistantSession | None = None
        self.publish_assistant_summary: AssistantFinishSummary | None = None
        self.publish_assistant_caption_text: tk.Text | None = None
        self._assistant_save_after_id: str | None = None
        self.model_download_state: dict[str, tuple[str, str]] = {}
        self.model_status_widgets: dict[str, tk.Label] = {}
        self.brand_image: tk.PhotoImage | None = None
        self.support_qr_image: tk.PhotoImage | None = None

        self.project_var = tk.StringVar()
        self.root_var = tk.StringVar(value=str(Path.home() / "短剧项目"))
        self.name_var = tk.StringVar(value="新短剧项目")
        self.drama_var = tk.StringVar(value="")
        self.mode_var = tk.StringVar(value="vertical")
        self.copies_var = tk.IntVar(value=3)
        self.account_count_var = tk.IntVar(value=3)
        self.dedup_level_var = tk.StringVar(value="标准")
        self.workflow_full_var = tk.BooleanVar(value=False)
        self.edit_target_var = tk.DoubleVar(value=60.0)
        self.edit_clip_var = tk.DoubleVar(value=4.0)
        self.edit_script_var = tk.StringVar()
        self.auto_strategy_var = tk.StringVar(value="爆点融合")
        self.auto_keywords_var = tk.StringVar(value=DEFAULT_KEYWORDS)
        # 赛道画风与画面前置参数（自动剪辑时套用剪辑库能力）+ 我的预设
        self.edit_genre_var = tk.StringVar(value="")
        self.aspect_ratio_var = tk.StringVar(value="9:16 竖屏")
        self.audio_match_var = tk.StringVar(value="裁剪多余画面")
        self.preset_var = tk.StringVar(value="")
        # opt-in：强制每份成片首帧互不相同（默认关，不动冻结去重基线）
        self.force_distinct_intro_var = tk.BooleanVar(value=False)
        # opt-in：断点续跑（默认关；开启后同一产线重跑跳过已完成的份）
        self.resume_var = tk.BooleanVar(value=False)
        # 后台任务暂停/停止（协作式取消；停止会终止在跑子进程）
        self._bg_stop = threading.Event()
        self._bg_pause = threading.Event()
        # 工作流计数缓存：refresh_status 不再每次全盘扫描素材目录
        self._counts_cache: dict = {}    # {项目根: (时间戳, counts)}
        self._counts_refreshing = False
        self.tl_filter_var = tk.StringVar(value="原片")
        self.tl_effect_var = tk.StringVar(value="无")
        self.tl_transition_var = tk.StringVar(value="")
        self.tl_text_var = tk.StringVar(value="")
        self.tl_duration_var = tk.DoubleVar(value=3.0)
        # 剪辑工作台导出参数
        self.export_resolution_var = tk.StringVar(value="1080P")
        self.export_format_var = tk.StringVar(value="MP4")
        self.export_codec_var = tk.StringVar(value="H.264")
        self.export_fps_var = tk.IntVar(value=30)
        self.library_filter_var = tk.StringVar(value="全部")
        self.description_query_var = tk.StringVar(value="")
        self.silence_db_var = tk.DoubleVar(value=-35.0)
        self.min_silence_var = tk.DoubleVar(value=0.45)
        self.scene_threshold_var = tk.DoubleVar(value=0.35)
        self.render_source_var = tk.StringVar(value="原始视频")
        self.ffmpeg_var = tk.StringVar()
        self.ffprobe_var = tk.StringVar()
        self.local_editor_exe_var = tk.StringVar()
        self.publish_exe_var = tk.StringVar()
        self.download_url_var = tk.StringVar()
        self.download_target_var = tk.StringVar(value="原始视频")
        self.package_template_var = tk.StringVar(value=DEFAULT_TEMPLATE)
        self.dub_audio_dir_var = tk.StringVar()
        self.dub_video_dir_var = tk.StringVar()
        self.dub_name_var = tk.StringVar(value="")
        self.dub_subtitle_var = tk.BooleanVar(value=False)
        self.dub_auto_produce_var = tk.BooleanVar(value=True)
        self.dub_aspect_var = tk.StringVar(value="9:16 竖屏")   # 配音成片画面尺寸
        self.episode_file_var = tk.StringVar()
        self.episode_versions_var = tk.IntVar(value=3)
        self.episode_audio_mode_var = tk.StringVar(value="keep_original")
        self.episode_replacement_audio_var = tk.StringVar()
        self.publish_assistant_platform_var = tk.StringVar(value="")
        self.publish_assistant_remember_var = tk.BooleanVar(value=False)
        # 各素材角色最近选择/导入的路径，在素材入库页回显。
        self.material_path_vars: dict[str, tk.StringVar] = {}

        self._build_style()
        self._build_shell()
        self.show_page("material")

    def _set_window_icon(self) -> None:
        icon_path = version_root() / "assets" / "brand" / "shuixing_icon.ico"
        if not icon_path.exists():
            return
        try:
            self.iconbitmap(str(icon_path))
        except tk.TclError:
            pass

    def _load_brand_image(self) -> tk.PhotoImage | None:
        image_path = version_root() / "assets" / "brand" / "shuixing_icon.png"
        if not image_path.exists():
            return None
        try:
            image = tk.PhotoImage(file=str(image_path))
            return image.subsample(max(1, image.width() // 48), max(1, image.height() // 48))
        except tk.TclError:
            return None

    def _load_support_qr_image(self) -> tk.PhotoImage | None:
        image_path = version_root() / "assets" / "brand" / "technical_support_qr.png"
        if not image_path.exists():
            return None
        try:
            image = tk.PhotoImage(file=str(image_path))
            factor = max(1, (max(image.width(), image.height()) + 95) // 96)
            return image.subsample(factor, factor)
        except tk.TclError:
            return None

    def _build_style(self) -> None:
        theme.apply_theme(self)

    def _build_shell(self) -> None:
        shell = tk.Frame(self, bg=MAIN_BG)
        shell.pack(fill=tk.BOTH, expand=True)

        sidebar = tk.Frame(shell, bg=SIDEBAR_BG, width=284, highlightbackground=BORDER, highlightthickness=1)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=SIDEBAR_BG)
        brand.pack(fill=tk.X, padx=18, pady=(20, 18))
        self.brand_image = self._load_brand_image()
        if self.brand_image:
            logo = tk.Label(brand, image=self.brand_image, bg=SIDEBAR_BG, width=50, height=50)
        else:
            logo = tk.Label(brand, text="水", bg=ACCENT, fg="white", font=(FONT, 20, "bold"), width=2, height=1)
        logo.pack(side=tk.LEFT)
        brand_text = tk.Frame(brand, bg=SIDEBAR_BG)
        brand_text.pack(side=tk.LEFT, padx=12)
        tk.Label(brand_text, text=APP_NAME, bg=SIDEBAR_BG, fg=TEXT, font=(FONT, 16, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="矩阵产线驾驶舱", bg=SIDEBAR_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")

        self.nav_container = tk.Frame(sidebar, bg=SIDEBAR_BG)
        self.nav_container.pack(fill=tk.X, padx=14)
        self._rebuild_nav()

        bottom = tk.Frame(sidebar, bg=SIDEBAR_BG)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=18, pady=18)
        tk.Label(bottom, text="当前项目", bg=SIDEBAR_BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        self.sidebar_project = tk.Label(
            bottom,
            text="未选择项目",
            bg=SIDEBAR_BG,
            fg=TEXT,
            font=(FONT, 9),
            wraplength=210,
            justify=tk.LEFT,
        )
        self.sidebar_project.pack(anchor="w", pady=(4, 0))
        # 激活码有效期倒计时（左下角）
        self.license_countdown_label = tk.Label(
            bottom, text="激活状态：读取中…", bg=SIDEBAR_BG, fg=MUTED,
            font=(FONT, 9, "bold"), wraplength=210, justify=tk.LEFT,
        )
        self.license_countdown_label.pack(anchor="w", pady=(10, 0))
        self._refresh_license_countdown()

        self.support = tk.Frame(sidebar, bg=SIDEBAR_BG)
        self.support.pack(side=tk.BOTTOM, fill=tk.X, padx=28, pady=(0, 24))
        self._button(self.support, "帮助与支持", self.toggle_support_panel, kind="ghost").pack(anchor="w")
        self.support_detail = tk.Frame(self.support, bg=SIDEBAR_BG)
        self.support_qr_image = self._load_support_qr_image()

        workspace = tk.Frame(shell, bg=MAIN_BG)
        workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._workspace = workspace  # 时间线页固定布局需要在滚动区之外挂载

        header = tk.Frame(workspace, bg=MAIN_BG)
        header.pack(fill=tk.X, padx=28, pady=(24, 10))
        title_area = tk.Frame(header, bg=MAIN_BG)
        title_area.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.page_title_label = tk.Label(title_area, text="", bg=MAIN_BG, fg=TEXT, font=(FONT, 16, "bold"))
        self.page_title_label.pack(anchor="w")
        self.page_subtitle_label = tk.Label(title_area, text="", bg=MAIN_BG, fg=MUTED, font=(FONT, 10))
        self.page_subtitle_label.pack(anchor="w", pady=(3, 0))

        self._button(header, "帮助", self.open_operation_manual, kind="secondary").pack(side=tk.RIGHT, padx=(8, 0))
        self._button(header, "刷新", self.refresh_status, kind="secondary").pack(side=tk.RIGHT, padx=(8, 0))
        self.mode_bar = tk.Frame(header, bg=MAIN_BG)
        self.mode_bar.pack(side=tk.RIGHT, padx=(8, 0))
        self.simple_mode_button = self._button(self.mode_bar, "日常生产", lambda: self.set_workflow_mode("simple"), kind="primary")
        self.simple_mode_button.pack(side=tk.LEFT)
        self.full_mode_button = self._button(self.mode_bar, "精修模式", lambda: self.set_workflow_mode("full"), kind="secondary")
        self.full_mode_button.pack(side=tk.LEFT, padx=(6, 0))
        ui.attach_tooltip(self.simple_mode_button, "日常生产：一键矩阵生产直达合格待发布。")
        ui.attach_tooltip(self.full_mode_button, "精修模式：保留更完整的中间产物，便于外部精修。")
        self.status_badge = ui.status_chip(header, "未选择项目", "info")
        self.status_badge.pack(side=tk.RIGHT, padx=(8, 0))

        middle = tk.Frame(workspace, bg=MAIN_BG)
        middle.pack(fill=tk.BOTH, expand=True, padx=(28, 18), pady=(0, 10))
        self._middle = middle  # 滚动卡片流容器；进时间线页时隐藏、换固定布局
        self.canvas = tk.Canvas(middle, bg=MAIN_BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(middle, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.page_frame = tk.Frame(self.canvas, bg=MAIN_BG)
        self.page_window = self.canvas.create_window((0, 0), window=self.page_frame, anchor="nw")
        self.page_frame.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize_page_frame)
        self.bind_all("<MouseWheel>", self._on_mousewheel)

        log_shell = tk.Frame(workspace, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        log_shell.pack(fill=tk.X, padx=28, pady=(0, 22))
        self._log_shell = log_shell
        log_head = tk.Frame(log_shell, bg=CARD_BG)
        log_head.pack(fill=tk.X, padx=14, pady=(10, 0))
        tk.Label(log_head, text="运行提示", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        self._button(log_head, "清空", lambda: self.log.delete("1.0", tk.END), kind="ghost").pack(side=tk.RIGHT)
        # 剪辑/生产进度条 + 暂停/停止（仅任务运行时显示）
        self.progress_label = tk.Label(log_head, text="", bg=CARD_BG, fg=ACCENT, font=(FONT, 9, "bold"))
        self.progress_label.pack(side=tk.RIGHT, padx=10)
        self.progress_bar = ttk.Progressbar(log_head, mode="indeterminate", length=220)
        self.bg_stop_button = ui.button(log_head, "⏹ 停止", self._bg_request_stop, kind="secondary")
        self.bg_pause_button = ui.button(log_head, "⏸ 暂停", self._bg_toggle_pause, kind="secondary")
        self.log = tk.Text(log_shell, height=5, bd=0, bg=SOFT_BG, fg=TEXT, insertbackground=TEXT, font=(FONT, 9), wrap=tk.WORD)
        self.log.pack(fill=tk.X, padx=14, pady=10)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _nav_button(self, parent: tk.Widget, key: str, title: str, desc: str) -> tk.Button:
        button = tk.Button(
            parent,
            text=f"{title}\n{desc}",
            command=lambda page=key: self.show_page(page),
            anchor="w",
            justify=tk.LEFT,
            bd=0,
            relief=tk.FLAT,
            padx=14,
            pady=10,
            bg=SIDEBAR_BG,
            fg=TEXT,
            activebackground=SIDEBAR_ACTIVE,
            activeforeground=ACCENT,
            font=(FONT, 10, "bold"),
            cursor="hand2",
        )
        button.pack(fill=tk.X, pady=4)
        self.nav_buttons[key] = button
        return button

    def _rebuild_nav(self) -> None:
        if not hasattr(self, "nav_container"):
            return
        for child in self.nav_container.winfo_children():
            child.destroy()
        self.nav_buttons.clear()
        config = self._config_or_none()
        full_mode = bool(config and config.workflow_mode == "full")
        self.advanced_expanded = full_mode or self.advanced_expanded

        for key, title, desc in self.CORE_NAV_ITEMS:
            self._nav_button(self.nav_container, key, title, desc)

        self.advanced_toggle_button = tk.Button(
            self.nav_container,
            text=("高级功能 ▾" if self.advanced_expanded else "高级功能 ▸"),
            command=self.toggle_advanced_nav,
            anchor="w",
            bd=0,
            relief=tk.FLAT,
            padx=14,
            pady=8,
            bg=SIDEBAR_BG,
            fg=MUTED,
            activebackground=SIDEBAR_ACTIVE,
            activeforeground=TEXT,
            font=(FONT, 10, "bold"),
            cursor="hand2",
        )
        self.advanced_toggle_button.pack(fill=tk.X, pady=(12, 4))
        self.advanced_nav_frame = tk.Frame(self.nav_container, bg=SIDEBAR_BG)
        self.advanced_nav_frame.pack(fill=tk.X)
        if self.advanced_expanded:
            for key, title, desc in self.ADVANCED_NAV_ITEMS:
                self._nav_button(self.advanced_nav_frame, key, title, desc)

    def toggle_advanced_nav(self) -> None:
        self.advanced_expanded = not self.advanced_expanded
        self._rebuild_nav()
        self._highlight_current_nav()

    def _resize_page_frame(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.page_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.focus_get() is self.log:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _button(self, parent: tk.Widget, text: str, command, kind: str = "primary", requires_project: bool = False) -> tk.Button:
        if kind == "light":
            kind = "secondary"
        button = ui.button(parent, text, self._guard_project(command, text) if requires_project else command, kind=kind)
        if requires_project and not self._config_or_none():
            button.configure(state=tk.DISABLED, fg=theme.DISABLED, cursor="arrow")
            ui.attach_tooltip(button, "先在素材入库创建或选择项目")
        return button

    def _guard_project(self, command, label: str):
        def wrapped():
            if self._config_or_none():
                return command()
            self._show_project_required(label)
            return None

        return wrapped

    def _show_project_required(self, label: str = "当前操作") -> None:
        go = messagebox.askyesno("需要先选择项目", f"{label} 需要先创建或选择项目。\n\n是否前往素材入库？")
        if go:
            self.show_page("material")

    def toggle_support_panel(self) -> None:
        self.support_expanded = not self.support_expanded
        for child in self.support_detail.winfo_children():
            child.destroy()
        if not self.support_expanded:
            self.support_detail.pack_forget()
            return
        self.support_detail.pack(fill=tk.X, pady=(8, 0))
        if self.support_qr_image:
            tk.Label(self.support_detail, image=self.support_qr_image, bg=SIDEBAR_BG, bd=0).pack(anchor="w")
        else:
            tk.Label(self.support_detail, text="支持二维码未加载", bg=SIDEBAR_BG, fg=WARN, font=(FONT, 9)).pack(anchor="w")

    def toggle_edit_advanced(self) -> None:
        self.edit_advanced_expanded = not self.edit_advanced_expanded
        self.show_page("remix")

    def _style_button(self, button: tk.Button, kind: str) -> None:
        bg, fg, active, border = ui.BUTTON_STYLES.get(kind, ui.BUTTON_STYLES["primary"])
        button.configure(bg=bg, fg=fg, activebackground=active, activeforeground=fg, highlightbackground=border)

    def _card(self, parent: tk.Widget, title: str, subtitle: str = "") -> tk.Frame:
        card = ui.Card(parent, title, subtitle)
        body = card.body
        body.card = card  # type: ignore[attr-defined]
        return body

    def _place_card(self, body: tk.Frame, row: int, column: int, columnspan: int = 1, sticky: str = "nsew") -> None:
        body.card.grid(row=row, column=column, columnspan=columnspan, sticky=sticky, padx=8, pady=8)  # type: ignore[attr-defined]

    def _row(self, parent: tk.Widget, label: str, var: tk.StringVar, picker=None) -> None:
        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill=tk.X, pady=5)
        tk.Label(row, text=label, bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        if picker:
            self._button(row, "选择", picker, kind="secondary").pack(side=tk.LEFT, padx=(8, 0))

    def _stat_strip(self, parent: tk.Widget, stats: list[tuple[str, str, str]], columns: int = 4) -> None:
        grid = tk.Frame(parent, bg=CARD_BG)
        grid.pack(fill=tk.X)
        for index, (label, value, color) in enumerate(stats):
            item = tk.Frame(grid, bg=SOFT_BG, highlightbackground=BORDER, highlightthickness=1)
            item.grid(row=index // columns, column=index % columns, sticky="ew", padx=5, pady=5)
            grid.grid_columnconfigure(index % columns, weight=1)
            tk.Label(item, text=value, bg=SOFT_BG, fg=color, font=(FONT, 18, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Label(item, text=label, bg=SOFT_BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=12, pady=(0, 10))

    def _empty_state(self, parent: tk.Widget, text: str) -> None:
        ui.empty_hint(parent, text).pack(anchor="w")

    # 页面已下线的目录组：仅隐藏“完整目录表”里的介绍，底层目录常量与引擎不动
    # （04_标题封面：标题/封面/包装页已删；05_发布交接：发布回流页已删）。
    _HIDDEN_DIR_GROUPS = {"04_标题封面", "05_发布交接"}

    def _directory_overview(self, parent: tk.Widget) -> None:
        grouped: dict[str, list[str]] = {}
        for spec in DIRECTORY_SPECS:
            top = spec.path.split("/", 1)[0]
            if top in self._HIDDEN_DIR_GROUPS:
                continue  # 对应页面已下线，不再在目录表里介绍，避免“功能没了介绍还在”
            grouped.setdefault(top, []).append(f"{spec.path}：{spec.title} - {spec.purpose}")
        for top, lines in grouped.items():
            group = tk.Frame(parent, bg=SOFT_BG, highlightbackground=BORDER, highlightthickness=1)
            group.pack(fill=tk.X, pady=4)
            tk.Label(group, text=top, bg=SOFT_BG, fg=TEXT, font=(FONT, 10, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
            for line in lines:
                tk.Label(group, text=line, bg=SOFT_BG, fg=MUTED, font=(FONT, 9), anchor="w", justify=tk.LEFT, wraplength=1040).pack(fill=tk.X, padx=10, pady=(0, 3))

    def show_page(self, key: str) -> None:
        self.current_page = key
        title, subtitle = self.PAGE_META.get(key, self.PAGE_META["material"])
        self.page_title_label.configure(text=title)
        self.page_subtitle_label.configure(text=subtitle)
        self._highlight_current_nav()

        for child in self.page_frame.winfo_children():
            child.destroy()
        for column in range(4):
            self.page_frame.grid_columnconfigure(column, weight=1)

        if key == "material":
            page_method = self._page_project
        else:
            page_method = getattr(self, f"_page_{key}", self._page_project)
        page_method()
        self.refresh_status()
        self.canvas.yview_moveto(0)

    def _on_close(self) -> None:
        """退出：停后台任务的子进程，再销毁窗口。"""
        try:
            from . import proc as _proc

            self._bg_stop.set()
            _proc.terminate_active()
        except Exception:
            pass
        self.destroy()

    def _highlight_current_nav(self) -> None:
        for nav_key, button in self.nav_buttons.items():
            selected = nav_key == self.current_page
            button.configure(
                bg=SIDEBAR_ACTIVE if selected else SIDEBAR_BG,
                fg=ACCENT if selected else TEXT,
            )

    def _config_or_none(self):
        value = self.project_var.get().strip()
        if not value:
            return None
        path = Path(value)
        if not (path / "project.json").exists():
            return None
        try:
            return load_config(path)
        except Exception:
            return None

    def current_project(self) -> Path:
        value = self.project_var.get().strip()
        if not value:
            raise ValueError("当前路径不是水星剪辑项目，请先创建或选择项目。")
        return Path(value)

    def _load_config(self):
        return load_config(self.current_project())

    def _cached_counts(self, config):
        """工作流计数带 TTL 缓存 + 后台刷新：避免每次 refresh_status 全盘扫素材目录卡 UI。

        命中缓存立即返回旧值（≤8s 内不重扫）；过期则用旧值先渲染、后台线程重扫，
        扫完再刷新一次徽标。冷启动无缓存时同步扫一次。
        """
        import time as _time

        root = str(config.root)
        now = _time.monotonic()
        cached = self._counts_cache.get(root)
        if cached and now - cached[0] < 8.0:
            return cached[1]
        if cached is None:
            counts = workflow_counts(config)
            self._counts_cache[root] = (now, counts)
            return counts
        # 有旧值：先返回旧值，后台重扫
        if not self._counts_refreshing:
            self._counts_refreshing = True

            def rescan():
                try:
                    fresh = workflow_counts(config)
                except Exception:
                    fresh = None

                def apply():
                    self._counts_refreshing = False
                    if fresh is not None:
                        self._counts_cache[root] = (_time.monotonic(), fresh)
                        if self._config_or_none() and str(self._config_or_none().root) == root:
                            try:
                                mode_text = "精修模式" if config.workflow_mode == "full" else "日常生产"
                                self.status_badge.configure(text=f"{mode_text} · 待发布 {fresh.ready}")
                            except Exception:
                                pass

                self.after(0, apply)

            threading.Thread(target=rescan, daemon=True).start()
        return cached[1]

    def refresh_status(self) -> None:
        config = self._config_or_none()
        if not config:
            self.status_badge.configure(text="未选择项目", bg=theme.INFO_BG, fg=theme.INFO)
            self.sidebar_project.configure(text="未选择项目")
            self._style_button(self.simple_mode_button, "secondary")
            self._style_button(self.full_mode_button, "secondary")
            return
        counts = self._cached_counts(config)
        mode_text = "精修模式" if config.workflow_mode == "full" else "日常生产"
        self.status_badge.configure(text=f"{mode_text} · 待发布 {counts.ready}", bg=theme.SUCCESS_BG, fg=OK)
        self.sidebar_project.configure(text=config.project_name)
        self.workflow_full_var.set(config.workflow_mode == "full")
        self._style_button(self.simple_mode_button, "secondary" if config.workflow_mode == "full" else "primary")
        self._style_button(self.full_mode_button, "primary" if config.workflow_mode == "full" else "secondary")
        self._load_tool_vars(config)
        self._load_dub_dir_vars(config)
        self._rebuild_nav()
        self._highlight_current_nav()

    def _load_tool_vars(self, config) -> None:
        self.ffmpeg_var.set(config.tools.ffmpeg)
        self.ffprobe_var.set(config.tools.ffprobe)
        self.local_editor_exe_var.set(config.tools.local_editor_exe)
        self.publish_exe_var.set(config.tools.publish_tool_exe)

    def _load_dub_dir_vars(self, config) -> None:
        project_root = str(config.root)
        project_changed = self._dub_paths_project_root != project_root
        self._dub_paths_project_root = project_root
        for attr, var in (
            ("dub_last_audio_dir", self.dub_audio_dir_var),
            ("dub_last_video_dir", self.dub_video_dir_var),
        ):
            current = var.get().strip()
            if current and Path(current).exists() and not project_changed:
                continue
            remembered = getattr(config, attr, "")
            var.set(remembered if remembered and Path(remembered).exists() else "")

    def write_log(self, text: str) -> None:
        self.log.insert(tk.END, text + "\n")
        self.log.see(tk.END)

    def _error_log_path(self) -> Path:
        config = self._config_or_none()
        if config:
            root = config.root / DIR_LOGS
        else:
            root = version_root() / "logs"
        root.mkdir(parents=True, exist_ok=True)
        return root / "ui_errors.log"

    def _write_error_detail(self, label: str) -> Path:
        path = self._error_log_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{label}]\n")
            handle.write(traceback.format_exc())
        return path

    def _human_error(self, exc: Exception) -> tuple[str, str]:
        raw = str(exc).strip() or exc.__class__.__name__
        lower = raw.lower()
        if "水星剪辑项目" in raw or "项目配置" in raw or "project.json" in lower or "请先创建或选择项目" in raw:
            return "当前路径不是水星剪辑项目，请先创建或选择项目", "去素材入库创建项目，或选择已有项目目录。"
        if "没有可检测视频" in raw or "没有可用成片" in raw or "为空" in raw or "没有视频文件" in raw or "原始视频目录" in raw or "没有找到可重新生成的原始素材" in raw:
            return "还没有可用素材", "先去素材入库导入原始视频，或在剪辑与整合页生成分镜素材包。"
        if "还没有自动剪辑分镜" in raw:
            return "还没有自动剪辑结果", "先在剪辑与整合页点击一键自动剪辑。"
        if isinstance(exc, FileNotFoundError):
            return "需要的文件或目录不存在", "检查项目目录是否正确，或先完成上一步生成。"
        return raw[:120], "按提示检查输入；完整错误已写入运行日志，可交给管理员排查。"

    def _failure_message(self, label: str, exc: Exception, detail_path: Path | None = None) -> str:
        reason, suggestion = self._human_error(exc)
        suffix = f"\n日志：{detail_path}" if detail_path else ""
        return f"失败：{label}。原因：{reason}。建议：{suggestion}{suffix}"

    def run_background(self, label: str, action, on_done=None, confirm: str | None = None) -> None:
        # 忙碌守卫：一次只跑一个后台任务，避免重复点击把任务堆叠成“卡屏”观感。
        if getattr(self, "_bg_busy", False):
            messagebox.showinfo("请稍候", f"正在执行：{getattr(self, '_bg_label', '上一个任务')}，完成后再操作。")
            return
        # 二次确认：生成/重做类耗时或有产物写入的动作先弹确认。
        if confirm and not messagebox.askyesno(f"确认{label}", confirm):
            return

        self._bg_busy = True
        self._bg_label = label
        try:
            self.configure(cursor="watch")
        except Exception:
            pass
        self._progress_show(label)

        def finish() -> None:
            self._bg_busy = False
            try:
                self.configure(cursor="")
            except Exception:
                pass
            self._progress_hide()

        def worker() -> None:
            self.after(0, lambda: self.write_log(f"开始：{label}"))
            try:
                result = action()
            except TaskCancelled:
                def cancelled() -> None:
                    self.write_log(f"已停止：{label}。（可重新发起；批量任务可用断点续跑接续）")
                    finish()

                self.after(0, cancelled)
                return
            except Exception as exc:
                if self._bg_stop.is_set():
                    # 停止时终止了子进程，引擎会以异常收场——按“已停止”处理，不弹错误框。
                    def cancelled2() -> None:
                        self.write_log(f"已停止：{label}（在跑的子进程已终止）。")
                        finish()

                    self.after(0, cancelled2)
                    return
                detail_path = self._write_error_detail(label)
                friendly = self._failure_message(label, exc, detail_path)

                def fail() -> None:
                    self.write_log(friendly)
                    finish()
                    messagebox.showerror(label, friendly)

                self.after(0, fail)
                return

            def ok() -> None:
                self.write_log(f"完成：{result}")
                finish()
                if on_done:
                    on_done()
                self.refresh_status()

            self.after(0, ok)

        threading.Thread(target=worker, daemon=True).start()

    # ---- 进度条 + 暂停/停止 ----
    def _progress_show(self, label: str) -> None:
        bar = getattr(self, "progress_bar", None)
        if bar is None:
            return
        self._bg_stop.clear()
        self._bg_pause.clear()
        try:
            self.progress_label.configure(text=f"⏳ {label}…")
            bar.configure(mode="indeterminate")
            bar.pack(side=tk.RIGHT)
            bar.start(12)
            self.bg_pause_button.configure(text="⏸ 暂停")
            self.bg_stop_button.pack(side=tk.RIGHT, padx=(4, 0))
            self.bg_pause_button.pack(side=tk.RIGHT, padx=(4, 0))
        except Exception:
            pass

    def _progress_hide(self) -> None:
        bar = getattr(self, "progress_bar", None)
        if bar is None:
            return
        try:
            bar.stop()
            bar.pack_forget()
            self.progress_label.configure(text="")
            self.bg_pause_button.pack_forget()
            self.bg_stop_button.pack_forget()
        except Exception:
            pass

    def _bg_toggle_pause(self) -> None:
        """暂停/继续：在下一个进度检查点生效（不打断当前正在写的文件）。"""
        if not getattr(self, "_bg_busy", False):
            return
        if self._bg_pause.is_set():
            self._bg_pause.clear()
            try:
                self.bg_pause_button.configure(text="⏸ 暂停")
            except Exception:
                pass
            self.write_log(f"已继续：{getattr(self, '_bg_label', '任务')}")
        else:
            self._bg_pause.set()
            try:
                self.bg_pause_button.configure(text="▶ 继续")
            except Exception:
                pass
            self.write_log(f"已暂停：{getattr(self, '_bg_label', '任务')}（在下一个进度点停住，点「继续」恢复）")

    def _bg_request_stop(self) -> None:
        """停止：立刻终止在跑的子进程；任务在检查点/子进程退出后收尾。"""
        if not getattr(self, "_bg_busy", False):
            return
        from . import proc as _proc

        self._bg_stop.set()
        self._bg_pause.clear()
        killed = _proc.terminate_active()
        self.write_log(f"已请求停止：{getattr(self, '_bg_label', '任务')}（终止子进程 {killed} 个）…")

    def _bg_checkpoint(self) -> None:
        """协作式检查点：暂停时在此等待，停止时抛 TaskCancelled。在工作线程里调用。"""
        import time as _time

        while self._bg_pause.is_set() and not self._bg_stop.is_set():
            _time.sleep(0.2)
        if self._bg_stop.is_set():
            raise TaskCancelled()

    def set_progress(self, done: int, total: int | None, label: str = "") -> None:
        """供后台生产任务（工作线程）汇报确定态百分比进度：done/total。

        同时兼任暂停/停止检查点——所有汇报进度的生产流程天然可暂停、可停止。
        引擎侧以 progress(done, total) 回调调用；UI 更新通过 self.after 回主线程。
        """
        if threading.current_thread() is not threading.main_thread() and getattr(self, "_bg_busy", False):
            self._bg_checkpoint()

        def apply() -> None:
            bar = getattr(self, "progress_bar", None)
            if bar is None:
                return
            try:
                if total and total > 0:
                    bar.stop()
                    bar.configure(mode="determinate", maximum=total, value=min(done, total))
                    pct = int(done * 100 / total)
                    self.progress_label.configure(text=f"⏳ {label or self._bg_label} {done}/{total}（{pct}%）")
                    if not bar.winfo_ismapped():
                        bar.pack(side=tk.RIGHT)
            except Exception:
                pass

        self.after(0, apply)

    def _refresh_license_countdown(self) -> None:
        """左下角激活有效期倒计时：读许可缓存的 expire_at，每分钟刷新一次。"""
        label = getattr(self, "license_countdown_label", None)
        if label is None:
            return
        text, level = "激活状态：未激活", "muted"
        try:
            from datetime import datetime

            from .protection import LicenseCache, format_license_countdown

            expire_at = str(LicenseCache().load().get("expire_at") or "")
            text, level = format_license_countdown(expire_at, datetime.now())
        except Exception:
            text, level = "激活状态：未知", "muted"
        color = {"success": OK, "warning": WARN, "danger": ERROR, "muted": MUTED}.get(level, MUTED)
        try:
            label.configure(text=text, fg=color)
        except Exception:
            return
        # 每 60 秒刷新一次倒计时
        self.after(60000, self._refresh_license_countdown)

    def _set_material_path(self, role: str, text: str) -> None:
        self.material_path_vars.setdefault(role, tk.StringVar(value="未选择")).set(text)

    def pick_root(self) -> None:
        path = filedialog.askdirectory(title="选择项目父目录")
        if path:
            self.root_var.set(path)

    def pick_project(self) -> None:
        path = filedialog.askdirectory(title="选择已有项目目录")
        if path:
            self.project_var.set(path)
            config = self._config_or_none()
            if config:
                self.name_var.set(config.project_name)
                self.drama_var.set(config.drama_name)
            self.show_page("material")

    def pick_script(self) -> None:
        path = filedialog.askopenfilename(
            title="选择分镜表",
            filetypes=[("分镜表", "*.csv *.txt *.xlsx *.xlsm"), ("全部文件", "*.*")],
        )
        if path:
            self.edit_script_var.set(path)

    def download_dub_manifest_template(self) -> None:
        from .dub_bridge import write_dub_manifest_template

        target = self.dub_audio_dir_var.get().strip()
        if target:
            out_dir = Path(target)
        else:
            out_dir = self.current_project() / DIR_A
        try:
            path = write_dub_manifest_template(out_dir)
        except Exception as exc:
            messagebox.showerror("配音清单模板", f"生成模板失败：{exc}")
            return
        self.write_log(f"配音清单模板已生成：{path}")
        if messagebox.askyesno("配音清单模板", f"模板已生成：\n{path}\n\n是否打开所在目录？"):
            self.open_folder(path.parent)

    def validate_dub_manifest_action(self) -> None:
        from .dub_bridge import validate_dub_manifest

        path = filedialog.askopenfilename(title="选择回传的配音清单", filetypes=[("配音清单", "*.csv"), ("全部文件", "*.*")])
        if not path:
            return
        issues = validate_dub_manifest(Path(path))
        if not issues:
            messagebox.showinfo("配音清单校验", "校验通过，可用于对齐合成。")
        else:
            messagebox.showwarning("配音清单校验", "发现以下问题，请修正后重试：\n- " + "\n- ".join(issues))

    def pick_dub_audio_dir(self) -> None:
        path = filedialog.askdirectory(title="选择配音包文件夹")
        if path:
            self.dub_audio_dir_var.set(path)

    def pick_dub_video_dir(self) -> None:
        path = filedialog.askdirectory(title="选择分镜视频文件夹")
        if path:
            self.dub_video_dir_var.set(path)

    def pick_episode_file(self) -> None:
        path = filedialog.askopenfilename(title="选择整集视频", filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.m4v *.webm"), ("全部文件", "*.*")])
        if path:
            self.episode_file_var.set(path)

    def pick_episode_replacement_audio(self) -> None:
        path = filedialog.askopenfilename(title="选择替换音频", filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg"), ("全部文件", "*.*")])
        if path:
            self.episode_replacement_audio_var.set(path)

    def _pick_exe(self, var: tk.StringVar, title: str) -> None:
        path = filedialog.askopenfilename(title=title, filetypes=[("可执行文件", "*.exe"), ("全部文件", "*.*")])
        if path:
            var.set(path)

    def open_folder(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        starter = getattr(os, "startfile", None)
        if starter:
            starter(str(path))
        else:
            messagebox.showinfo("打开目录", str(path))

    def open_file(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")
        starter = getattr(os, "startfile", None)
        if starter:
            starter(str(path))
        else:
            messagebox.showinfo("打开文件", str(path))

    def open_operation_manual(self) -> None:
        manual = version_root() / "docs" / OPERATION_MANUAL_NAME
        if not manual.exists():
            messagebox.showwarning("帮助", f"未找到操作手册：{manual}")
            return
        self.open_file(manual)

    def _production_dirs(self, config) -> list[Path]:
        ready_root = config.root / DIR_READY
        if not ready_root.exists():
            return []
        return sorted(
            [path for path in ready_root.iterdir() if path.is_dir() and path.name.startswith("产线_")],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _latest_production_dir(self, config) -> Path | None:
        dirs = self._production_dirs(config)
        return dirs[0] if dirs else None

    def _dub_production_options(self, config) -> tuple[int, str]:
        if self.remix_page_initialized:
            copies = int(self.account_count_var.get())
            dedup_label = self.dedup_level_var.get()
        else:
            copies = int(config.recipe.copies_per_source or 3)
            dedup_label = {"light": "基础", "balanced": "标准", "strong": "增强"}.get(config.recipe.dedup_level, "标准")
        return max(1, copies), dedup_label or "标准"

    def open_latest_production_dir(self) -> None:
        config = self._load_config()
        latest = self._latest_production_dir(config)
        if latest:
            self.open_folder(latest)
            return
        self.open_folder(config.root / DIR_READY)

    def _page_project(self) -> None:
        setup = self._card(self.page_frame, "新建或选择项目", "项目创建后，素材、成片、清单和回流数据都会固定放在项目目录里。")
        self._place_card(setup, 0, 0, 2)
        self._row(setup, "父目录", self.root_var, self.pick_root)
        self._row(setup, "项目名称", self.name_var)
        self._row(setup, "剧名", self.drama_var)
        self._row(setup, "当前项目", self.project_var, self.pick_project)
        actions = tk.Frame(setup, bg=CARD_BG)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._button(actions, "创建项目", self.create_project, kind="primary").pack(side=tk.LEFT)
        self._button(actions, "选择已有项目", self.pick_project, kind="secondary").pack(side=tk.LEFT, padx=8)
        self._button(actions, "打开项目目录", lambda: self.open_folder(self.current_project()), kind="ghost", requires_project=True).pack(side=tk.LEFT, padx=8)

        status = self._card(self.page_frame, "入库状态", "核心素材是否已经准备好。")
        self._place_card(status, 0, 2, 2)
        config = self._config_or_none()
        if config:
            counts = self._cached_counts(config)
            self._stat_strip(
                status,
                [
                    ("原始视频", str(counts.source), ACCENT),
                    ("剪辑输入", str(counts.auto_input), ACCENT),
                    ("背景素材", str(counts.background), ACCENT),
                    ("贴图素材", str(counts.stickers), ACCENT),
                ],
            )
        else:
            self._empty_state(status, "还没有选择项目。先创建项目，再导入原片。")

        material = self._card(self.page_frame, "素材清单与目录", "各类素材的导入已下放到对应模块（矩阵生产/配音对齐）；这里生成清单、打开目录。")
        self._place_card(material, 1, 0, 4)
        material_actions = tk.Frame(material, bg=CARD_BG)
        material_actions.pack(fill=tk.X)
        self._button(material_actions, "生成素材清单", self.inventory, kind="secondary", requires_project=True).pack(side=tk.LEFT)
        self._button(material_actions, "打开项目目录", lambda: self.open_folder(self.current_project()), kind="ghost", requires_project=True).pack(side=tk.LEFT, padx=8)

        quick = self._card(self.page_frame, "下一步", "素材准备好后进入矩阵生产（含自动剪辑），或做配音对齐成片。")
        self._place_card(quick, 2, 0, 4)
        self._button(quick, "去矩阵生产", lambda: self.show_page("remix"), kind="primary").pack(side=tk.LEFT)
        self._button(quick, "去配音对齐成片", lambda: self.show_page("dub"), kind="secondary").pack(side=tk.LEFT, padx=8)

        dirs = self._card(self.page_frame, "完整目录表", "低频查看：每个流程的固定输入和产出位置。")
        self._place_card(dirs, 3, 0, 4)
        self._directory_overview(dirs)
        self._button(dirs, "打开目录表文件", lambda: self.open_file(self._load_config().root / DIR_CONFIG / "项目目录表.csv"), kind="secondary", requires_project=True).pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------------------------- 配音对齐成片（独立入口）
    DUB_ASPECTS = {
        "9:16 竖屏": (1080, 1920),
        "16:9 横屏": (1920, 1080),
        "1:1 方形": (1080, 1080),
        "4:3": (1440, 1080),
        "3:4": (1080, 1440),
    }

    def _page_dub(self) -> None:
        dub = self._card(self.page_frame, "配音对齐成片",
                         "配音包与分镜视频按句对齐；选画面尺寸合成，可直接进矩阵生产，或导出剪映草稿二次精修。")
        self._place_card(dub, 0, 0, 4)
        self._row(dub, "配音包", self.dub_audio_dir_var, self.pick_dub_audio_dir)
        self._row(dub, "分镜视频", self.dub_video_dir_var, self.pick_dub_video_dir)
        self._row(dub, "成片名", self.dub_name_var)
        size_row = tk.Frame(dub, bg=CARD_BG)
        size_row.pack(fill=tk.X, pady=5)
        tk.Label(size_row, text="画面尺寸", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(size_row, textvariable=self.dub_aspect_var, values=list(self.DUB_ASPECTS.keys()),
                     state="readonly", width=14).pack(side=tk.LEFT)
        tk.Label(size_row, text="（画面按此比例缩放并居中填充黑边，1:1/4:3/3:4/9:16/16:9）",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side=tk.LEFT, padx=8)
        dub_options = tk.Frame(dub, bg=CARD_BG)
        dub_options.pack(fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(dub_options, text="烧录字幕", variable=self.dub_subtitle_var).pack(side=tk.LEFT)
        ttk.Checkbutton(dub_options, text="完成后直接一键生产", variable=self.dub_auto_produce_var).pack(side=tk.LEFT, padx=12)
        self._button(dub_options, "对齐并合成", self.dub_assemble_action, kind="primary", requires_project=True).pack(side=tk.LEFT, padx=12)
        self._button(dub_options, "打开成片目录", lambda: self.open_folder(self._load_config().root / DIR_DUB_ASSEMBLED), kind="ghost", requires_project=True).pack(side=tk.LEFT)
        dub_template = tk.Frame(dub, bg=CARD_BG)
        dub_template.pack(fill=tk.X, pady=(6, 0))
        tk.Label(dub_template, text="配音清单：", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side=tk.LEFT)
        self._button(dub_template, "下载模板", self.download_dub_manifest_template, kind="secondary", requires_project=True).pack(side=tk.LEFT)
        self._button(dub_template, "校验回传清单", self.validate_dub_manifest_action, kind="ghost", requires_project=True).pack(side=tk.LEFT, padx=8)

        capcut = self._card(self.page_frame, "导出剪映草稿", "把对齐后的成片/分镜导出为剪映（CapCut）草稿包，导入剪映二次精修。")
        self._place_card(capcut, 1, 0, 4)
        tk.Label(capcut, text="导出后在剪映「本地草稿」里打开，逐镜微调转场/特效/字幕。",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9), wraplength=980, justify=tk.LEFT).pack(anchor="w")
        self._button(capcut, "导出剪映草稿包", self.export_capcut, kind="secondary", requires_project=True).pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------------------------- 剪辑工作台：资源库
    def _edit_library_card(self, row: int = 6) -> None:
        from .edit_library import (
            EFFECT_CATALOG,
            FILTERS,
            SOUND_EFFECTS,
            SUBTITLE_STYLES,
            TRANSITION_CATALOG,
        )

        counts = f"滤镜 {len(FILTERS)} · 特效 {len(EFFECT_CATALOG)} · 转场 {len(TRANSITION_CATALOG)} · 字幕 {len(SUBTITLE_STYLES)} · 音效 {len(SOUND_EFFECTS)}"
        card = self._card(self.page_frame, "剪辑资源库", f"自动剪辑与手动剪辑共用同一套库：{counts}。")
        self._place_card(card, row, 0, 4)
        filter_row = tk.Frame(card, bg=CARD_BG)
        filter_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(filter_row, text="筛选", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=6, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(filter_row, textvariable=self.library_filter_var,
                     values=["全部", "滤镜", "特效", "转场", "字幕", "音效"], state="readonly", width=10).pack(side=tk.LEFT)
        self._button(filter_row, "刷新列表", lambda: self.show_page("remix"), kind="ghost").pack(side=tk.LEFT, padx=8)

        tree = ttk.Treeview(card, columns=("lib", "cat", "name", "detail"), show="headings", height=10)
        for col, text, width in (("lib", "库", 70), ("cat", "分类", 110), ("name", "名称", 160), ("detail", "实现/说明", 560)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.pack(fill=tk.X)
        sel = self.library_filter_var.get()
        rows: list[tuple[str, str, str, str]] = []
        if sel in ("全部", "滤镜"):
            for name, chain in FILTERS.items():
                rows.append(("滤镜", "画面", name, chain or "原片直出"))
        if sel in ("全部", "特效"):
            for it in EFFECT_CATALOG:
                rows.append(("特效", it.category, it.name, it.ffmpeg))
        if sel in ("全部", "转场"):
            for it in TRANSITION_CATALOG:
                rows.append(("转场", it.category, it.name, f"xfade={it.base}"))
        if sel in ("全部", "字幕"):
            for name, style in SUBTITLE_STYLES.items():
                rows.append(("字幕", "样式", name, str(style)))
        if sel in ("全部", "音效"):
            for it in SOUND_EFFECTS:
                rows.append(("音效", it.category, it.name, it.filename))
        for r in rows:
            tree.insert("", tk.END, values=r)
        tk.Label(card, text=f"共 {len(rows)} 项。这些名称即赛道预设/语义匹配/编排引擎引用的真实库项。",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(8, 0))

    def _import_card(self, row: int, title: str, roles: list[str], span: int = 4) -> None:
        """通用素材导入卡：为一组素材角色渲染「选文件夹/选文件」按钮（导入到项目对应目录）。"""
        card = self._card(self.page_frame, title, "选文件夹批量导入；少量补充可选文件。素材自动归位到项目目录。")
        self._place_card(card, row, 0, span)
        grid = tk.Frame(card, bg=CARD_BG)
        grid.pack(fill=tk.X)
        for index, role in enumerate(roles):
            r, c = index // 2, index % 2
            item = tk.Frame(grid, bg=SOFT_BG, highlightbackground=BORDER, highlightthickness=1)
            item.grid(row=r, column=c, sticky="ew", padx=5, pady=5)
            grid.grid_columnconfigure(c, weight=1)
            top = tk.Frame(item, bg=SOFT_BG)
            top.pack(fill=tk.X)
            tk.Label(top, text=role, bg=SOFT_BG, fg=TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT, padx=(12, 8), pady=8)
            self._button(top, "选文件夹", lambda cur=role: self.import_assets_folder_action(cur), kind="primary", requires_project=True).pack(side=tk.LEFT, pady=6)
            self._button(top, "选文件", lambda cur=role: self.import_assets_action(cur), kind="secondary", requires_project=True).pack(side=tk.LEFT, padx=8, pady=6)
            path_var = self.material_path_vars.setdefault(role, tk.StringVar(value="未选择"))
            tk.Label(item, textvariable=path_var, bg=SOFT_BG, fg=MUTED, font=(FONT, 8),
                     anchor="w", justify=tk.LEFT, wraplength=440).pack(fill=tk.X, padx=12, pady=(0, 8))

    def _auto_cut_cards(self, row: int) -> None:
        """自动剪辑分镜（矩阵生产的前置步骤）：入口卡 + 高级参数卡。"""
        from .edit_compose import ASPECT_RATIOS
        from .edit_genre import genre_names

        task = self._card(self.page_frame, "② 自动剪辑分镜（可选前置）",
                          "把原始素材智能剪成分镜素材包，作为矩阵生产的输入；已有分镜可跳过。")
        self._place_card(task, row, 0, 2)
        quick_row = tk.Frame(task, bg=CARD_BG)
        quick_row.pack(fill=tk.X, pady=5)
        tk.Label(quick_row, text="目标时长", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
        ttk.Spinbox(quick_row, from_=15, to=600, increment=5, textvariable=self.edit_target_var, width=10).pack(side=tk.LEFT)
        tk.Label(quick_row, text="秒", bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(side=tk.LEFT, padx=8)
        genre_row = tk.Frame(task, bg=CARD_BG)
        genre_row.pack(fill=tk.X, pady=5)
        tk.Label(genre_row, text="赛道画风", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(genre_row, textvariable=self.edit_genre_var, values=["", *genre_names()], state="readonly", width=16).pack(side=tk.LEFT)
        tk.Label(genre_row, text="（留空=不套赛道；选后自动铺画风）", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side=tk.LEFT, padx=8)
        canvas_row = tk.Frame(task, bg=CARD_BG)
        canvas_row.pack(fill=tk.X, pady=5)
        tk.Label(canvas_row, text="画面比例", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
        ratios = list(ASPECT_RATIOS.keys()) if isinstance(ASPECT_RATIOS, dict) else list(ASPECT_RATIOS)
        ttk.Combobox(canvas_row, textvariable=self.aspect_ratio_var, values=ratios, state="readonly", width=16).pack(side=tk.LEFT)
        sync_row = tk.Frame(task, bg=CARD_BG)
        sync_row.pack(fill=tk.X, pady=5)
        tk.Label(sync_row, text="音画对齐", bg=CARD_BG, fg=TEXT, font=(FONT, 10, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
        for mode in ("裁剪多余画面", "变速匹配", "不处理"):
            ttk.Radiobutton(sync_row, text=mode, value=mode, variable=self.audio_match_var).pack(side=tk.LEFT, padx=(0, 12))
        actions = tk.Frame(task, bg=CARD_BG)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._button(actions, "一键自动剪辑", self.auto_cut, requires_project=True).pack(side=tk.LEFT)
        self._button(actions, "高级参数", self.toggle_edit_advanced, kind="secondary").pack(side=tk.LEFT, padx=8)

        advanced = self._card(self.page_frame, "② 自动剪辑 · 高级参数", "按字幕、关键词或分镜表精细控制时再展开；含场景检测。")
        self._place_card(advanced, row, 2, 2)
        if not self.edit_advanced_expanded:
            tk.Label(advanced, text="已收起。日常生产保持默认即可。", bg=CARD_BG, fg=MUTED, font=(FONT, 10), wraplength=520, justify=tk.LEFT).pack(anchor="w")
        else:
            strategy_row = tk.Frame(advanced, bg=CARD_BG)
            strategy_row.pack(fill=tk.X, pady=5)
            tk.Label(strategy_row, text="剪辑策略", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
            ttk.Combobox(strategy_row, textvariable=self.auto_strategy_var, values=STRATEGIES, state="readonly", width=20).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            self._button(strategy_row, "按目标推荐", self.recommend_auto_cut_strategy, kind="secondary", requires_project=True).pack(side=tk.LEFT)
            number_row = tk.Frame(advanced, bg=CARD_BG)
            number_row.pack(fill=tk.X, pady=5)
            tk.Label(number_row, text="单段时长", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
            ttk.Spinbox(number_row, from_=1, to=30, increment=0.5, textvariable=self.edit_clip_var, width=8).pack(side=tk.LEFT)
            tk.Label(number_row, text="最短静音", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=10, anchor="w").pack(side=tk.LEFT, padx=(14, 0))
            ttk.Spinbox(number_row, from_=0.2, to=3.0, increment=0.05, textvariable=self.min_silence_var, width=8).pack(side=tk.LEFT)
            threshold_row = tk.Frame(advanced, bg=CARD_BG)
            threshold_row.pack(fill=tk.X, pady=5)
            tk.Label(threshold_row, text="静音阈值", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=10, anchor="w").pack(side=tk.LEFT)
            ttk.Spinbox(threshold_row, from_=-60, to=-15, increment=1, textvariable=self.silence_db_var, width=8).pack(side=tk.LEFT)
            tk.Label(threshold_row, text="镜头阈值", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=10, anchor="w").pack(side=tk.LEFT, padx=(14, 0))
            ttk.Spinbox(threshold_row, from_=0.1, to=0.8, increment=0.05, textvariable=self.scene_threshold_var, width=8).pack(side=tk.LEFT)
            self._row(advanced, "分镜表", self.edit_script_var, self.pick_script)
            self._row(advanced, "关键词", self.auto_keywords_var)
            advanced_actions = tk.Frame(advanced, bg=CARD_BG)
            advanced_actions.pack(fill=tk.X, pady=(8, 0))
            self._button(advanced_actions, "按分镜表生成", self.auto_edit, kind="secondary", requires_project=True).pack(side=tk.LEFT)
            self._button(advanced_actions, "场景检测", self.detect_scene_action, kind="secondary", requires_project=True).pack(side=tk.LEFT, padx=8)

    def _page_remix(self) -> None:
        self.remix_page_initialized = True
        # ① 导入生产素材（原始/剪辑输入/去重背景/贴图/文案）
        self._import_card(0, "① 导入生产素材", ["原始视频", "自动剪辑输入", "去重背景", "贴图素材", "文案分镜", "音频素材"])
        # ② 自动剪辑分镜（前置）
        self._auto_cut_cards(row=1)
        config_card = self._card(self.page_frame, "③ 矩阵生产参数", "确认素材来源、账号数和去重强度，然后一键生产。")
        self._place_card(config_card, 2, 0, 2)
        source_row = tk.Frame(config_card, bg=CARD_BG)
        source_row.pack(fill=tk.X, pady=5)
        tk.Label(source_row, text="素材来源", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(source_row, textvariable=self.render_source_var, values=["原始视频", "最新自动剪辑分镜"], state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)

        mode_row = tk.Frame(config_card, bg=CARD_BG)
        mode_row.pack(fill=tk.X, pady=8)
        tk.Label(mode_row, text="画面模式", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="竖屏", value="vertical", variable=self.mode_var).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_row, text="横屏", value="horizontal", variable=self.mode_var).pack(side=tk.LEFT, padx=18)

        account_row = tk.Frame(config_card, bg=CARD_BG)
        account_row.pack(fill=tk.X, pady=5)
        tk.Label(account_row, text="账号数", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Spinbox(account_row, from_=1, to=50, textvariable=self.account_count_var, width=8).pack(side=tk.LEFT)
        tk.Label(account_row, text="个（一键生产和生成中间结果都使用这个数量）", bg=CARD_BG, fg=MUTED, font=(FONT, 9), wraplength=360, justify=tk.LEFT).pack(side=tk.LEFT, padx=8)

        dedup_row = tk.Frame(config_card, bg=CARD_BG)
        dedup_row.pack(fill=tk.X, pady=5)
        tk.Label(dedup_row, text="去重强度", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(dedup_row, textvariable=self.dedup_level_var, values=["基础", "标准", "增强"], state="readonly", width=10).pack(side=tk.LEFT)
        intro_row = tk.Frame(config_card, bg=CARD_BG)
        intro_row.pack(fill=tk.X, pady=5)
        tk.Label(intro_row, text="首帧差异化", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Checkbutton(intro_row, text="强制每份成片首帧互不相同（平台抽首帧判重时更稳）",
                        variable=self.force_distinct_intro_var).pack(side=tk.LEFT)
        resume_row = tk.Frame(config_card, bg=CARD_BG)
        resume_row.pack(fill=tk.X, pady=5)
        tk.Label(resume_row, text="断点续跑", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Checkbutton(resume_row, text="中断后重跑同一产线跳过已完成的份（长批量更稳）",
                        variable=self.resume_var).pack(side=tk.LEFT)
        action_row = tk.Frame(config_card, bg=CARD_BG)
        action_row.pack(fill=tk.X, pady=(12, 0))
        self._button(action_row, "一键生产", self.produce_matrix, kind="primary", requires_project=True).pack(side=tk.LEFT)
        self._button(action_row, "生成中间结果", self.render, kind="secondary", requires_project=True).pack(side=tk.LEFT, padx=8)
        self._button(action_row, "打开最新产线", self.open_latest_production_dir, kind="ghost", requires_project=True).pack(side=tk.LEFT, padx=8)

        material = self._card(self.page_frame, "素材与模式状态", "日常生产会直达合格待发布；精修模式会保留更多中间产物。")
        self._place_card(material, 2, 2, 2)
        config = self._config_or_none()
        if config:
            counts = self._cached_counts(config)
            self._stat_strip(
                material,
                [
                    ("原始视频", str(counts.source), ACCENT),
                    ("背景视频", str(counts.background), ACCENT),
                    ("贴图图片", str(counts.stickers), ACCENT),
                    ("中间结果", str(counts.intermediate), WARN if counts.intermediate else MUTED),
                ],
            )
            ui.status_chip(material, "当前模式：精修模式" if config.workflow_mode == "full" else "当前模式：日常生产", "info").pack(anchor="w", pady=(8, 0))
        else:
            self._empty_state(material, "先选择项目。")

        folders = self._card(self.page_frame, "④ 产物目录", "一键生产会生成产线目录、生产清单、合格待发布成片和回流模板。")
        self._place_card(folders, 4, 0, 2)
        self._folder_buttons(folders, [DIR_REVIEW, DIR_READY, DIR_BATCH_ARCHIVE, DIR_ASSEMBLED, DIR_EPISODE_REWORK])
        self._button(folders, "生成批次档案", self.build_batch_archive_action, kind="secondary", requires_project=True).pack(anchor="w", pady=(10, 0))

        episode_card = self._card(self.page_frame, "整集重组生产", "把一集完整视频按镜头切开，逐段做差异化，再按原顺序合成多条整集版本。")
        self._place_card(episode_card, 4, 2, 2)
        self._episode_rework_panel(episode_card, config)

        preset = self._card(self.page_frame, "③ 我的预设配套参数", "把常用的赛道+画面比例+去重强度+音画对齐存成命名预设，下次一键套用。")
        self._place_card(preset, 3, 0, 4)
        from .edit_presets import preset_names

        preset_row = tk.Frame(preset, bg=CARD_BG)
        preset_row.pack(fill=tk.X)
        tk.Label(preset_row, text="预设", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=8, anchor="w").pack(side=tk.LEFT)
        names = preset_names()
        ttk.Combobox(preset_row, textvariable=self.preset_var, values=names, state="readonly", width=22).pack(side=tk.LEFT)
        self._button(preset_row, "应用预设", self.apply_preset_action, kind="primary").pack(side=tk.LEFT, padx=8)
        self._button(preset_row, "保存为预设", self.save_preset_action, kind="secondary").pack(side=tk.LEFT, padx=4)
        self._button(preset_row, "删除预设", self.delete_preset_action, kind="ghost").pack(side=tk.LEFT, padx=4)
        current = tk.Frame(preset, bg=CARD_BG)
        current.pack(fill=tk.X, pady=(10, 0))
        tk.Label(current, text=f"当前：赛道「{self.edit_genre_var.get() or '未选'}」· 画面 {self.aspect_ratio_var.get()}"
                              f" · 去重 {self.dedup_level_var.get()} · 音画 {self.audio_match_var.get()}",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9), wraplength=980, justify=tk.LEFT).pack(anchor="w")
        if not names:
            tk.Label(preset, text="还没有预设。设好赛道/画面/去重参数后点击“保存为预设”。", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(6, 0))

        rework = self._card(self.page_frame, "⑤ 高相似重做（复盘）", "读取最新去重指纹报告，自动生成新的重做批次。")
        self._place_card(rework, 5, 0, 4)
        self._high_similarity_rework_panel(rework, config)

        self._edit_library_card(row=6)

    def _episode_rework_panel(self, parent: tk.Widget, config) -> None:
        if not config:
            tk.Label(parent, text="先选择项目。", bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
            return
        self._row(parent, "整集视频", self.episode_file_var, self.pick_episode_file)
        version_row = tk.Frame(parent, bg=CARD_BG)
        version_row.pack(fill=tk.X, pady=5)
        tk.Label(version_row, text="版本数量", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Spinbox(version_row, from_=1, to=50, textvariable=self.episode_versions_var, width=8).pack(side=tk.LEFT)
        tk.Label(version_row, text="条", bg=CARD_BG, fg=MUTED, font=(FONT, 9)).pack(side=tk.LEFT, padx=8)

        audio_row = tk.Frame(parent, bg=CARD_BG)
        audio_row.pack(fill=tk.X, pady=5)
        tk.Label(audio_row, text="音频模式", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Radiobutton(audio_row, text="保留原声", value="keep_original", variable=self.episode_audio_mode_var).pack(side=tk.LEFT)
        ttk.Radiobutton(audio_row, text="替换整条音频", value="replace_track", variable=self.episode_audio_mode_var).pack(side=tk.LEFT, padx=14)

        self._row(parent, "替换音频", self.episode_replacement_audio_var, self.pick_episode_replacement_audio)

        strength_row = tk.Frame(parent, bg=CARD_BG)
        strength_row.pack(fill=tk.X, pady=5)
        tk.Label(strength_row, text="去重强度", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(strength_row, textvariable=self.dedup_level_var, values=["基础", "标准", "增强"], state="readonly", width=10).pack(side=tk.LEFT)

        actions = tk.Frame(parent, bg=CARD_BG)
        actions.pack(fill=tk.X, pady=(12, 0))
        self._button(actions, "开始整集重组", self.episode_rework_action, kind="primary", requires_project=True).pack(side=tk.LEFT)
        self._button(actions, "打开整集重组目录", lambda: self.open_folder(self._load_config().root / DIR_EPISODE_REWORK), kind="ghost", requires_project=True).pack(side=tk.LEFT, padx=8)

    def _high_similarity_rework_panel(self, parent: tk.Widget, config) -> None:
        if not config:
            tk.Label(parent, text="先选择项目。", bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
            return
        try:
            selection = select_high_similarity_sources(config)
        except FileNotFoundError:
            tk.Label(parent, text="暂无去重指纹报告。", bg=CARD_BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w")
            return
        except Exception as exc:
            tk.Label(parent, text=f"指纹报告读取失败：{exc}", bg=CARD_BG, fg=ERROR, font=(FONT, 10)).pack(anchor="w")
            return

        high_count = len(selection.high_pairs)
        output_count = len(selection.high_outputs)
        color = ERROR if high_count else OK
        self._stat_strip(
            parent,
            [
                ("高相似组", str(high_count), color),
                ("涉及成片", str(output_count), color),
                ("待重做素材", str(len(selection.source_files)), color if high_count else OK),
                ("去重强度", "增强", ACCENT),
            ],
        )
        tk.Label(parent, text=f"最新报告：{selection.report_path.name}", bg=CARD_BG, fg=TEXT, font=(FONT, 9), wraplength=980, justify=tk.LEFT).pack(anchor="w", pady=(8, 4))
        if selection.high_pairs:
            for pair in selection.high_pairs[:4]:
                line = f"{pair.similarity:.4f}｜{Path(pair.left).name} / {Path(pair.right).name}"
                tk.Label(parent, text=line, bg=CARD_BG, fg=MUTED, font=(FONT, 9), wraplength=980, justify=tk.LEFT).pack(anchor="w")
            actions = tk.Frame(parent, bg=CARD_BG)
            actions.pack(fill=tk.X, pady=(10, 0))
            self._button(actions, "重新生成高相似成片", self.regenerate_high_similarity, kind="danger", requires_project=True).pack(side=tk.LEFT)
            self._button(actions, "打开原报告目录", lambda: self.open_folder(selection.report_path.parent), kind="secondary", requires_project=True).pack(side=tk.LEFT, padx=8)
        else:
            tk.Label(parent, text="当前最新报告未发现高相似。", bg=CARD_BG, fg=OK, font=(FONT, 10, "bold")).pack(anchor="w", pady=(6, 0))

    def switch_theme(self, name: str) -> None:
        if name == theme.active_theme_name():
            return
        try:
            theme.save_theme_name(name)
        except Exception as exc:
            messagebox.showerror("界面外观", f"保存主题失败：{exc}")
            return
        self.write_log(f"界面主题已切换为：{name}（重启后生效）")
        messagebox.showinfo("界面外观", f"已切换到“{name}”主题。\n重启软件后完全生效。")
        self.show_page("plugins")

    def _page_plugins(self) -> None:
        hub = self._card(self.page_frame, "下载与依赖中心", "所有需要下载的组件、模型、运行时和依赖环境都集中在本页：下方“模型库”“可下载运行产物”统一下载，“运行路径”配置 ffmpeg/ffprobe。")
        self._place_card(hub, 0, 0, 4)
        tk.Label(hub, text="轻量安装策略：安装包不含模型/大组件，按需在此下载启用。",
                 bg=CARD_BG, fg=MUTED, font=(FONT, 9), wraplength=980, justify=tk.LEFT).pack(anchor="w")

        actions = self._card(self.page_frame, "能力组件状态", "这里显示每项服务能力是否已准备好，技术来源放在报告里。")
        self._place_card(actions, 1, 0, 4)
        self._button(actions, "刷新能力清单", self.plugins).pack(side=tk.LEFT)
        self._button(actions, "能力自检", self.capability_check, kind="light").pack(side=tk.LEFT, padx=8)
        self._button(actions, "打开组件目录", lambda: self.open_folder(vendor_root()), kind="light").pack(side=tk.LEFT, padx=8)
        latest_check = version_root() / "docs" / "能力组件自检_latest.md"
        self._button(actions, "打开自检报告", lambda: self.open_file(latest_check), kind="ghost").pack(side=tk.LEFT, padx=8)

        appearance = self._card(self.page_frame, "界面外观（换肤）", "选择一套界面配色，重启软件后生效。")
        self._place_card(appearance, 2, 0, 4)
        current_theme = theme.active_theme_name()
        theme_row = tk.Frame(appearance, bg=CARD_BG)
        theme_row.pack(fill=tk.X, pady=(2, 0))
        for name, palette in theme.THEMES.items():
            selected = name == current_theme
            swatch = tk.Frame(theme_row, bg=SOFT_BG, highlightthickness=2, cursor="hand2",
                              highlightbackground=(palette["PRIMARY"] if selected else BORDER))
            swatch.pack(side=tk.LEFT, padx=(0, 10), pady=4)
            dots = tk.Frame(swatch, bg=palette["BG"])
            dots.pack(fill=tk.X, padx=8, pady=(8, 4))
            for color in (palette["PRIMARY"], palette["TEXT"], palette["MUTED"]):
                tk.Frame(dots, bg=color, width=16, height=16, highlightthickness=0).pack(side=tk.LEFT, padx=2, pady=6)
            label = f"● {name}" if selected else name
            tk.Label(swatch, text=label, bg=SOFT_BG, fg=(palette["PRIMARY"] if selected else TEXT),
                     font=(FONT, 9, "bold")).pack(padx=8, pady=(0, 8))
            for target in (swatch, dots, *dots.winfo_children()):
                target.bind("<Button-1>", lambda _e, n=name: self.switch_theme(n))

        tools = self._card(self.page_frame, "运行路径", "保存当前项目使用的本地工具路径。")
        self._place_card(tools, 3, 0, 4)
        self._row(tools, "ffmpeg", self.ffmpeg_var, lambda: self._pick_exe(self.ffmpeg_var, "选择 ffmpeg.exe"))
        self._row(tools, "ffprobe", self.ffprobe_var, lambda: self._pick_exe(self.ffprobe_var, "选择 ffprobe.exe"))
        self._button(tools, "保存路径", self.save_tool_paths, requires_project=True).pack(anchor="e", pady=(8, 0))

        downloader = self._card(self.page_frame, "素材链接下载", "使用授权下载组件下载已授权的视频素材并自动入库。")
        self._place_card(downloader, 4, 0, 4)
        self._row(downloader, "视频链接", self.download_url_var)
        target_row = tk.Frame(downloader, bg=CARD_BG)
        target_row.pack(fill=tk.X, pady=5)
        tk.Label(target_row, text="保存位置", bg=CARD_BG, fg=TEXT, font=(FONT, 9, "bold"), width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Combobox(target_row, textvariable=self.download_target_var, values=["原始视频", "自动剪辑输入"], state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._button(downloader, "下载并入库", self.download_url, kind="primary", requires_project=True).pack(anchor="e", pady=(8, 0))

        model_card = self._card(self.page_frame, "模型库", "安装包保持轻量；需要本地语音转写时，在这里下载运行时和模型。")
        self._place_card(model_card, 5, 0, 4)
        whisper = whisper_environment()
        tk.Label(model_card, text=whisper.message, bg=CARD_BG, fg=OK if whisper.ready else WARN, font=(FONT, 9), wraplength=960, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))
        self._model_library_rows(model_card)
        model_actions = tk.Frame(model_card, bg=CARD_BG)
        model_actions.pack(fill=tk.X, pady=(8, 0))
        self._button(model_actions, "下载 tiny 主模型", lambda: self.install_whisper_runtime_action("tiny"), kind="primary").pack(side=tk.LEFT, padx=(0, 8))
        self._button(model_actions, "下载 base 增强模型", lambda: self.install_whisper_runtime_action("base"), kind="light").pack(side=tk.LEFT, padx=(0, 8))
        self._button(model_actions, "下载 small 增强模型", lambda: self.install_whisper_runtime_action("small"), kind="light").pack(side=tk.LEFT, padx=(0, 8))
        self._button(model_actions, "打开模型目录", lambda: self.open_folder(whisper_models_dir()), kind="ghost").pack(side=tk.LEFT)

        statuses = plugin_statuses()
        runtime_items = [item for item in statuses if item.get("classification") == "runtime_download"]

        runtime = self._card(self.page_frame, "可下载运行产物", "官方已有可下载或可安装的运行产物；下载后仍以能力自检为准。")
        self._place_card(runtime, 6, 0, 4)
        if not runtime_items:
            self._empty_state(runtime, "暂无可直接下载启用的组件。")
        for item in runtime_items:
            self._component_row(runtime, item)

    def _component_row(self, parent: tk.Widget, item: dict) -> None:
        ready = item.get("entry_ready") or item.get("python_import_ready") or item.get("path_ready")
        downloaded = item.get("downloaded")
        tier = item.get("tier", "reference")
        classification = item.get("classification", "source_reference")
        if ready:
            status_text, status_level = "可用", "success"
        elif classification == "architecture_reference":
            status_text, status_level = "架构参考", "info"
        elif downloaded and tier == "reference":
            status_text, status_level = "源码已就绪，待编译/依赖", "info"
        elif downloaded:
            status_text, status_level = "已下载，待启用", "warning"
        elif classification == "runtime_download":
            status_text, status_level = "待下载或待放置", "warning"
        else:
            status_text, status_level = "参考源码未下载", "info"

        row = tk.Frame(parent, bg=SOFT_BG, highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill=tk.X, pady=6)
        head = tk.Frame(row, bg=SOFT_BG)
        head.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(head, text=item.get("name", ""), bg=SOFT_BG, fg=TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        ui.status_chip(head, status_text, status_level).pack(side=tk.LEFT, padx=8)
        if item.get("classification_reason"):
            tk.Label(row, text=item.get("classification_reason", ""), bg=SOFT_BG, fg=MUTED, font=(FONT, 9), wraplength=1040, justify=tk.LEFT).pack(anchor="w", padx=12)
        tk.Label(row, text=item.get("business_goal", ""), bg=SOFT_BG, fg=MUTED, font=(FONT, 9), wraplength=1040, justify=tk.LEFT).pack(anchor="w", padx=12)
        tk.Label(row, text=item.get("install_note", ""), bg=SOFT_BG, fg=MUTED, font=(FONT, 9), wraplength=1040, justify=tk.LEFT).pack(anchor="w", padx=12, pady=(2, 0))
        action = tk.Frame(row, bg=SOFT_BG)
        action.pack(fill=tk.X, padx=12, pady=(8, 10))
        if item.get("download_url") and classification == "runtime_download":
            self._button(action, "下载", lambda key=item["key"]: self.download_component_action(key), kind="secondary").pack(side=tk.LEFT)

    def _model_project_root(self) -> Path | None:
        config = self._config_or_none()
        return config.root if config else None

    def _model_library_rows(self, parent: tk.Widget) -> None:
        self.model_status_widgets = {}
        for status in model_library_statuses(self._model_project_root()):
            row = tk.Frame(parent, bg=SOFT_BG, highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill=tk.X, pady=4)
            head = tk.Frame(row, bg=SOFT_BG)
            head.pack(fill=tk.X, padx=12, pady=(8, 2))
            tk.Label(head, text=status.name, bg=SOFT_BG, fg=TEXT, font=(FONT, 10, "bold"), width=14, anchor="w").pack(side=tk.LEFT)
            text, level = self.model_download_state.get(status.key, (status.label, status.level))
            chip = ui.status_chip(head, text, level)
            chip.pack(side=tk.LEFT, padx=8)
            self.model_status_widgets[status.key] = chip
            detail = f"{status.filename} | {status.path} | {status.detail}"
            tk.Label(row, text=detail, bg=SOFT_BG, fg=MUTED, font=(FONT, 9), wraplength=1040, justify=tk.LEFT).pack(anchor="w", padx=12, pady=(0, 8))

    def _set_model_download_state(self, key: str, text: str, level: str = "info") -> None:
        self.model_download_state[key] = (text, level)
        widget = self.model_status_widgets.get(key)
        if widget:
            bg, fg = ui.CHIP_STYLES.get(level, ui.CHIP_STYLES["info"])
            widget.configure(text=text, bg=bg, fg=fg)

    def _folder_buttons(self, parent: tk.Widget, names: list[str]) -> None:
        wrap = tk.Frame(parent, bg=CARD_BG)
        wrap.pack(fill=tk.X)
        for name in names:
            self._button(wrap, name, lambda folder=name: self.open_folder(self._load_config().root / folder), kind="light").pack(side=tk.LEFT, padx=(0, 8), pady=4)

    def create_project(self) -> None:
        drama = self.drama_var.get().strip() or self.name_var.get().strip()
        config = create_project(self.root_var.get(), self.name_var.get(), drama_name=drama)
        self.project_var.set(config.project_root)
        self.name_var.set(config.project_name)
        self.drama_var.set(config.drama_name)
        self.write_log(f"项目已创建：{config.project_root}")
        self.show_page("material")

    def set_workflow_mode(self, mode: str) -> None:
        config = self._load_config()
        config.workflow_mode = "full" if mode == "full" else "simple"
        save_config(config)
        self.workflow_full_var.set(config.workflow_mode == "full")
        self.write_log(f"项目模式已切换：{'精修模式' if config.workflow_mode == 'full' else '日常生产'}")
        self._rebuild_nav()
        self.show_page("dashboard" if self.current_page in {"review", "health"} else self.current_page)

    def import_assets_action(self, role: str) -> None:
        if role not in ASSET_TARGETS:
            messagebox.showerror("素材入库", f"未知素材类型：{role}")
            return
        _target_dir, allowed = ASSET_TARGETS[role]
        suffixes = " ".join(f"*{item}" for item in sorted(allowed))
        files = filedialog.askopenfilenames(title=f"选择{role}", filetypes=[(role, suffixes), ("全部文件", "*.*")])
        if not files:
            return
        self._set_material_path(role, f"已选 {len(files)} 个文件：{Path(files[0]).parent}")
        self.run_background(
            f"素材入库：{role}",
            lambda: f"已导入 {len(import_assets(self._load_config(), role, list(files)))} 个文件",
            lambda: self.show_page("material"),
        )

    def import_assets_folder_action(self, role: str) -> None:
        if role not in ASSET_TARGETS:
            messagebox.showerror("素材入库", f"未知素材类型：{role}")
            return
        folder = filedialog.askdirectory(title=f"选择{role}文件夹")
        if not folder:
            return
        self._set_material_path(role, f"文件夹：{folder}")
        subdir_matches = count_matching_files_in_subdirs(role, folder)
        include_subdirs = False
        if subdir_matches:
            include_subdirs = messagebox.askyesno("素材入库", f"子文件夹中还发现 {subdir_matches} 个文件，是否一并导入？")

        def action() -> str:
            result = import_material_folder(self._load_config(), role, Path(folder), include_subdirs=include_subdirs)
            if result.imported == 0 and not result.failed:
                return f"该文件夹没有找到可导入的{role}文件。跳过 {result.skipped_non_media} 个无关文件。"
            failed = f"，失败 {len(result.failed)} 个" if result.failed else ""
            if result.failed:
                failed += "；示例：" + "；".join(result.failed[:3])
            return f"导入 {result.imported} 个，跳过 {result.skipped_non_media} 个{failed}。"

        self.run_background(f"文件夹导入：{role}", action, lambda: self.show_page("material"))

    def inventory(self) -> None:
        def action() -> str:
            csv_path, json_path = write_inventory(self._load_config())
            return f"素材清单已生成：{csv_path}；{json_path}"

        self.run_background("生成素材清单", action)

    def _auto_cut_settings(self) -> AutoCutSettings:
        # 前置赛道/画面参数：非空赛道会让 run_strategy_auto_edit 先 apply_genre 自适应画风，
        # 画面比例与音画对齐模式由用户在前置面板选择，注入渲染 -vf。
        return AutoCutSettings(
            strategy=self.auto_strategy_var.get(),
            target_seconds=float(self.edit_target_var.get()),
            clip_seconds=float(self.edit_clip_var.get()),
            silence_db=float(self.silence_db_var.get()),
            min_silence=float(self.min_silence_var.get()),
            scene_threshold=float(self.scene_threshold_var.get()),
            keywords=self.auto_keywords_var.get(),
            genre=self.edit_genre_var.get().strip(),
            aspect_ratio=self.aspect_ratio_var.get() or "9:16 竖屏",
            audio_match_mode=self.audio_match_var.get() or "裁剪多余画面",
        )

    def save_preset_action(self) -> None:
        from .edit_presets import ProductionPreset, save_preset

        name = simpledialog.askstring("保存预设", "预设名称：", parent=self)
        if not name or not name.strip():
            return
        preset = ProductionPreset(
            name=name.strip(),
            genre=self.edit_genre_var.get().strip() or "影视解说",
            aspect_ratio=self.aspect_ratio_var.get() or "9:16 竖屏",
            dedup_level=self.dedup_level_var.get() or "标准",
            audio_match_mode=self.audio_match_var.get() or "裁剪多余画面",
        )
        try:
            save_preset(preset)
        except Exception as exc:
            messagebox.showerror("保存预设", f"保存失败：{exc}")
            return
        self.preset_var.set(preset.name)
        self.write_log(f"已保存预设：{preset.name}（赛道{preset.genre}/画面{preset.aspect_ratio}/去重{preset.dedup_level}）")
        messagebox.showinfo("保存预设", f"已保存预设“{preset.name}”。")
        self.show_page("remix")

    def apply_preset_action(self) -> None:
        from .edit_presets import get_preset

        name = self.preset_var.get().strip()
        if not name:
            messagebox.showinfo("应用预设", "请选择一个预设。")
            return
        preset = get_preset(name)
        if not preset:
            messagebox.showwarning("应用预设", f"未找到预设“{name}”。")
            return
        self.edit_genre_var.set(preset.genre)
        self.aspect_ratio_var.set(preset.aspect_ratio)
        self.dedup_level_var.set(preset.dedup_level)
        self.audio_match_var.set(preset.audio_match_mode)
        self.write_log(f"已应用预设：{name}")
        messagebox.showinfo("应用预设", f"已套用预设“{name}”：赛道{preset.genre}·画面{preset.aspect_ratio}·去重{preset.dedup_level}。")
        self.show_page("remix")

    def delete_preset_action(self) -> None:
        from .edit_presets import delete_preset

        name = self.preset_var.get().strip()
        if not name:
            messagebox.showinfo("删除预设", "请选择一个预设。")
            return
        if not messagebox.askyesno("删除预设", f"确认删除预设“{name}”？"):
            return
        try:
            delete_preset(name)
        except Exception as exc:
            messagebox.showerror("删除预设", f"删除失败：{exc}")
            return
        self.preset_var.set("")
        self.write_log(f"已删除预设：{name}")
        self.show_page("remix")

    def auto_cut(self) -> None:
        settings = self._auto_cut_settings()  # 主线程读取 Tk 变量

        def action() -> str:
            result = run_strategy_auto_edit(self._load_config(), settings,
                                            progress=lambda d, t: self.set_progress(d, t, "自动剪辑"))
            preview = f"，预览：{result.edit.preview_path}" if result.edit.preview_path else ""
            return f"{result.strategy} 生成 {result.segments} 个计划分镜，素材包：{result.edit.work_dir}{preview}"

        genre_note = f"、赛道画风“{settings.genre}”" if settings.genre else ""
        self.run_background(
            "一键自动剪辑",
            action,
            lambda: self.show_page("remix"),
            confirm=f"将按“{settings.strategy}”策略、目标 {settings.target_seconds} 秒{genre_note}、"
                    f"画面 {settings.aspect_ratio}、音画“{settings.audio_match_mode}”自动剪辑。确认开始？",
        )

    def recommend_auto_cut_strategy(self) -> None:
        goal = simpledialog.askstring("按目标推荐策略", "请输入剪辑目标，例如：60秒冲突反转高光，或去掉口播停顿。")
        if not goal:
            return
        recommendation = recommend_auto_cut_settings(self._load_config(), goal)
        self.auto_strategy_var.set(recommendation.settings.strategy)
        self.edit_target_var.set(recommendation.settings.target_seconds)
        self.edit_clip_var.set(recommendation.settings.clip_seconds)
        self.silence_db_var.set(recommendation.settings.silence_db)
        self.min_silence_var.set(recommendation.settings.min_silence)
        self.scene_threshold_var.set(recommendation.settings.scene_threshold)
        self.auto_keywords_var.set(recommendation.settings.keywords)
        report = f"\n报告：{recommendation.report_json}" if recommendation.report_json else ""
        self.write_log(f"已推荐剪辑策略：{recommendation.settings.strategy}\n{recommendation.reason}{report}")
        self.show_page("remix")

    def auto_edit(self) -> None:
        script = self.edit_script_var.get().strip() or None
        target_seconds = float(self.edit_target_var.get())
        clip_seconds = float(self.edit_clip_var.get())

        def action() -> str:
            result = run_auto_edit(
                self._load_config(),
                script_path=script,
                target_seconds=target_seconds,
                clip_seconds=clip_seconds,
                progress=lambda d, t: self.set_progress(d, t, "按分镜表生成"),
            )
            preview = f"，预览：{result.preview_path}" if result.preview_path else ""
            return f"{result.selected_count} 个分镜，素材包：{result.work_dir}{preview}"

        self.run_background(
            "按分镜表生成",
            action,
            lambda: self.show_page("remix"),
            confirm="将按分镜表生成剪辑素材包。确认开始？",
        )

    def detect_scene_action(self) -> None:
        def action() -> str:
            config = self._load_config()
            videos = iter_media(config.root / DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS)
            if not videos:
                raise FileNotFoundError(f"{DIR_AUTO_EDIT_INPUT} 中没有可检测视频。")
            results = [detect_scenes(config, video, threshold=float(self.scene_threshold_var.get())) for video in videos]
            engines = "、".join(sorted({result.engine for result in results}))
            scene_total = sum(len(result.scenes) for result in results)
            return f"场景检测完成：共 {len(results)} 个视频，引擎 {engines}，场景合计 {scene_total} 个。\n产物目录：{config.root / DIR_SCENE_DETECT}"

        self.run_background("场景检测", action)

    def install_whisper_runtime_action(self, model: str = "tiny") -> None:
        model_label = {"tiny": "tiny 主模型", "base": "base 增强模型", "small": "small 增强模型"}.get(model, model)
        if not messagebox.askyesno("模型库下载", f"将下载 whisper.cpp Windows 运行包和 {model_label}。文件较大，是否继续？"):
            return

        def progress(done: int, total: int | None) -> None:
            if total:
                percent = min(100, int(done * 100 / max(total, 1)))
                text = f"下载中 {percent}%"
            else:
                text = f"下载中 {done / 1024 / 1024:.1f} MB"
            self.after(0, lambda: self._set_model_download_state("whisper_cli", text, "info"))
            self.after(0, lambda: self._set_model_download_state(model, text, "info"))
            self.after(0, lambda: self.write_log(f"{model_label} {text}"))

        def action() -> str:
            self.after(0, lambda: self._set_model_download_state("whisper_cli", "校验中", "info"))
            self.after(0, lambda: self._set_model_download_state(model, "校验中", "info"))
            result = install_whisper_runtime(model=model, with_model=True, project_root=self._model_project_root(), progress=progress)
            if not result.ok:
                self.after(0, lambda: self._set_model_download_state(model, "文件损坏", "danger"))
                raise RuntimeError(result.message)
            self.model_download_state.pop("whisper_cli", None)
            self.model_download_state.pop(model, None)
            return result.message

        self.run_background("模型库下载", action, lambda: self.show_page("plugins"))

    def dub_assemble_action(self) -> None:
        audio_dir = self.dub_audio_dir_var.get().strip()
        video_dir = self.dub_video_dir_var.get().strip()
        if not audio_dir or not video_dir:
            messagebox.showwarning("配音对齐成片", "请先选择配音包文件夹和分镜视频文件夹。")
            return

        def action() -> str:
            config = self._load_config()
            # 画面尺寸：按所选比例设定输出宽高，dub_sync 会 scale+pad 到该尺寸
            width, height = self.DUB_ASPECTS.get(self.dub_aspect_var.get(), (config.edit.output_width, config.edit.output_height))
            config.edit.output_width = width
            config.edit.output_height = height
            result = dub_assemble(
                config,
                audio_dir=Path(audio_dir),
                video_dir=Path(video_dir),
                name=self.dub_name_var.get().strip() or None,
                burn_subtitle=self.dub_subtitle_var.get(),
                progress=lambda d, t: self.set_progress(d, t, "配音对齐"),
            )
            config.dub_last_audio_dir = str(Path(audio_dir))
            config.dub_last_video_dir = str(Path(video_dir))
            save_config(config)
            counts = Counter(segment.strategy for segment in result.segments)
            successful = sum(1 for segment in result.segments if segment.segment_file)
            strategy_text = "，".join(f"{key}={value}" for key, value in sorted(counts.items())) or "无"
            message = (
                f"配音对齐成片完成：{successful}/{len(result.segments)} 段，约 {result.total_seconds:.1f} 秒。"
                f"\n策略：{strategy_text}"
                f"\n未匹配配音 {len(result.unmatched_audio)}，未匹配视频 {len(result.unmatched_video)}。"
                f"\n成片：{result.output_path}"
                f"\n对齐表：{result.plan_csv}"
            )
            if not self.dub_auto_produce_var.get():
                return message

            copies, dedup_label = self._dub_production_options(config)
            config.workflow_mode = "simple"
            config.recipe.copies_per_source = copies
            config.recipe.mode = self.mode_var.get()
            config.recipe.dedup_level = {"基础": "light", "标准": "balanced", "增强": "strong"}.get(dedup_label, "balanced")
            save_config(config)
            try:
                production = produce(
                    config,
                    source=result.output_path,
                    copies=copies,
                    dedup_level=dedup_label,
                    name="配音对齐产线",
                    progress=lambda d, t: self.set_progress(d, t, "配音后矩阵生产"),
                )
            except Exception as exc:
                reason, _suggestion = self._human_error(exc)
                return (
                    f"{message}"
                    f"\n成片已生成于 {result.output_path}，矩阵生产失败：{reason}，可到矩阵生产页手动重试。"
                )
            ready_dir = production.ready_dir or production.batch_dir
            return (
                f"{message}"
                f"\n矩阵生产完成：账号 {production.account_count}，合格 {len(production.ready_videos)} 条，遗留 high {production.remaining_high_count} 条。"
                f"\n合格目录：{ready_dir}"
                f"\n回流模板：{production.feedback_template or '-'}"
                f"\n产线报告：{production.report_md}"
            )

        self.run_background("配音对齐成片", action, lambda: self.show_page("dub"))

    def render(self) -> None:
        copies = int(self.account_count_var.get())
        mode = self.mode_var.get()
        dedup_display = self.dedup_level_var.get()
        render_source = self.render_source_var.get()
        force_distinct = bool(self.force_distinct_intro_var.get())

        def action() -> str:
            config = self._load_config()
            config.recipe.copies_per_source = copies
            config.recipe.mode = mode
            config.recipe.dedup_level = {"基础": "light", "标准": "balanced", "增强": "strong"}.get(dedup_display, "balanced")
            config.recipe.force_distinct_intro = force_distinct
            save_config(config)
            source_files = None
            source_label = "A 原始视频"
            if render_source == "最新自动剪辑分镜":
                selected = latest_auto_edit_selected_dir(config)
                if not selected:
                    raise FileNotFoundError("还没有自动剪辑分镜，请先在自动剪辑页面生成素材包。")
                source_files = iter_media(selected, VIDEO_EXTENSIONS)
                source_label = "自动剪辑分镜"
            outputs = render_project(config, copies=copies, source_files=source_files, source_label=source_label,
                                     progress=lambda d, t: self.set_progress(d, t, "二创成片"))
            return f"生成 {len(outputs)} 条，已进入中间结果目录。"

        self.run_background(
            "二创成片",
            action,
            lambda: self.show_page("remix"),
            confirm=f"将按 {copies} 份生成中间结果成片。确认开始？",
        )

    def episode_rework_action(self) -> None:
        video_text = self.episode_file_var.get().strip()
        if not video_text:
            self.pick_episode_file()
            video_text = self.episode_file_var.get().strip()
        if not video_text:
            return
        message: dict[str, str] = {}
        versions = int(self.episode_versions_var.get())
        audio_mode = self.episode_audio_mode_var.get()
        replacement_audio = self.episode_replacement_audio_var.get().strip() or None
        mode = self.mode_var.get()
        dedup_display = self.dedup_level_var.get()

        def action() -> str:
            config = self._load_config()
            if versions < 1 or versions > 50:
                raise ValueError("版本数量需要在 1 到 50 之间。")
            if audio_mode == "replace_track" and not replacement_audio:
                raise FileNotFoundError("选择替换整条音频时，请先选择替换音频文件。")
            config.recipe.mode = mode
            config.recipe.dedup_level = {"基础": "light", "标准": "balanced", "增强": "strong"}.get(dedup_display, "balanced")
            save_config(config)
            result = episode_rework(
                config,
                video=Path(video_text),
                versions=versions,
                audio_mode=audio_mode,
                replacement_audio=Path(replacement_audio) if replacement_audio else None,
                dedup_level=config.recipe.dedup_level,
                mode=config.recipe.mode,
                name=Path(video_text).stem,
                progress=lambda d, t: self.set_progress(d, t, "整集重组"),
            )
            summary = (
                f"段数 {result.scene_count}，版本 {len(result.output_paths)}，失败段 {len(result.failed_segments)}。"
                f"\n输出目录：{result.batch_dir}"
            )
            message["text"] = summary
            return summary

        def done() -> None:
            self.show_page("remix")
            messagebox.showinfo("整集重组完成", message.get("text", "整集重组已完成。"))

        self.run_background(
            "整集重组生产",
            action,
            done,
            confirm=f"将对整集切片并生成 {versions} 个重组版本，耗时较长。确认开始？",
        )

    def produce_matrix(self) -> None:
        # 在主线程快照 Tk 变量，杜绝在工作线程访问 Tcl 导致的卡屏/卡死。
        copies = int(self.account_count_var.get())
        mode = self.mode_var.get()
        dedup_display = self.dedup_level_var.get()
        render_source = self.render_source_var.get()
        force_distinct = bool(self.force_distinct_intro_var.get())
        resume = bool(self.resume_var.get())

        def action() -> str:
            config = self._load_config()
            config.workflow_mode = "simple"
            config.recipe.copies_per_source = copies
            config.recipe.mode = mode
            config.recipe.dedup_level = {"基础": "light", "标准": "balanced", "增强": "strong"}.get(dedup_display, "balanced")
            config.recipe.force_distinct_intro = force_distinct
            save_config(config)
            source = None
            input_dir = None
            if render_source == "最新自动剪辑分镜":
                selected = latest_auto_edit_selected_dir(config)
                if not selected:
                    raise FileNotFoundError("还没有自动剪辑分镜，请先在自动剪辑页面生成素材包。")
                input_dir = selected
            else:
                originals = iter_media(config.root / DIR_A, VIDEO_EXTENSIONS)
                if not originals:
                    raise FileNotFoundError("原始视频目录还没有可用成片。")
                source = originals[0]
            result = produce(
                config,
                source=source,
                input_dir=input_dir,
                copies=copies,
                dedup_level=dedup_display,
                name="矩阵产线",
                resume=resume,
                progress=lambda d, t: self.set_progress(d, t, "矩阵生产"),
            )
            return (
                f"账号 {result.account_count}，合格 {len(result.ready_videos)} 条，遗留 high {result.remaining_high_count} 条。"
                f"\n合格目录：{result.ready_dir}"
                f"\n回流模板：{result.feedback_template or '-'}"
                f"\n产线报告：{result.report_md}"
            )

        self.run_background(
            "短剧矩阵一键生产",
            action,
            lambda: self.show_page("remix"),
            confirm=f"将按 {copies} 个账号、去重强度“{dedup_display}”批量生成成片，耗时可能较长。确认开始？",
        )

    def regenerate_high_similarity(self) -> None:
        def action() -> str:
            result = render_high_similarity_replacements(self._load_config(), dedup_level="strong",
                                                         progress=lambda d, t: self.set_progress(d, t, "高相似重做"))
            fallback = f"\n路径兜底：{len(result.missing_sources)} 条" if result.missing_sources else ""
            comparison = f"\n对比结论：{result.comparison_conclusion}" if result.comparison_conclusion else ""
            comparison_path = f"\n对比报告：{result.comparison_md}" if result.comparison_md else ""
            return (
                f"高相似重做 {len(result.rendered)} 条，原 high 组 {result.high_pair_count} 组。"
                f"\n输出目录：{result.output_dir}"
                f"\n重做报告：{result.summary_md}"
                f"{comparison}"
                f"{comparison_path}"
                f"{fallback}"
            )

        self.run_background(
            "高相似成片重做",
            action,
            lambda: self.show_page("remix"),
            confirm="将读取最新去重指纹报告，对 high 相似组重新生成新批次。确认开始？",
        )

    def build_batch_archive_action(self) -> None:
        def action() -> str:
            result = build_batch_archive(self._load_config())
            return (
                f"成品批次档案已生成：{result.batch_id}"
                f"\nJSON：{result.archive_json}"
                f"\nMD：{result.archive_md}"
                f"\n缺失项：{len(result.missing_artifacts)}"
            )

        self.run_background("生成批次档案", action, lambda: self.show_page("remix"))

    def export_capcut(self) -> None:
        def action() -> str:
            result = export_capcut_draft_package(self._load_config(), create_real_draft=True)
            return f"{result.message} {result.package_dir}"

        self.run_background("导出剪映草稿包", action)

    def plugins(self) -> None:
        manifest = write_vendor_manifest()
        lines = [f"能力清单已刷新：{manifest}"]
        for item in plugin_statuses():
            ready = item.get("entry_ready") or item.get("python_import_ready") or item.get("path_ready")
            if ready:
                state = "可用"
            elif item.get("tier") == "reference" and item.get("downloaded"):
                state = "源码已就绪，待编译/依赖"
            elif item.get("downloaded"):
                state = "已下载，待启用"
            else:
                state = "待下载" if item.get("tier") == "runtime" else "参考源码未下载"
            lines.append(f"{item['name']}：{state}")
        self.write_log("\n".join(lines))
        self.show_page("plugins")

    def download_component_action(self, key: str) -> None:
        item = next((plugin for plugin in plugin_statuses() if plugin.get("key") == key), None)
        name = item.get("name", key) if item else key
        if not messagebox.askyesno("下载组件", f"将从 GitHub 下载“{name}”。国内网络可能失败，是否继续？"):
            return

        def progress(done: int, total: int | None) -> None:
            done_mb = done / 1024 / 1024
            if total:
                total_mb = total / 1024 / 1024
                text = f"{name} 下载中：{done_mb:.1f}/{total_mb:.1f} MB"
            else:
                text = f"{name} 下载中：{done_mb:.1f} MB"
            self.after(0, lambda: self.write_log(text))

        def action() -> str:
            result = download_component(key, progress=progress)
            ready = result.status.get("entry_ready") or result.status.get("python_import_ready") or result.status.get("path_ready")
            state = "可用" if ready else ("已下载，待启用" if result.status.get("tier") == "runtime" else "源码已就绪，待编译/依赖")
            return f"{result.message}\n目标目录：{result.target_dir}\n当前状态：{state}"

        self.run_background(f"下载组件：{name}", action, lambda: self.show_page("plugins"))

    def capability_check(self) -> None:
        def action() -> str:
            result = write_capability_report(version_root() / "docs")
            return "\n".join(
                [
                    format_capability_report(result.report),
                    f"Markdown报告：{result.markdown_path}",
                    f"JSON报告：{result.json_path}",
                    f"明细CSV：{result.csv_path}",
                ]
            )

        self.run_background("能力组件自检", action, lambda: self.show_page("plugins"))

    def download_url(self) -> None:
        url = self.download_url_var.get().strip()
        if not url:
            messagebox.showinfo("素材链接下载", "请先填写视频链接。")
            return

        def action() -> str:
            result = download_video(self._load_config(), url, target=self.download_target_var.get())
            if not result.ok:
                raise RuntimeError(result.message)
            return f"下载完成：{result.output_dir}"

        self.run_background("素材链接下载", action, lambda: self.show_page("plugins"))

    def save_tool_paths(self) -> None:
        def action() -> str:
            config = self._load_config()
            config.tools.ffmpeg = self.ffmpeg_var.get().strip()
            config.tools.ffprobe = self.ffprobe_var.get().strip()
            config.tools.local_editor_exe = self.local_editor_exe_var.get().strip()
            config.tools.publish_tool_exe = self.publish_exe_var.get().strip()
            save_config(config)
            return "路径已保存。"

        self.run_background("保存路径", action)


def main() -> None:
    app = WorkbenchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
