from __future__ import annotations

import tkinter as tk

from . import theme


BUTTON_STYLES = {
    "primary": (theme.PRIMARY, "#FFFFFF", theme.PRIMARY_ACTIVE, theme.PRIMARY),
    "secondary": (theme.PANEL, theme.TEXT, theme.HOVER, theme.BORDER),
    "danger": (theme.DANGER, "#FFFFFF", "#C94740", theme.DANGER),
    "ghost": (theme.BG, theme.MUTED, theme.HOVER, theme.BG),
    "light": (theme.HOVER, theme.TEXT, theme.PANEL, theme.BORDER),
}

CHIP_STYLES = {
    "success": (theme.SUCCESS_BG, theme.SUCCESS),
    "warning": (theme.WARNING_BG, theme.WARNING),
    "danger": (theme.DANGER_BG, theme.DANGER),
    "info": (theme.INFO_BG, theme.INFO),
}


def button(parent: tk.Widget, text: str, command, kind: str = "primary") -> tk.Button:
    bg, fg, active, border = BUTTON_STYLES.get(kind, BUTTON_STYLES["primary"])
    return tk.Button(
        parent,
        text=text,
        command=command,
        bd=0,
        highlightthickness=1,
        highlightbackground=border,
        relief=tk.FLAT,
        padx=14,
        pady=7,
        bg=bg,
        fg=fg,
        activebackground=active,
        activeforeground=fg,
        font=theme.font(10, "bold"),
        cursor="hand2",
    )


class Card(tk.Frame):
    def __init__(self, parent: tk.Widget, title: str, subtitle: str = "") -> None:
        super().__init__(parent, bg=theme.PANEL, highlightbackground=theme.BORDER, highlightthickness=1)
        self.configure(padx=16, pady=14)
        tk.Label(self, text=title, bg=theme.PANEL, fg=theme.TEXT, font=theme.font(13, "bold")).pack(anchor="w")
        if subtitle:
            tk.Label(
                self,
                text=subtitle,
                bg=theme.PANEL,
                fg=theme.MUTED,
                font=theme.font(10),
                wraplength=520,
                justify=tk.LEFT,
            ).pack(anchor="w", pady=(4, 0))
        self.body = tk.Frame(self, bg=theme.PANEL)
        self.body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))


def status_chip(parent: tk.Widget, text: str, level: str = "info") -> tk.Label:
    bg, fg = CHIP_STYLES.get(level, CHIP_STYLES["info"])
    return tk.Label(parent, text=text, bg=bg, fg=fg, font=theme.font(10, "bold"), padx=10, pady=4)


def stat_number(parent: tk.Widget, label: str, value: str, level: str = "info") -> tk.Frame:
    color = {
        "success": theme.SUCCESS,
        "warning": theme.WARNING,
        "danger": theme.DANGER,
        "info": theme.PRIMARY,
        "muted": theme.MUTED,
    }.get(level, theme.PRIMARY)
    frame = tk.Frame(parent, bg=theme.HOVER, highlightbackground=theme.BORDER, highlightthickness=1)
    tk.Label(frame, text=value, bg=theme.HOVER, fg=color, font=theme.font(22, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
    tk.Label(frame, text=label, bg=theme.HOVER, fg=theme.MUTED, font=theme.font(10)).pack(anchor="w", padx=14, pady=(0, 12))
    return frame


def empty_hint(parent: tk.Widget, text: str) -> tk.Label:
    return tk.Label(parent, text=text, bg=theme.PANEL, fg=theme.MUTED, font=theme.font(11), wraplength=900, justify=tk.LEFT)


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _event=None) -> None:
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.window,
            text=self.text,
            bg=theme.HOVER,
            fg=theme.TEXT,
            font=theme.font(10),
            padx=10,
            pady=6,
            justify=tk.LEFT,
            wraplength=260,
            highlightbackground=theme.BORDER,
            highlightthickness=1,
        )
        label.pack()

    def hide(self, _event=None) -> None:
        if self.window:
            self.window.destroy()
            self.window = None


def attach_tooltip(widget: tk.Widget, text: str) -> None:
    Tooltip(widget, text)
