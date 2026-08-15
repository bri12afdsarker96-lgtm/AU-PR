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
    "*.pyx", "*.pxd", "*.c", "*.h",                  # Cython 中间产物（源码等价物）
    "README.md", "README*.md", "PROTECTION.md",
    "*.md",                                          # 各种文档
    "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "conftest.py",
    ".git*", "*.gitignore",
)

# 但 _internal/dub_align_studio/ 下的 .pyc/.pyd 是 PyInstaller 打包产物 **必须保留**
# scrub_sensitive 会用 SENSITIVE_KEEP_DIRS 做例外
SENSITIVE_KEEP_DIRS = ("_internal",)


def _log(msg: str) -> None:
    print(f"[build_dist] {msg}", flush=True)


def clean_dist() -> None:
    if DIST_ROOT.exists():
        _log(f"清理旧的 {DIST_ROOT}")
        shutil.rmtree(DIST_ROOT, ignore_errors=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)


_LICENSING_BACKUP = HERE / ".licensing_src_backup"


def backup_licensing_sources() -> None:
    """Cython 会**删掉 source/licensing/*.py**（git 工作树里的源码！）。
    编译前必须先备份，打包完 restore 回来，否则仓库源码被销毁。"""
    lic_dir = SOURCE_DIR / "licensing"
    if _LICENSING_BACKUP.exists():
        shutil.rmtree(_LICENSING_BACKUP, ignore_errors=True)
    _LICENSING_BACKUP.mkdir(parents=True, exist_ok=True)
    for py in lic_dir.glob("*.py"):
        shutil.copy2(py, _LICENSING_BACKUP / py.name)
    _log(f"[OK] 已备份 licensing/*.py → {_LICENSING_BACKUP.name}（打包后自动恢复）")


def restore_licensing_sources() -> None:
    """把备份的 licensing/*.py 恢复回工作树，并清掉 Cython 产物（.pyd/.c/.so）。
    保证仓库回到打包前的干净状态。"""
    if not _LICENSING_BACKUP.exists():
        _log("[!] 无 licensing 源码备份，跳过恢复（若源码丢失请 git checkout）")
        return
    lic_dir = SOURCE_DIR / "licensing"
    # 先删 Cython 产物
    for pat in ("*.pyd", "*.so", "*.c"):
        for f in lic_dir.glob(pat):
            try:
                f.unlink()
            except OSError:
                pass
    # 恢复 .py
    n = 0
    for py in _LICENSING_BACKUP.glob("*.py"):
        shutil.copy2(py, lic_dir / py.name)
        n += 1
    shutil.rmtree(_LICENSING_BACKUP, ignore_errors=True)
    _log(f"[OK] 已恢复 {n} 个 licensing/*.py 源码，清理 Cython 产物")


def cython_compile_licensing() -> int:
    """Cython 编译 licensing/*.py → 原地 .pyd/.so，**编译成功后删掉对应 .py 源码**。

    为什么必须删 .py：
      若同目录同时存在 client.py 和 client.pyd，PyInstaller 会把 .py 也打成
      .pyc 进 PYZ，冻结运行时 PYZ importer 可能先命中 .pyc → .pyd 白编译。
      删掉 .py 后 PyInstaller 只见 .pyd → 打成二进制扩展 → 运行时只能加载 .pyd
      （无源码可反编译）。

    健壮性：
      - 逐个模块编译，单个失败只让那个模块退回 .py，不影响其他模块
      - 清理 .c 中间产物 + build/ 临时目录（.c 等价源码，绝不能留）

    要求：setuptools + C 编译器
      - Windows: MSVC Build Tools（Visual C++ 14.x）
      - Linux/macOS: gcc/clang

    返回：成功编译成 .pyd/.so 的模块数。
    """
    lic_dir = SOURCE_DIR / "licensing"
    # __init__.py 作为包入口，编成扩展较脆弱（包 init 语义），保留为源码；
    # 真正含密钥/HMAC/RASP/加密逻辑的是下面这些子模块，全部编成 .pyd。
    py_files = [p for p in sorted(lic_dir.glob("*.py")) if p.name != "__init__.py"]
    if not py_files:
        _log("licensing/ 无 .py 需要编译")
        return 0
    try:
        import Cython  # noqa: F401, PLC0415
    except ImportError:
        _log("[!] 未安装 cython，跳过（licensing 只有 .pyc，可反编译）。pip install cython")
        return 0

    is_win = platform.system() == "Windows"
    ext = ".pyd" if is_win else ".so"
    compiled = 0
    failed: list[str] = []
    for py in py_files:
        _log(f"Cython 编译 {py.name} …")
        cmd = [sys.executable, "-m", "Cython.Build.Cythonize",
               "-i", "-3", str(py)]
        r = subprocess.run(cmd, cwd=str(HERE))
        # 检查是否真产出了扩展模块（stem.*.pyd 或 stem.pyd）
        produced = list(lic_dir.glob(f"{py.stem}*{ext}"))
        if r.returncode == 0 and produced:
            # 删源码 .py（关键！否则 PyInstaller 会打 .pyc 盖过 .pyd）
            try:
                py.unlink()
            except OSError:
                pass
            compiled += 1
        else:
            failed.append(py.name)
            _log(f"[!] {py.name} 编译失败（exit={r.returncode}）→ 该模块保留 .py")

    # 清理 .c 中间产物（等价源码）+ Cython build/ 临时目录
    for c in lic_dir.glob("*.c"):
        try:
            c.unlink()
        except OSError:
            pass
    _bt = HERE / "build"
    if _bt.exists():
        shutil.rmtree(_bt, ignore_errors=True)

    if compiled:
        _log(f"[OK] Cython 编译成功 {compiled}/{len(py_files)} 个 licensing 模块 → {ext}")
    if failed:
        _log(f"[!] 未编译（保留 .py）：{', '.join(failed)}")
    return compiled


