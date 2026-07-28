from __future__ import annotations

import datetime as _dt
import importlib.metadata as metadata
import os
import re
import shutil
import site
import subprocess
import sys
from pathlib import Path, PurePosixPath


PRODUCT_NAME = "水星配音对齐工作室"
PKG_ROOT_NAME = "轻量云配版包"
DATA_DIR_NAME = "水星配音数据"
COMPONENTS_DIR = "组件"

# Cloud edition keeps only local timing/render/draft helpers. Local TTS stacks
# are intentionally excluded because they are multi-GB and should live in the
# full offline edition instead.
SEED_DISTRIBUTIONS = [
    "Pillow",
    "openpyxl",
    "numpy",
    "pycapcut",
    "scenedetect",
]
DIST_BLOCKLIST = {
    "torch",
    "torchaudio",
    "torchvision",
    "torchgen",
    "functorch",
    "triton",
    "nvidia",
    "dots-tts",
    "dots.tts",
    "fish-speech",
    "transformers",
    "tokenizers",
    "accelerate",
    "safetensors",
    "xformers",
    "flash-attn",
    "cusparselt",
    "cudnn",
}

DEFAULT_WHISPER_MODELS = ["tiny", "base"]
FFMPEG_BINARIES = ["ffmpeg.exe", "ffprobe.exe"]


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "source"
OUT_ROOT = REPO / PKG_ROOT_NAME


def log(message: str) -> None:
    print(message, flush=True)


def size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    if path.exists():
        for file in path.rglob("*"):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                pass
    return total


def fmt_size(num: int) -> str:
    if num >= 1024**3:
        return f"{num / 1024**3:.2f} GB"
    return f"{num / 1024**2:.1f} MB"


def ensure_inside(child: Path, parent: Path) -> None:
    child_r = child.resolve()
    parent_r = parent.resolve()
    try:
        child_r.relative_to(parent_r)
    except ValueError as exc:
        raise RuntimeError(f"Refusing to clean outside package root: {child_r}") from exc


def run_text(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, cwd=str(REPO), text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def app_version() -> str:
    sys.path.insert(0, str(SOURCE))
    try:
        from dub_align_studio.version import APP_VERSION

        return str(APP_VERSION)
    finally:
        try:
            sys.path.remove(str(SOURCE))
        except ValueError:
            pass


def build_stamp() -> str:
    git_hash = run_text(["git", "rev-parse", "--short", "HEAD"]) or "unknown"
    return _dt.datetime.now().strftime("%Y/%m/%d %H:%M") + f" · {git_hash}"


def clean_target(target: Path) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_inside(target, OUT_ROOT)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)


def ignore_stdlib(dir_path: str, names: list[str]) -> set[str]:
    blocked_dirs = {
        "site-packages",
        "test",
        "tests",
        "idlelib",
        "turtledemo",
        "ensurepip",
        "venv",
        "lib2to3",
    }
    blocked_files = {".pyc", ".pyo"}
    ignored: set[str] = set()
    for name in names:
        lower = name.lower()
        if lower in blocked_dirs or any(lower.endswith(suffix) for suffix in blocked_files):
            ignored.add(name)
    return ignored


