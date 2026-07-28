from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk


FONT = "Microsoft YaHei UI"

# 语义色在所有主题下保持一致（good/warning/critical 不随主题变，避免误读）。
SUCCESS = "#34C77B"
WARNING = "#F5B942"
DANGER = "#E5534B"
INFO = "#7E8CE0"

SUCCESS_BG = "#173527"
WARNING_BG = "#3A2F17"
DANGER_BG = "#3A1E1F"
INFO_BG = "#252A48"

# 四套可切换的界面主题（均为深色，仅中性色偏向与主强调色不同）。
THEMES: dict[str, dict[str, str]] = {
    "水星深蓝": {
        "BG": "#16181D", "PANEL": "#1F2229", "BORDER": "#2C3038", "HOVER": "#262A33",
        "PRIMARY": "#4FC3F7", "PRIMARY_ACTIVE": "#3BA9DC",
        "TEXT": "#E8EAED", "MUTED": "#9AA0A6", "DISABLED": "#5F6368",
    },
    "石墨紫": {
        "BG": "#17171C", "PANEL": "#201F27", "BORDER": "#312F3B", "HOVER": "#2A2833",
        "PRIMARY": "#A78BFA", "PRIMARY_ACTIVE": "#8B6EE6",
        "TEXT": "#ECEAF3", "MUTED": "#A09AAE", "DISABLED": "#635C6E",
    },
    "松墨绿": {
        "BG": "#12181A", "PANEL": "#1B2427", "BORDER": "#294044", "HOVER": "#233134",
        "PRIMARY": "#2DD4BF", "PRIMARY_ACTIVE": "#20B3A0",
        "TEXT": "#E6EEEC", "MUTED": "#93A6A2", "DISABLED": "#59706C",
    },
    "暖夜橙": {
        "BG": "#1B1613", "PANEL": "#26201B", "BORDER": "#3B3025", "HOVER": "#33291F",
        "PRIMARY": "#FB923C", "PRIMARY_ACTIVE": "#E07B26",
        "TEXT": "#F1E9E2", "MUTED": "#B0A296", "DISABLED": "#6E6152",
    },
}

DEFAULT_THEME = "水星深蓝"


def _settings_path() -> Path:
    return Path.home() / ".shuixing" / "ui_settings.json"


def active_theme_name() -> str:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        name = str(data.get("theme", DEFAULT_THEME))
        return name if name in THEMES else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def save_theme_name(name: str) -> None:
    if name not in THEMES:
        raise ValueError(f"未知主题：{name}")
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": name}, ensure_ascii=False), encoding="utf-8")


def apply_palette(name: str) -> None:
    """把指定主题的调色板写入本模块全局，供下游在导入时快照。"""
    palette = THEMES.get(name) or THEMES[DEFAULT_THEME]
    globals().update(palette)


# 模块导入时即按已保存选择应用调色板（在 widgets/app 快照 theme.* 之前完成）。
apply_palette(active_theme_name())


def font(size: int = 11, weight: str = "normal") -> tuple[str, int, str]:
    return (FONT, size, weight)


def apply_theme(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=font(11), background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("TEntry", fieldbackground=HOVER, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=6)
    style.configure("TSpinbox", fieldbackground=HOVER, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=6)
    style.configure("TCombobox", fieldbackground=HOVER, background=HOVER, foreground=TEXT, bordercolor=BORDER, arrowcolor=TEXT, padding=6)
    style.map("TCombobox", fieldbackground=[("readonly", HOVER)], foreground=[("readonly", TEXT)])
    style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("disabled", DISABLED), ("active", TEXT)])
    style.configure("TRadiobutton", background=PANEL, foreground=TEXT)
    style.map("TRadiobutton", background=[("active", PANEL)], foreground=[("disabled", DISABLED), ("active", TEXT)])
    style.configure("Horizontal.TProgressbar", troughcolor=HOVER, background=PRIMARY, bordercolor=BORDER)
    style.configure(
        "Treeview",
        background=PANEL,
        fieldbackground=PANEL,
        foreground=TEXT,
        rowheight=28,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
    )
    style.configure("Treeview.Heading", background=HOVER, foreground=TEXT, font=font(10, "bold"), relief="flat")
    style.map("Treeview", background=[("selected", HOVER)], foreground=[("selected", TEXT)])
    style.configure("Vertical.TScrollbar", background=HOVER, troughcolor=BG, arrowcolor=TEXT, bordercolor=BORDER)
    return style