def _find_icon() -> Path | None:
    """按优先级找应用图标：installer\\app.ico > icon.ico > None。"""
    for candidate in (
        HERE / "installer" / "app.ico",
        HERE / "icon.ico",
        HERE / "app.ico",
    ):
        if candidate.exists():
            return candidate
    return None


def nuitka_build(strict: bool = True) -> Path:
    """Nuitka --standalone 打包主程序到 dist/launcher.dist/。

    strict=True（默认，发行必须）：缺 nuitka 直接 raise，不再"跳过"生成
    纯 Python 包（那样打出来的 setup.exe 装完根本没 .exe 可运行）。
    """
    try:
        import nuitka  # noqa: F401, PLC0415
    except ImportError:
        msg = ("未安装 nuitka —— 发行包必须编译产 exe，不能跳过。"
               "\n    修复：pip install nuitka  然后重跑本脚本。"
               "\n    若只想生成 Python 源码目录用于开发调试，加 --skip-nuitka。")
        if strict:
            raise RuntimeError(msg)
        _log(f"[!] {msg}  （--skip-nuitka 已指定，继续但不会有 exe）")
        return HERE / "source"
    _log("Nuitka --standalone 编译主程序...")
    out_dir = DIST_ROOT / "_nuitka_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    ico = _find_icon()
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
    if ico is not None and platform.system() == "Windows":
        cmd.insert(-1, f"--windows-icon-from-ico={ico}")
        _log(f"图标：{ico}")
    elif platform.system() == "Windows":
        _log("[!] 未找到 icon.ico —— exe 用 Nuitka 默认图标")
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


def verify_no_personal_data(root: Path) -> list[str]:
    """扫发行目录，查是否有个人配置痕迹泄露。
    命中即返回违规路径列表；打包脚本调用后若非空应中止（防止发用户 URL/激活码）。

    检查项：
      - settings.json / license.json / gpu_state.json 文件（应已被 scrub 但双保险）
      - 任意 JSON/TXT 里出现 workers.dev（用户自建 Cloudflare Worker 域）
      - 任意 JSON/TXT 里出现激活服务器 IP（101.201.108.8）
    """
    violations: list[str] = []
    sensitive_names = {"settings.json", "license.json", "gpu_state.json",
                       "gpu_profiles.json", "queue.sqlite3"}
    # 内容级 patterns（防 config JSON 漏网）
    forbidden_substrings = (b"workers.dev", b"101.201.108.8")
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            # 文件名黑名单
            if name in sensitive_names:
                violations.append(f"文件泄漏：{full.relative_to(root)}")
                continue
            # 只扫描小文本文件（避免大型 dll/exe）
            try:
                sz = full.stat().st_size
            except OSError:
                continue
            if sz > 512 * 1024:  # 512 KB 上限
                continue
            if full.suffix.lower() not in (".json", ".txt", ".ini", ".cfg", ".yaml", ".yml"):
                continue
            try:
                data = full.read_bytes()
            except OSError:
                continue
            for pat in forbidden_substrings:
                if pat in data:
                    violations.append(
                        f"内容泄漏 [{pat.decode('ascii', 'ignore')}]："
                        f"{full.relative_to(root)}",
                    )
                    break
    return violations


