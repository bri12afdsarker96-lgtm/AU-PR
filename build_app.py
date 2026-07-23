"""Robust local build entry used by 打包.bat.

Keeping the long PyInstaller call in Python avoids cmd.exe edge cases with
multi-line carets, Chinese paths, and parent folders that contain '&'.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "source"
sys.path.insert(0, str(SOURCE_DIR))

from dub_align_studio.version import APP_NAME, APP_VERSION  # noqa: E402


PRODUCT_NAME = APP_NAME
DIST_DIR = ROOT / "dist"
PROD_DIR = DIST_DIR / PRODUCT_NAME
BUILD_DIR = ROOT / "build" / PRODUCT_NAME
SPEC_FILE = ROOT / f"{PRODUCT_NAME}.spec"
INTERNAL_DIR = PROD_DIR / "_internal"
HEAVY_MODULES = (
    "torch",
    "torchaudio",
    "torio",
    "torchvision",
    "transformers",
    "tokenizers",
    "huggingface_hub",
    "triton",
    "functorch",
    "torchgen",
)


def log(message: str) -> None:
    print(message, flush=True)


def run(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, env=env)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, args)
    return result


def safe_remove(path: Path, *, retries: int = 1, delay: float = 1.0) -> None:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        return
    if resolved == ROOT or ROOT not in resolved.parents:
        raise RuntimeError(f"拒绝删除工作区外路径：{resolved}")
    if not resolved.exists():
        return
    for attempt in range(retries):
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


def release_dir() -> Path:
    explicit = os.environ.get("AU_PR_RELEASE_DIR")
    if explicit:
        return Path(explicit).resolve()
    parent_release = ROOT.parent / "发布包"
    parent_data = ROOT.parent / "水星配音数据"
    if parent_release.exists() or parent_data.exists():
        return parent_release.resolve()
    return (ROOT / "发布包").resolve()


def protect_embedded_data() -> None:
    embedded = PROD_DIR / "水星配音数据"
    if not embedded.exists():
        return
    target = (ROOT.parent / "水星配音数据") if (ROOT.parent / "发布包").exists() else (ROOT / "水星配音数据")
    log(f"[保护] 发现 dist 内数据目录，先移动到：{target}")
    target.mkdir(parents=True, exist_ok=True)
    code = subprocess.run(
        ["robocopy", str(embedded), str(target), "/e", "/move", "/nfl", "/ndl", "/njh", "/njs", "/nc", "/ns"],
        cwd=ROOT,
    ).returncode
    if code >= 8:
        raise RuntimeError("移动 dist 内数据目录失败，请先关闭占用文件后重试。")


def clean_previous_outputs() -> None:
    log("[清理] 删除上次打包产物与旧缓存...")
    for path in (PROD_DIR, BUILD_DIR, SPEC_FILE):
        safe_remove(path, retries=8, delay=1.5)
    for path in ROOT.glob("dist_new_*"):
        safe_remove(path, retries=8, delay=1.5)
    for path in SOURCE_DIR.rglob("__pycache__"):
        safe_remove(path)
    if PROD_DIR.exists():
        raise RuntimeError(f"旧产物仍被占用，无法删除：{PROD_DIR}")


def ensure_pyinstaller() -> None:
    result = run([sys.executable, "-m", "pip", "show", "pyinstaller"], check=False)
    if result.returncode == 0:
        return
    log("[环境] 未找到 PyInstaller，正在安装...")
    run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def git_hash() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def write_build_info() -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M") + f" · {git_hash()}"
    build_info = SOURCE_DIR / "dub_align_studio" / "_build_info.py"
    build_info.write_text(f'BUILD_STAMP = "{stamp}"\n', encoding="utf-8")
    log(f"[版本] 本次构建：v{APP_VERSION} · {stamp}")


def pyinstaller_args() -> list[str]:
    add_data = str(SOURCE_DIR / "dub_align_studio" / "web") + os.pathsep + "dub_align_studio/web"
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        PRODUCT_NAME,
        "--paths",
        str(SOURCE_DIR),
        "--add-data",
        add_data,
        "--collect-submodules",
        "dub_align_studio",
        "--collect-submodules",
        "integrated_workbench",
    ]
    for module in HEAVY_MODULES:
        args.extend(["--exclude-module", module])
    args.extend(["--console", str(SOURCE_DIR / "dub_align_studio" / "launcher.py")])
    return args


def remove_accidental_heavy_modules() -> None:
    for name in HEAVY_MODULES:
        safe_remove(INTERNAL_DIR / name)
        safe_remove(INTERNAL_DIR / f"{name}.py")
    for info in INTERNAL_DIR.glob("*.dist-info"):
        if any(info.name.lower().startswith(name.lower().replace("_", "-")) for name in HEAVY_MODULES):
            safe_remove(info)


def copy_stdlib_for_component_runtime() -> None:
    pyhome = Path(sys.base_prefix)
    lib = pyhome / "Lib"
    if not lib.exists():
        log(f"[提示] 未找到 Python 标准库目录，跳过补齐：{lib}")
        return
    log("[补齐] 复制 Python 标准库到 exe 内部，供外置组件依赖导入...")
    code = subprocess.run(
        [
            "robocopy",
            str(lib),
            str(INTERNAL_DIR),
            "/e",
            "/xd",
            "site-packages",
            "__pycache__",
            "/xf",
            "*.pyc",
            "/nfl",
            "/ndl",
            "/njh",
            "/njs",
            "/nc",
            "/ns",
        ],
        cwd=ROOT,
    ).returncode
    if code >= 8:
        raise RuntimeError("复制 Python 标准库失败。")


def finish_release() -> None:
    target = release_dir()
    target.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_DIR)
    env["AU_PR_RELEASE_DIR"] = str(target)
    log(f"[收尾] 发布压缩包输出目录：{target}")
    run([sys.executable, "打包收尾.py"], env=env)


def cleanup_success_artifacts() -> None:
    safe_remove(BUILD_DIR)
    safe_remove(SPEC_FILE)
    try:
        (ROOT / "build").rmdir()
    except OSError:
        pass


def main() -> int:
    try:
        subprocess.run(["taskkill", "/f", "/im", f"{PRODUCT_NAME}.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        protect_embedded_data()
        clean_previous_outputs()
        ensure_pyinstaller()
        write_build_info()
        log("[打包] 正在运行 PyInstaller...")
        run(pyinstaller_args())
        if not (PROD_DIR / f"{PRODUCT_NAME}.exe").is_file():
            raise RuntimeError(f"PyInstaller 结束但未找到 exe：{PROD_DIR / (PRODUCT_NAME + '.exe')}")
        remove_accidental_heavy_modules()
        copy_stdlib_for_component_runtime()
        finish_release()
        cleanup_success_artifacts()
        log(f"[完成] 产物目录：{PROD_DIR}")
        return 0
    except subprocess.CalledProcessError as exc:
        log(f"[错误] 命令执行失败，退出码 {exc.returncode}：{' '.join(map(str, exc.cmd))}")
        return int(exc.returncode or 1)
    except Exception as exc:
        log(f"[错误] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
