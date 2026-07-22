"""软件设置：持久化到 ~/.dub_align_studio/settings.json。

当前设置项：
    component_root：工具箱组件（whisper-cli/ggml 模型等大文件）的保存根目录。
        默认在软件目录旁的 vendor_tools（打包后即 exe 旁），用户可改到任意盘，
        避免占用 C 盘空间；修改后下载与探测都走新位置，旧位置文件不自动迁移。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STUDIO_HOME = Path.home() / ".dub_align_studio"
SETTINGS_FILE = STUDIO_HOME / "settings.json"


def default_component_root() -> Path:
    """默认组件根：exe/仓库旁 vendor_tools（与水星轻量安装布局一致）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "vendor_tools"
    return Path(__file__).resolve().parents[2] / "vendor_tools"


def load_settings() -> dict:
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def save_settings(update: dict) -> dict:
    settings = load_settings()
    settings.update({k: v for k, v in update.items() if v is not None})
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def component_root() -> Path:
    """组件保存根目录（用户设置优先，其次默认）。"""
    value = str(load_settings().get("component_root") or "").strip()
    return Path(value) if value else default_component_root()


def set_component_root(path: str | Path) -> Path:
    path = Path(str(path)).expanduser()
    path.mkdir(parents=True, exist_ok=True)  # 提前建目录，验证可写
    save_settings({"component_root": str(path)})
    return path


def whisper_home() -> Path:
    return component_root() / "whisper.cpp"


def whisper_models_dir() -> Path:
    return whisper_home() / "models"


def whisper_runtime_zip(filename: str) -> Path:
    return whisper_home() / "runtime_downloads" / filename


def whisper_cli_path() -> Path | None:
    """whisper-cli 可执行文件：自定义根优先，其次 PATH。"""
    import shutil

    for candidate in (
        whisper_home() / "build" / "bin" / "Release" / "whisper-cli.exe",
        whisper_home() / "whisper-cli.exe",
        whisper_home() / "build" / "bin" / "whisper-cli",
    ):
        if candidate.exists():
            return candidate
    found = shutil.which("whisper-cli") or shutil.which("whisper-cli.exe")
    return Path(found) if found else None