def scrub_sensitive(root: Path) -> int:
    """遍历发行包，按 SENSITIVE_PATTERNS 删除敏感文件。返回删除数。

    例外：SENSITIVE_KEEP_DIRS 里的目录（如 _internal/）保留 .pyc/.pyd
    —— 它们是 PyInstaller 打包必需，删了应用起不来。
    """
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        # 判断当前目录是否属于「保留区」——PyInstaller 打包产物
        rel_parts = Path(dirpath).relative_to(root).parts
        in_keep = any(part in SENSITIVE_KEEP_DIRS for part in rel_parts)
        # 先删文件
        for name in filenames:
            for pat in SENSITIVE_PATTERNS:
                if fnmatch.fnmatch(name, pat):
                    # 保留区里，.pyc/.pyd/.pyo 是必需产物；.py 仍应删
                    if in_keep and pat in ("*.pyc", "*.pyo", "*.pyw"):
                        break
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

    # 【关键】内嵌完 ffmpeg 立刻验证它带不带 h264_nvenc——不带的话用户机器
    # 永远只能 CPU 编码。在打包当下就大声警告，别等装到用户机器才发现。
    ff = dist_dir / ("ffmpeg.exe" if is_win else "ffmpeg")
    if ff.exists():
        try:
            _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run([str(ff), "-hide_banner", "-encoders"],
                               capture_output=True, text=True, timeout=15,
                               creationflags=_flags)
            enc = (r.stdout or "") + (r.stderr or "")
            if "h264_nvenc" in enc:
                _log("[OK] 内嵌 ffmpeg **带 h264_nvenc** → 用户机器可用 NVIDIA 硬件编码")
            else:
                _log("=" * 60)
                _log("[X] 警告：内嵌的 ffmpeg **不带 h264_nvenc**！")
                _log("    → 用户机器无论怎么选 NVIDIA，都只能 CPU(libx264) 编码。")
                _log("    → 修复：先跑 准备ffmpeg.bat 把 gyan.dev full 版 ffmpeg")
                _log("      放进 tools\\ffmpeg\\，再重新打包。")
                _log("=" * 60)
        except Exception as exc:  # noqa: BLE001
            _log(f"[!] 无法验证内嵌 ffmpeg 的 nvenc 支持：{exc}")
    return n


def write_launcher_bat(dist_dir: Path, exe_name: str) -> None:
    """生成两个启动器：
        启动软件.bat  —— 传统 bat（会一闪 cmd 窗；开发调试用）
        启动软件.vbs  —— VBS 静默启动器（**完全无 cmd 窗口**，用户默认走这个）

    两者都会 set DUB_ALIGN_LICENSE_REQUIRED=1 + DUB_ALIGN_RASP_STRICT=1
    才能启用激活码 gate + RASP strict。
    """
    if platform.system() != "Windows":
        return

    # ---------- 1) bat（保留，兼容命令行/调试）----------
    bat = dist_dir / "启动软件.bat"
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

    # ---------- 2) vbs（默认，完全无窗口）----------
    # 原理：WshShell.Environment("Process") 设的 env 只影响本进程 + 其派生子进程
    # 然后 shell.Run 拉起 exe（子进程），子进程继承带 env 的环境
    # Run 的 intWindowStyle=0 → 完全隐藏窗口；bWaitOnReturn=False → 不阻塞退出
    vbs = dist_dir / "启动软件.vbs"
    vbs_body = (
        "' 水星配音对齐工作室 · 静默启动器（无任何 cmd 窗口）\r\n"
        "Option Explicit\r\n"
        "Dim shell, fso, appDir, env, exePath\r\n"
        "Set shell = CreateObject(\"WScript.Shell\")\r\n"
        "Set fso = CreateObject(\"Scripting.FileSystemObject\")\r\n"
        "appDir = fso.GetParentFolderName(WScript.ScriptFullName)\r\n"
        "Set env = shell.Environment(\"Process\")\r\n"
        "env(\"DUB_ALIGN_LICENSE_REQUIRED\") = \"1\"\r\n"
        "env(\"DUB_ALIGN_RASP_STRICT\") = \"1\"\r\n"
        "shell.CurrentDirectory = appDir\r\n"
        f"exePath = appDir & \"\\{exe_name}\"\r\n"
        "shell.Run \"\"\"\" & exePath & \"\"\"\", 0, False\r\n"
    )
    vbs.write_text(vbs_body, encoding="gbk")
    _log(f"生成 {vbs.name}（推荐 · 完全无窗口）")


