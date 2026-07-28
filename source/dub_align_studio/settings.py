"""软件设置：统一数据总目录 + 持久化（~/.dub_align_studio/settings.json）。

需求（用户 2026-07-22 第三轮）：所有下载/生成资产收进一个可自定义的总目录，
整个文件夹拷到另一台电脑、在软件里指回该目录即可直接使用（环境自检识别，
已存在的组件/模型不再重复下载）。

总目录（data_root，默认 exe/仓库旁「水星配音数据」）固定子结构：
    组件/whisper.cpp/…      whisper-cli 运行时 + ggml 模型
    音色库/<voice_id>/…     克隆参考音色（含转写/元数据）
    克隆音频/…              每次整篇克隆产出的 master 存档
    字体/…                  字幕/文本框可选字体（可手动放入 ttf/otf/ttc）

settings.json 始终在用户目录（找到总目录之前必须有处可读）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STUDIO_HOME = Path.home() / ".dub_align_studio"
SETTINGS_FILE = STUDIO_HOME / "settings.json"

DIR_COMPONENTS = "组件"
DIR_VOICES = "音色库"
DIR_CLONES = "克隆音频"
DIR_FONTS = "字体"
DIR_AUDIO = "配乐音效"   # BGM 与音效素材（可手动放入 mp3/wav），第五轮


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_data_root() -> Path:
    """默认数据总目录：exe/仓库旁「水星配音数据」；不存在时向上两级回退查找。

    为什么要回退（用户 2026-07-24「重新打包把模型弄丢了」）：打包.bat 清理 dist 前
    会把 dist 内的数据目录搬到仓库根保护；新 exe 在 dist\\软件名\\ 下、默认只看 exe 旁
    → 找不到被保护的数据（模型/音色「丢失」）。向上（dist、仓库根）回退即可自动找回。
    """
    primary = _app_dir() / "水星配音数据"
    if primary.is_dir():
        return primary
    for ancestor in (_app_dir().parent, _app_dir().parent.parent):
        candidate = ancestor / "水星配音数据"
        if candidate.is_dir():
            return candidate
    return primary


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


CLOUD_EDITION_MARKER = "cloud_edition.flag"


def _cloud_marker_present() -> bool:
    """轻量云配版打包时会往包内放一个 cloud_edition.flag 标记文件（--add-data 进 _internal，
    同时拷一份到 exe 旁）。有它 → 即便**直接双击 exe**(没走 启动.bat、没设环境变量)也走云配版，
    绝不再弹本地模型自检/报错。源码直跑无此文件 → 不受影响。"""
    for base in (
        getattr(sys, "_MEIPASS", None),                              # PyInstaller 运行时资源根(=_internal)
        Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None,  # exe 旁
    ):
        try:
            if base and (Path(base) / CLOUD_EDITION_MARKER).is_file():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def cloud_only() -> bool:
    """轻量云配版模式：只用云配音，隐藏本地模型(dots/torch/fish)相关的引擎/组件/自检项，
    环境自检也不因缺本地模型报错。三种开启方式（任一即可）：
      1. 包内标记文件 cloud_edition.flag（云配版打包自动放入 → 直接双击 exe 也生效）；
      2. 环境变量 MERCURY_CLOUD_ONLY=1（启动.bat 设，兼容源码/旧包）；
      3. settings.json 的 cloud_only。
    正式版无标记、无环境变量 → 行为完全不变。"""
    v = os.environ.get("MERCURY_CLOUD_ONLY")
    if v is not None:
        return v.strip() not in ("", "0", "false", "False", "no")
    if _cloud_marker_present():
        return True
    try:
        return bool(load_settings().get("cloud_only"))
    except Exception:  # noqa: BLE001
        return False


def ffmpeg_tool(name: str = "ffmpeg") -> str:
    """把 ffmpeg / ffprobe 解析成**绝对路径**：依次找 启动.bat/exe 旁边、数据总目录、组件目录、
    exe 所在目录及其上级、当前目录，最后查系统 PATH；都没有则原样返回名字（交上层给缺失指引）。

    根因（2026-07-28 用户实测）：旧逻辑只 shutil.which(依赖 PATH/当前目录)，用户把 ffmpeg 放在
    启动项旁边但启动时的当前目录并非该文件夹 → 找不到。改为主动搜已知目录并返回绝对路径，
    渲染/量时长子进程无论当前目录是什么都能用。"""
    import shutil

    exe = name + (".exe" if sys.platform == "win32" else "")
    dirs: list[Path] = []
    for producer in (
        lambda: _app_dir(),                    # 启动.bat / exe 旁边（用户最常见放置处）
        lambda: Path(sys.executable).resolve().parent,           # 打包 exe 所在文件夹
        lambda: Path(sys.executable).resolve().parent / "_internal",  # PyInstaller 打包资源目录
        lambda: Path(getattr(sys, "_MEIPASS", "")),              # PyInstaller 运行时资源根(=_internal)
        lambda: data_root(),
        lambda: components_root(),
        lambda: data_root() / DIR_COMPONENTS,
        lambda: Path(sys.executable).resolve().parent.parent,
        lambda: Path.cwd(),
    ):
        try:
            dirs.append(producer())
        except Exception:  # noqa: BLE001
            continue
    for d in dirs:
        try:
            cand = d / exe
            if cand.is_file():
                return str(cand)
        except Exception:  # noqa: BLE001
            continue
    return shutil.which(name) or name


def dots_remote_config() -> tuple[str, str]:
    """云 dots.tts 远程引擎配置：(服务器地址, API Key)。
    环境变量优先（DOTS_REMOTE_ENDPOINT / DOTS_REMOTE_API_KEY），其次 settings.json
    （dots_remote_endpoint / dots_remote_api_key）。地址去掉尾部斜杠。"""
    settings = load_settings()
    endpoint = (os.environ.get("DOTS_REMOTE_ENDPOINT")
                or str(settings.get("dots_remote_endpoint") or "")).strip().rstrip("/")
    api_key = (os.environ.get("DOTS_REMOTE_API_KEY")
               or str(settings.get("dots_remote_api_key") or "")).strip()
    return endpoint, api_key


def data_root() -> Path:
    """统一数据总目录（用户设置优先；兼容旧 component_root 设置）。"""
    settings = load_settings()
    value = str(settings.get("data_root") or "").strip()
    if value:
        return Path(value)
    legacy = str(settings.get("component_root") or "").strip()  # 旧版仅组件可自定义
    if legacy:
        return Path(legacy).parent / "水星配音数据" if Path(legacy).name == DIR_COMPONENTS else Path(legacy)
    return default_data_root()


def set_data_root(path: str | Path) -> Path:
    path = Path(str(path)).expanduser()
    for sub in (DIR_COMPONENTS, DIR_VOICES, DIR_CLONES, DIR_FONTS, DIR_AUDIO):
        (path / sub).mkdir(parents=True, exist_ok=True)  # 建全子结构，顺带验证可写
    save_settings({"data_root": str(path)})
    _migrate_legacy_voices(path)
    return path


def _migrate_legacy_voices(root: Path) -> None:
    """旧版音色库（~/.dub_align_studio/音色库）自动并入总目录（仅目标为空时拷贝）。"""
    import shutil

    legacy = STUDIO_HOME / DIR_VOICES
    target = root / DIR_VOICES
    try:
        if legacy.is_dir() and any(legacy.iterdir()) and not any(target.iterdir()):
            for voice_dir in legacy.iterdir():
                if voice_dir.is_dir():
                    shutil.copytree(voice_dir, target / voice_dir.name, dirs_exist_ok=True)
    except Exception:
        pass  # 迁移失败不阻塞启动，音色包导入可兜底


# ------------------------------------------------------------------ 子目录
def components_root() -> Path:
    return data_root() / DIR_COMPONENTS


def voices_library_root() -> Path:
    """音色库根（voice_library 的 library_root 参数；其内部再套一层「音色库」目录名，
    故这里返回 data_root 本身，保证磁盘路径为 总目录/音色库/<voice_id>）。"""
    return data_root()


def clones_dir() -> Path:
    path = data_root() / DIR_CLONES
    path.mkdir(parents=True, exist_ok=True)
    return path


def fonts_dir() -> Path:
    path = data_root() / DIR_FONTS
    path.mkdir(parents=True, exist_ok=True)
    return path


def audio_assets_dir() -> Path:
    """BGM/音效素材目录（可手动放入 mp3/wav/m4a…）。"""
    path = data_root() / DIR_AUDIO
    path.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ whisper 布局
def whisper_home() -> Path:
    return components_root() / "whisper.cpp"


def whisper_models_dir() -> Path:
    return whisper_home() / "models"


def whisper_runtime_zip(filename: str) -> Path:
    return whisper_home() / "runtime_downloads" / filename


def whisper_cli_path() -> Path | None:
    """whisper-cli 可执行文件：总目录优先，其次旧组件目录，最后 PATH。

    已知布局直查 + 递归搜索兜底：官方 whisper-bin-x64.zip 用
    Compress-Archive 打包，内含一层 Release\\ 目录（v1.9.1 核实）；
    上游未来再改打包结构也能靠递归搜索找到。"""
    import shutil

    roots = [whisper_home()]
    legacy = str(load_settings().get("component_root") or "").strip()
    if legacy:
        roots.append(Path(legacy) / "whisper.cpp")
    roots.append(_app_dir() / "vendor_tools" / "whisper.cpp")  # 最早期默认位置兜底
    for root in roots:
        for candidate in (
            root / "build" / "bin" / "Release" / "whisper-cli.exe",
            root / "Release" / "whisper-cli.exe",  # 官方 zip 的真实布局
            root / "whisper-cli.exe",
            root / "build" / "bin" / "whisper-cli",
        ):
            if candidate.exists():
                return candidate
        if root.is_dir():  # 布局不认识时递归找（目录很小，代价可忽略）
            for name in ("whisper-cli.exe", "whisper-cli"):
                found = next(iter(root.rglob(name)), None)
                if found is not None and found.is_file():
                    return found
    found = shutil.which("whisper-cli") or shutil.which("whisper-cli.exe")
    return Path(found) if found else None


# 旧接口兼容（第二轮曾暴露 component_root 概念，现映射到总目录/组件）
def component_root() -> Path:
    return components_root()


def default_component_root() -> Path:
    return default_data_root() / DIR_COMPONENTS


def set_component_root(path: str | Path) -> Path:
    """旧端点兼容：把传入目录视为总目录设置。"""
    return set_data_root(path) / DIR_COMPONENTS
