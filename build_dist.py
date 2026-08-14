# -*- coding: utf-8 -*-
"""发行包构建脚本 · 轻量云配版包（AU-PR）

用法（Windows / macOS / Linux 均可）：
    python build_dist.py [--skip-cython] [--skip-nuitka] [--out DIR]

流程：
    1. 清理 dist/ 旧内容
    2. Cython 编译授权关键模块（licensing/*.py） → .pyd/.so
        （攻击者拿到 .pyd 无源码可读；比 Nuitka 主包还难反）
    3. Nuitka 打包主程序（standalone）
    4. 拷贝必需资源（web/、组件、fonts）
    5. **明确排除**敏感/非必要内容：
        - source/ 里的 .py 源码（Nuitka 编译产物覆盖）
        - tests/  docs/ .git .github .claude
        - 用户自己的 settings.json / license.json / gpu_state.json
        - README*.md / *.md
        - __pycache__ / *.pyc  中间产物
        - .env  .venv  venv  virtualenv
        - 任何 requirements-dev.txt / pyproject.toml 等 metadata
    6. --with-ffmpeg：把 ffmpeg.exe / ffprobe.exe 一起塞进发行包
        （用户机器不用再自己装环境；配合 Inno Setup 安装器一键就能跑）
        搜索优先级：
            ./tools/ffmpeg/ffmpeg.exe    （项目内自带的裁剪版，最省）
            ./tools/ffmpeg/bin/ffmpeg.exe
            PATH 中的 ffmpeg（系统装的完整版，兜底）
    7. 生成启动脚本 `启动软件.bat`（含 DUB_ALIGN_LICENSE_REQUIRED=1）
    8. 计算 exe SHA-256 → 落到 `integrity.hash`（供 RASP 完整性自检）
    9. 打印发行清单摘要（列出根目录所有文件 + 大小）

强制排除的敏感文件（**发行包里绝不能出现**）：
    * 任何 .py、.pyc（用户/破解者不能拿到源码）
    * settings.json / license.json / gpu_profiles.json（含个人激活/使用痕迹）
    * .git 目录（含全部提交历史）
    * docs/PROTECTION.md（讲怎么破解的文档不能给用户）
    * tests/ 目录（含 mock 逻辑，可被反向利用）
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SOURCE_DIR = HERE / "source" / "dub_align_studio"
DIST_ROOT = HERE / "dist" / "轻量云配版包"


# 明确排除的文件/目录（相对项目根）
FORBIDDEN_PATHS = (
    ".git", ".github", ".claude", ".vscode", ".idea",
    "tests", "docs", "水星配音数据",   # 用户本地数据
    ".env", ".venv", "venv", "virtualenv", "__pycache__",
)

# 必带的资源目录（相对 source/dub_align_studio/）
RESOURCE_DIRS_UNDER_PACKAGE = ("web", "fonts")   # web UI + 内置字体（若有）

# 敏感文件模式——遍历发行目录时若命中一律删除
SENSITIVE_PATTERNS = (
    "settings.json", "license.json", "gpu_state.json",
    "gpu_profiles.json", "*.log", "queue.sqlite3",
    "*.py", "*.pyc", "*.pyo", "*.pyw",              # 源码/字节码不给
    "README.md", "README*.md", "PROTECTION.md",
    "*.md",                                          # 各种文档
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "conftest.py",
    ".git*", "*.gitignore",
)


def _log(msg: str) -> None:
    print(f"[build_dist] {msg}", flush=True)


def clean_dist() -> None:
    if DIST_ROOT.exists():
        _log(f"清理旧的 {DIST_ROOT}")
        shutil.rmtree(DIST_ROOT, ignore_errors=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)


def cython_compile_licensing() -> None:
    """Cython 编译 licensing/*.py → 原地生成 .pyd/.so，然后**删掉 .py 源码**。

    Cython 编译**要求**装了 setuptools + C 编译器：
      - Windows: MSVC Build Tools（用 pip install setuptools cython）
      - Linux/macOS: gcc/clang
    """
    lic_dir = SOURCE_DIR / "licensing"
    py_files = sorted(lic_dir.glob("*.py"))
    py_files = [p for p in py_files if p.name != "__init__.py"]  # 保留 __init__
    if not py_files:
        _log("licensing/ 无 .py 需要编译")
        return
    try:
        import Cython.Build.Cythonize as _cy  # noqa: F401, PLC0415
    except ImportError:
        _log("⚠ 未安装 cython，跳过（发行强度会下降）。pip install cython")
        return
    _log(f"Cython 编译 {len(py_files)} 个 licensing 模块…")
    cmd = [
        sys.executable, "-m", "Cython.Build.Cythonize",
        "-i",           # inplace 生成 .so/.pyd
        "-3",           # Python 3
    ] + [str(p) for p in py_files]
    r = subprocess.run(cmd, cwd=str(HERE))
    if r.returncode != 0:
        raise RuntimeError(f"Cython 编译失败 exit={r.returncode}")
    _log("Cython OK（licensing/*.pyd 已生成）")


def nuitka_build() -> Path:
    """Nuitka --standalone 打包主程序到 dist/launcher.dist/。"""
    try:
        import nuitka  # noqa: F401, PLC0415
    except ImportError:
        _log("⚠ 未安装 nuitka，跳过 Nuitka（会生成纯 Python 包）")
        return HERE / "source"
    _log("Nuitka --standalone 编译主程序…")
    out_dir = DIST_ROOT / "_nuitka_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    ico = HERE / "icon.ico"
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--enable-plugin=multiprocessing",
        f"--include-package=dub_align_studio",
        f"--include-data-dir={SOURCE_DIR / 'web'}=dub_align_studio/web",
        "--windows-console-mode=disable" if platform.system() == "Windows" else "--no-deployment-flag=self-execution",
        "--assume-yes-for-downloads",
        f"--output-dir={out_dir}",
        f"--output-filename=水星配音对齐工作室.exe" if platform.system() == "Windows" else "--output-filename=dub_align_studio",
        str(SOURCE_DIR.parent / "dub_align_studio" / "launcher.py"),
    ]
    if ico.exists() and platform.system() == "Windows":
        cmd.insert(-1, f"--windows-icon-from-ico={ico}")
    _log(f"命令：{' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(HERE))
    if r.returncode != 0:
        raise RuntimeError(f"Nuitka 编译失败 exit={r.returncode}")
    # Nuitka 产物：out_dir/launcher.dist/
    dist_bin_dir = out_dir / "launcher.dist"
    if not dist_bin_dir.exists():
        # 有些 Nuitka 版本用 <target>.dist；尽力找一个
        for cand in out_dir.iterdir():
            if cand.is_dir() and cand.name.endswith(".dist"):
                dist_bin_dir = cand
                break
    _log(f"Nuitka OK: {dist_bin_dir}")
    return dist_bin_dir


def copy_nuitka_output(dist_bin_dir: Path) -> None:
    """把 Nuitka 产物拷贝到 DIST_ROOT，然后扫敏感文件删除。"""
    if not dist_bin_dir.exists():
        _log("Nuitka 产物目录不存在——跳过拷贝")
        return
    _log(f"拷贝产物 {dist_bin_dir} → {DIST_ROOT}")
    for item in dist_bin_dir.iterdir():
        target = DIST_ROOT / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def scrub_sensitive(root: Path) -> int:
    """遍历发行包，按 SENSITIVE_PATTERNS 删除敏感文件。返回删除数。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        # 先删文件
        for name in filenames:
            for pat in SENSITIVE_PATTERNS:
                if fnmatch.fnmatch(name, pat):
                    try:
                        (Path(dirpath) / name).unlink()
                        n += 1
                    except OSError:
                        pass
                    break
        # 再删空 __pycache__
        for d in list(dirnames):
            if d == "__pycache__":
                try:
                    shutil.rmtree(Path(dirpath) / d, ignore_errors=True)
                    n += 1
                except OSError:
                    pass
                dirnames.remove(d)
    return n


def _find_ffmpeg_binary(name: str) -> Path | None:
    """按优先级搜 ffmpeg.exe/ffprobe.exe：项目自带 → PATH → None。

    Windows 上 name 应带 .exe；其它平台可省。
    返回 Path 或 None（None 表示找不到；调用方自己决定 warn / raise）。
    """
    is_win = platform.system() == "Windows"
    stem = name.removesuffix(".exe")
    fname = f"{stem}.exe" if is_win else stem
    # 1) 项目自带
    for candidate in (
        HERE / "tools" / "ffmpeg" / fname,
        HERE / "tools" / "ffmpeg" / "bin" / fname,
    ):
        if candidate.exists():
            return candidate
    # 2) PATH 兜底
    found = shutil.which(fname) or shutil.which(name)
    if found:
        return Path(found)
    return None


def bundle_ffmpeg(dist_dir: Path) -> int:
    """把 ffmpeg / ffprobe 拷进发行目录。返回拷贝的文件数。

    找不到时不 raise（保持向后兼容 --skip-ffmpeg 老流程），只 warn；
    调用方（--with-ffmpeg 显式指定）需检查返回值决定是否退出。
    """
    is_win = platform.system() == "Windows"
    names = ("ffmpeg.exe", "ffprobe.exe") if is_win else ("ffmpeg", "ffprobe")
    n = 0
    for name in names:
        src = _find_ffmpeg_binary(name)
        if src is None:
            _log(f"⚠ 未找到 {name}（既不在 ./tools/ffmpeg/ 也不在 PATH）")
            continue
        dst = dist_dir / name
        shutil.copy2(src, dst)
        try:
            sz_mb = dst.stat().st_size / 1024 / 1024
            _log(f"内嵌 {name}  <-  {src}  ({sz_mb:.1f} MB)")
        except OSError:
            _log(f"内嵌 {name}  <-  {src}")
        n += 1
    return n


def write_launcher_bat(dist_dir: Path, exe_name: str) -> None:
    """生成 `启动软件.bat` 强制启用 license gate。"""
    if platform.system() != "Windows":
        return
    bat = dist_dir / "启动软件.bat"
    # Chinese chars in REM 注释 → 必须用 GBK（Windows cmd 默认 codepage）
    lines = [
        "@echo off",
        "REM 强制启用激活码 gate（发行版必须）",
        "set DUB_ALIGN_LICENSE_REQUIRED=1",
        "REM 强制启用 RASP strict 模式（调试器/frida/vm 检测到就退出）",
        "set DUB_ALIGN_RASP_STRICT=1",
        "cd /d \"%~dp0\"",
        f"start \"\" \"%~dp0{exe_name}\"",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="gbk")
    _log(f"生成 {bat.name}")


def write_integrity_hash(dist_dir: Path, exe_name: str) -> None:
    exe_path = dist_dir / exe_name
    if not exe_path.exists():
        _log(f"⚠ 无 exe 可算 hash：{exe_path}")
        return
    h = hashlib.sha256()
    with open(exe_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    (dist_dir / "integrity.hash").write_text(digest + "\n", encoding="ascii")
    _log(f"integrity.hash = {digest}")


def dist_manifest(dist_dir: Path) -> None:
    _log("=" * 60)
    _log(f"发行清单（{dist_dir}）：")
    total_size = 0
    for p in sorted(dist_dir.rglob("*")):
        if p.is_file():
            sz = p.stat().st_size
            total_size += sz
            rel = p.relative_to(dist_dir)
            _log(f"  {sz:>12,d} B  {rel}")
    _log(f"  ---- 合计 {total_size:,} B ({total_size / 1024 / 1024:.1f} MB) ----")


def main() -> int:
    global DIST_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-cython", action="store_true")
    ap.add_argument("--skip-nuitka", action="store_true")
    ap.add_argument("--out", default=str(DIST_ROOT))
    ap.add_argument("--with-ffmpeg", action="store_true",
                    help="内嵌 ffmpeg.exe/ffprobe.exe（用户机器不用装环境）")
    ap.add_argument("--require-ffmpeg", action="store_true",
                    help="配合 --with-ffmpeg：找不到 ffmpeg 时直接失败（CI 打包用）")
    args = ap.parse_args()

    DIST_ROOT = Path(args.out).resolve()

    _log(f"项目根：{HERE}")
    _log(f"发行目录：{DIST_ROOT}")

    clean_dist()

    if not args.skip_cython:
        cython_compile_licensing()

    dist_bin_dir = HERE / "source"
    if not args.skip_nuitka:
        dist_bin_dir = nuitka_build()

    copy_nuitka_output(dist_bin_dir)

    # 敏感文件二次清扫（保底：即使 Nuitka 内嵌了 .py，全给删了）
    scrubbed = scrub_sensitive(DIST_ROOT)
    _log(f"敏感文件已清除：{scrubbed} 项")

    exe_name = "水星配音对齐工作室.exe" if platform.system() == "Windows" \
        else "dub_align_studio"

    if args.with_ffmpeg:
        n = bundle_ffmpeg(DIST_ROOT)
        expected = 2   # ffmpeg + ffprobe
        if n < expected and args.require_ffmpeg:
            _log(f"❌ --require-ffmpeg：只拷进 {n}/{expected} 个，构建终止")
            return 3

    write_launcher_bat(DIST_ROOT, exe_name)
    write_integrity_hash(DIST_ROOT, exe_name)

    dist_manifest(DIST_ROOT)
    _log("✅ 构建完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