def write_integrity_hash(dist_dir: Path, exe_name: str,
                          hmac_key: bytes | None = None) -> str:
    """把 exe 的完整性哈希写出。

    hmac_key 不为空 → HMAC-SHA256(exe, key)，返回 hex；不写 side-file
        （baseline 会被 write_build_info 塞进 _build_info.py，
         攻击者不知道 key 就伪造不了）
    hmac_key 为空 → 老逻辑，SHA256 写到 integrity.hash side-file
    """
    exe_path = dist_dir / exe_name
    if not exe_path.exists():
        _log(f"[!] 无 exe 可算 hash：{exe_path}")
        return ""
    if hmac_key:
        import hmac as _hmac
        h = _hmac.new(hmac_key, b"", hashlib.sha256)
    else:
        h = hashlib.sha256()
    with open(exe_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    if hmac_key:
        _log(f"integrity HMAC = {digest[:16]}... (藏在 _build_info)")
    else:
        (dist_dir / "integrity.hash").write_text(digest + "\n", encoding="ascii")
        _log(f"integrity.hash = {digest}")
    return digest


def prepare_build_info() -> Path:
    """**打包前**在 source/dub_align_studio/_build_info.py 写入 PACKAGED=True
    + 随机 HMAC/XOR 密钥。这样 pyinstaller 才会把它编成 .pyc 打进 PYZ。
    运行时 `from . import _build_info` 才 import 得到。

    exe HMAC 因为 chicken-egg 无法在这一步算（exe 还没生成）；后续
    finalize_build_info_hmac() 生成 sidecar HMAC 供 integrity_check 参考。
    """
    import secrets
    from datetime import datetime
    integrity_key = secrets.token_bytes(32)
    xor_seed = secrets.token_bytes(32)
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    content = (
        "# -*- coding: utf-8 -*-\n"
        "# GENERATED by build_dist.prepare_build_info() —— DO NOT EDIT\n"
        "PACKAGED = True\n"
        # version.py 读的是 BUILD_STAMP（不是 BUILD_TIMESTAMP）——两个都写，
        # 否则界面右上角会显示"源码运行"（等于宣告这是没保护的开发版）。
        f"BUILD_STAMP = {ts!r}\n"
        f"BUILD_TIMESTAMP = {ts!r}\n"
        f"INTEGRITY_HMAC_KEY = {integrity_key!r}\n"
        f"XOR_SEED = {xor_seed!r}\n"
        "EXE_HMAC_HEX = ''\n"
        "SERVER_URL_ENC = ''\n"
        "APP_ID_ENC = ''\n"
    )
    target = SOURCE_DIR / "_build_info.py"
    target.write_text(content, encoding="utf-8")
    # 把 integrity_key 另存到临时文件——因为 _build_info.py 可能被 Cython 编译成
    # .pyd 后删掉源码，finalize_build_info_hmac 就读不到 key 了，改从这里读。
    _BUILD_INFO_KEY_TMP.write_text(integrity_key.hex(), encoding="ascii")
    _log(f"[OK] source/_build_info.py 写入（pyinstaller 会打进 PYZ）")
    return target


# integrity_key 的临时落脚（打包完清掉）
_BUILD_INFO_KEY_TMP = HERE / ".build_info_key.tmp"


def cython_compile_build_info() -> bool:
    """把 source/dub_align_studio/_build_info.py 也 Cython 编成 .pyd。

    为什么必须：_build_info 里有 PACKAGED=True（gate/RASP 的总开关）+
    INTEGRITY_HMAC_KEY + XOR_SEED。若只被 PyInstaller 编成 .pyc，攻击者
    pyinstxtractor 抠出 .pyc → 把 PACKAGED 改 False（或反编译拿 key）就破了。
    编成 .pyd 后是二进制扩展，改标志/挖 key 的成本高一个数量级。

    返回 True=成功编成 .pyd 并删了 .py；False=没编（缺 Cython/MSVC，退回 .pyc）。
    """
    bi = SOURCE_DIR / "_build_info.py"
    if not bi.exists():
        return False
    try:
        import Cython  # noqa: F401, PLC0415
    except ImportError:
        _log("[!] 无 Cython，_build_info 退回 .pyc（PACKAGED/密钥可被 patch）")
        return False
    ext = ".pyd" if platform.system() == "Windows" else ".so"
    cmd = [sys.executable, "-m", "Cython.Build.Cythonize", "-i", "-3", str(bi)]
    r = subprocess.run(cmd, cwd=str(HERE))
    produced = list(SOURCE_DIR.glob(f"_build_info*{ext}"))
    if r.returncode == 0 and produced:
        try:
            bi.unlink()
        except OSError:
            pass
        for c in SOURCE_DIR.glob("_build_info*.c"):
            try:
                c.unlink()
            except OSError:
                pass
        _bt = HERE / "build"
        if _bt.exists():
            shutil.rmtree(_bt, ignore_errors=True)
        _log(f"[OK] _build_info 已编译成 {ext}（PACKAGED/密钥进二进制，无源码可 patch）")
        return True
    _log(f"[!] _build_info Cython 编译失败（exit={r.returncode}）→ 退回 .pyc")
    return False


def cleanup_build_info() -> None:
    """打包完清 source/_build_info.py + .pyd/.so/.c + key 临时文件，保持仓库干净。"""
    for pat in ("_build_info.py", "_build_info*.pyd", "_build_info*.so", "_build_info*.c"):
        for f in SOURCE_DIR.glob(pat):
            try:
                f.unlink()
                _log(f"清理 {f.name}")
            except OSError:
                pass
    if _BUILD_INFO_KEY_TMP.exists():
        try:
            _BUILD_INFO_KEY_TMP.unlink()
        except OSError:
            pass


def finalize_build_info_hmac(dist_dir: Path, exe_name: str) -> str:
    """算 exe HMAC 写 sidecar (dist_dir/_build_hmac.dat)。
    key 藏在 _build_info(.pyd/.pyc)，sidecar 只有 hash 值本身。
    攻击者要伪造 HMAC 需要先从二进制挖 key。

    key 来源优先：.build_info_key.tmp（prepare 时落的）→ 若无再尝试读 .py。"""
    integrity_key = b""
    if _BUILD_INFO_KEY_TMP.exists():
        try:
            integrity_key = bytes.fromhex(_BUILD_INFO_KEY_TMP.read_text(encoding="ascii").strip())
        except Exception:  # noqa: BLE001
            integrity_key = b""
    if len(integrity_key) < 32:
        bi_path = SOURCE_DIR / "_build_info.py"
        if bi_path.exists():
            _ns: dict = {}
            exec(bi_path.read_text(encoding="utf-8"), _ns)  # noqa: S102
            k = _ns.get("INTEGRITY_HMAC_KEY", b"")
            if isinstance(k, (bytes, bytearray)):
                integrity_key = bytes(k)
    if len(integrity_key) < 32:
        _log("[!] 拿不到 integrity_key，跳过 HMAC baseline")
        return ""
    exe_path = dist_dir / exe_name
    if not exe_path.exists():
        return ""
    import hmac as _hmac
    h = _hmac.new(integrity_key, b"", hashlib.sha256)
    with open(exe_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    (dist_dir / "_build_hmac.dat").write_text(
        "hmac:" + digest + "\n", encoding="ascii",
    )
    _log("[OK] exe HMAC baseline sidecar 已写入（key 藏在 _build_info 二进制）")
    return digest


# 老 API 名保留，转成 finalize 语义（bat 里可能仍在调）
def write_build_info(dist_dir: Path, exe_name: str) -> Path:
    finalize_build_info_hmac(dist_dir, exe_name)
    return SOURCE_DIR / "_build_info.py"


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

    # 关键校验：exe 必须存在，否则打出来的 setup 装完是空壳，
    # [Run] 阶段会 CreateProcess failed; code 2（文件不存在）
    if not args.skip_nuitka:
        exe_path = DIST_ROOT / exe_name
        if not exe_path.exists():
            _log("=" * 60)
            _log(f"[X] 关键失败：{exe_path} 不存在")
            _log("    很可能 Nuitka 编译成功但输出目录/文件名不对——检查")
            _log(f"    {DIST_ROOT / '_nuitka_out'} 下有什么。")
            _log("    切勿把这个 dist 用去打 installer——装完会 CreateProcess"
                 " failed; code 2（正是你现在看到的错）。")
            return 4
        sz_mb = exe_path.stat().st_size / 1024 / 1024
        _log(f"[OK] 主 exe 存在：{exe_path.name}  ({sz_mb:.1f} MB)")

    dist_manifest(DIST_ROOT)
    _log("[OK] 构建完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