def copytree(src: Path, dst: Path, ignore=None) -> None:
    if not src.exists():
        return
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def copy_python_runtime(target: Path) -> Path:
    base = Path(sys.base_prefix).resolve()
    exe_dir = Path(sys.executable).resolve().parent
    if OUT_ROOT.resolve() in base.parents or base == OUT_ROOT.resolve():
        raise RuntimeError("Do not run the packer with a Python copied from a previous package.")
    if "WindowsApps" in str(base):
        raise RuntimeError("Microsoft Store Python is not portable enough. Use python.org Python 3.11+.")

    py_target = target / "python"
    py_target.mkdir(parents=True)
    log(f"[1/6] Copying clean Python runtime: {base}")

    for name in ("python.exe", "pythonw.exe", "python3.dll", f"python{sys.version_info.major}{sys.version_info.minor}.dll",
                 "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        for src_root in (exe_dir, base):
            src = src_root / name
            if src.is_file():
                shutil.copy2(src, py_target / name)
                break

    for name in ("DLLs", "Lib", "tcl"):
        copytree(base / name, py_target / name, ignore=ignore_stdlib if name == "Lib" else None)

    site_target = py_target / "Lib" / "site-packages"
    site_target.mkdir(parents=True, exist_ok=True)
    copied = copy_whitelisted_distributions(site_target)
    if copied:
        log("      kept site-packages: " + ", ".join(copied))
    else:
        log("      kept site-packages: none")
    return py_target


def normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str | None:
    if "extra ==" in requirement:
        return None
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    return match.group(1) if match else None


def resolve_distribution(name: str):
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return None


def copy_whitelisted_distributions(site_target: Path) -> list[str]:
    queue = list(SEED_DISTRIBUTIONS)
    copied: dict[str, str] = {}
    while queue:
        requested = queue.pop(0)
        norm_req = normalize_dist_name(requested)
        if norm_req in copied or norm_req in DIST_BLOCKLIST:
            continue
        dist = resolve_distribution(requested)
        if dist is None:
            continue
        dist_name = dist.metadata.get("Name", requested)
        norm = normalize_dist_name(dist_name)
        if norm in copied or norm in DIST_BLOCKLIST:
            continue
        copy_distribution_files(dist, site_target)
        copied[norm] = dist_name
        for req in dist.requires or []:
            name = requirement_name(req)
            if name and normalize_dist_name(name) not in DIST_BLOCKLIST:
                queue.append(name)
    return [copied[k] for k in sorted(copied)]


def should_skip_dist_file(rel: PurePosixPath) -> bool:
    parts = [p.lower() for p in rel.parts]
    if any(part in {"__pycache__", "test", "tests"} for part in parts):
        return True
    if any(part == ".." for part in rel.parts):
        return True
    name = parts[-1] if parts else ""
    if name.endswith((".pyc", ".pyo")):
        return True
    return False


def copy_distribution_files(dist, site_target: Path) -> None:
    for rel in dist.files or []:
        rel_path = PurePosixPath(str(rel).replace("\\", "/"))
        if should_skip_dist_file(rel_path):
            continue
        src = Path(dist.locate_file(rel))
        if not src.is_file():
            continue
        dst = site_target.joinpath(*rel_path.parts)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def ignore_source(dir_path: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__"}
    ignored.update(name for name in names if name.endswith((".pyc", ".pyo")))
    return ignored


def copy_source(target: Path, stamp: str) -> None:
    log("[2/6] Copying application source")
    copytree(SOURCE, target / "source", ignore=ignore_source)
    server = REPO / "server"
    if server.exists():
        copytree(server, target / "server", ignore=ignore_source)
    build_info = target / "source" / "dub_align_studio" / "_build_info.py"
    build_info.write_text(f'BUILD_STAMP = "{stamp}"\n', encoding="utf-8")


def data_root() -> Path:
    sys.path.insert(0, str(SOURCE))
    try:
        from dub_align_studio.settings import data_root as app_data_root

        return Path(app_data_root())
    finally:
        try:
            sys.path.remove(str(SOURCE))
        except ValueError:
            pass


def selected_whisper_models() -> list[str]:
    raw = os.environ.get("MERCURY_LITE_WHISPER_MODELS", "")
    values = [item.strip().lower() for item in raw.split(",") if item.strip()] if raw else DEFAULT_WHISPER_MODELS
    return [item for item in values if item in {"tiny", "base", "small"}]


def ignore_whisper_runtime(dir_path: str, names: list[str]) -> set[str]:
    ignored = {"models", "runtime_downloads", "__pycache__"}
    ignored.update(name for name in names if name.lower().endswith((".zip", ".tmp", ".part", ".download", ".pyc", ".pyo")))
    return ignored


def copy_data(target: Path) -> None:
    log("[3/6] Copying lite data (whisper runtime/models, pyCapCut, audio assets)")
    src_root = data_root()
    dst_root = target / DATA_DIR_NAME
    for sub in (COMPONENTS_DIR, "音色库", "克隆音频", "字体", "配乐音效"):
        (dst_root / sub).mkdir(parents=True, exist_ok=True)

    if not src_root.exists():
        log(f"      data root not found: {src_root}")
        return

    src_components = src_root / COMPONENTS_DIR
    dst_components = dst_root / COMPONENTS_DIR

    src_whisper = src_components / "whisper.cpp"
    if src_whisper.exists():
        copytree(src_whisper, dst_components / "whisper.cpp", ignore=ignore_whisper_runtime)
        src_models = src_whisper / "models"
        dst_models = dst_components / "whisper.cpp" / "models"
        dst_models.mkdir(parents=True, exist_ok=True)
        for key in selected_whisper_models():
            name = f"ggml-{key}.bin"
            src = src_models / name
            if src.is_file():
                shutil.copy2(src, dst_models / name)
                log(f"      included whisper model: {name}")
            else:
                log(f"      missing whisper model, skipped: {name}")

    for name in ("pyCapCut", "pycapcut"):
        src = src_components / name
        if src.exists():
            copytree(src, dst_components / name, ignore=ignore_source)

    audio = src_root / "配乐音效"
    if audio.exists():
        copytree(audio, dst_root / "配乐音效", ignore=ignore_source)

    preset = src_root / "作品参数预设.json"
    if preset.is_file():
        shutil.copy2(preset, dst_root / preset.name)

    note = (
        "轻量云配版不随包携带本地 TTS 模型、torch、fish-speech、字体库、音色库和克隆音频。\n"
        "需要迁移这些重资产时，请使用整合离线版，或在目标机把完整“水星配音数据”目录指回软件。\n"
        "默认随包 whisper 模型："
        + ", ".join(selected_whisper_models())
        + "。可在打包前设置 MERCURY_LITE_WHISPER_MODELS=tiny,base,small 自定义。\n"
    )
    (dst_root / "轻量版数据说明.txt").write_text(note, encoding="utf-8")


def find_binary(name: str) -> Path | None:
    for candidate in (REPO / name, REPO / "dist" / PRODUCT_NAME / name):
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def copy_ffmpeg(target: Path) -> None:
    log("[4/6] Copying ffmpeg/ffprobe")
    missing: list[str] = []
    for name in FFMPEG_BINARIES:
        src = find_binary(name)
        if src is None:
            missing.append(name)
            continue
        shutil.copy2(src, target / name)
        log(f"      included {name}")
    if missing:
        raise RuntimeError(
            "轻量云配版必须自带 ffmpeg.exe / ffprobe.exe，否则云配音成功后无法渲染成片。"
            " 缺少：" + ", ".join(missing) + "。请先把这两个文件放到仓库根目录或系统 PATH 后重新打包。"
        )


def write_launcher_files(target: Path, version: str, stamp: str) -> None:
    log("[5/6] Writing launcher and instructions")
    launcher = "\r\n".join(
        [
            "@echo off",
            "chcp 65001 >nul",
            "cd /d \"%~dp0\"",
            f"title {PRODUCT_NAME} - 轻量云配版",
            "set \"MERCURY_CLOUD_ONLY=1\"",
            "set \"PYTHONNOUSERSITE=1\"",
            "set \"PYTHONHOME=%~dp0python\"",
            "set \"PYTHONPATH=%~dp0source\"",
            "set \"PATH=%~dp0;%~dp0python;%~dp0python\\DLLs;%PATH%\"",
            "if not exist \"%~dp0python\\python.exe\" goto NOPY",
            "if not exist \"%~dp0ffmpeg.exe\" echo [提示] 未发现 ffmpeg.exe，渲染成片需要把 ffmpeg.exe/ffprobe.exe 放到本文件夹。",
            "echo 正在启动轻量云配版，请在设置里填写云配音地址和 API Key。",
            "\"%~dp0python\\python.exe\" \"%~dp0source\\dub_align_studio\\launcher.py\"",
            "goto END",
            ":NOPY",
            "echo [错误] 缺少 python\\python.exe，压缩包可能没有完整解压。",
            ":END",
            "pause",
            "",
        ]
    )
    (target / "启动.bat").write_text(launcher, encoding="utf-8", newline="")

    readme = (
        f"{PRODUCT_NAME} 轻量云配版\n\n"
        "1. 双击 启动.bat。\n"
        "2. 在设置里填写云配音服务地址和 API Key，配音引擎选择 dots.tts 云 GPU 远程。\n"
        "3. 本版本不携带本地 torch/dots.tts/fish-speech 大模型，适合发到普通电脑使用。\n"
        "4. 渲染成片需要的 ffmpeg.exe / ffprobe.exe 已随包放在 启动.bat 旁边；如果安装目录缺这两个文件，需重新安装最新版安装包。\n"
        "5. 本机计时使用 whisper.cpp；默认只随包 tiny/base 模型，small 可在工具箱按需下载。\n"
        "6. 字体、音色库、克隆音频属于用户数据，轻量包不自动携带，可手动导入或指向完整数据目录。\n"
    )
    (target / "首次使用说明.txt").write_text(readme, encoding="utf-8")
    (target / "版本.txt").write_text(f"v{version} · {stamp} · 轻量云配版\n", encoding="utf-8")


def make_zip(target: Path, version: str) -> Path | None:
    if os.environ.get("MERCURY_SKIP_ZIP", "").strip() in {"1", "true", "True"}:
        return None
    release_root = REPO / "发布包"
    release_root.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    archive_base = release_root / f"{PRODUCT_NAME}_轻量云配版_v{version}_{stamp}"
    log("[6/6] Creating zip archive")
    return Path(shutil.make_archive(str(archive_base), "zip", OUT_ROOT, target.name))


def print_size_report(target: Path, archive: Path | None) -> None:
    log("")
    log("[size] package folder: " + fmt_size(size_bytes(target)))
    for child in sorted(target.iterdir(), key=lambda p: size_bytes(p), reverse=True):
        log(f"       {child.name}: {fmt_size(size_bytes(child))}")
    if archive:
        log("[size] zip archive: " + fmt_size(size_bytes(archive)))
        log("[done] zip: " + str(archive))
    log("[done] folder: " + str(target))


def main() -> int:
    if not SOURCE.exists():
        log("[ERROR] source directory was not found. Run this script from the repository root.")
        return 1
    version = app_version()
    stamp = build_stamp()
    target = OUT_ROOT / f"{PRODUCT_NAME}_轻量版_v{version}"
    try:
        log(f"[version] v{version} · {stamp}")
        clean_target(target)
        copy_python_runtime(target)
        copy_source(target, stamp)
        copy_data(target)
        copy_ffmpeg(target)
        write_launcher_files(target, version, stamp)
        archive = make_zip(target, version)
        print_size_report(target, archive)
        return 0
    except Exception as exc:
        log("[ERROR] " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
