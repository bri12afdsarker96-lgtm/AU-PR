# -*- coding: utf-8 -*-
"""生成环境快照（对齐版）：深扒本地仓 + 诊断与 GitHub 真源的对齐关系，写成 环境快照.txt。

与 v0.5 既有 `环境快照.py`（偏 dots.tts 引擎/API 探测）互补：本脚本聚焦"换机/新窗口对齐"——
先回答"我在哪个分支？是否= 真源 v0.5？本地到底有没有源码？数据总目录/模型/音色在不在？"，
再深扫仓库文件树与数据总目录。若同目录存在既有 `环境快照.py`，末尾自动调用它把引擎/API
深探一并追加，输出仍是一个 环境快照.txt。纯标准库；任何一步失败只记录、不中断。

用途：配合 生成环境快照.bat 一键生成并推送到 GitHub，开发者/新对话窗口直接读文件即可对齐颗粒度，
无需截图。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "环境快照.txt"
L: list[str] = []

# 真源分支：软件全套源码所在处（不是 main）。见 docs/项目交接与颗粒度对齐_20260729.md。
TRUTH_BRANCH = "release/v0.5-download-fix"
# 换机约定路径
EXPECTED_DATA_DIR = r"D:\GitHub\By\AU&PR\水星配音数据"

_UTF8_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def sec(title: str) -> None:
    L.append("")
    L.append("═" * 8 + " " + title + " " + "═" * 8)


def run(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT,
                                       timeout=timeout, encoding="utf-8", errors="replace",
                                       env=_UTF8_ENV).strip()
    except Exception as exc:  # noqa: BLE001
        return f"<失败: {exc}>"


def _have_git() -> bool:
    import shutil
    return bool(shutil.which("git"))


def tree(path: Path, depth: int = 2, max_entries: int = 40, exclude: set | None = None) -> None:
    exclude = exclude or set()
    if not path.exists():
        L.append(f"  （不存在）{path}")
        return

    def walk(p: Path, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except OSError as exc:
            L.append("  " * level + f"<无法读取: {exc}>")
            return
        shown = 0
        for e in entries:
            if e.name in exclude or e.name.startswith("."):
                continue
            if shown >= max_entries:
                L.append("  " * level + f"…（其余 {len(entries) - shown} 项略）")
                break
            shown += 1
            if e.is_dir():
                L.append("  " * level + f"📁 {e.name}/")
                walk(e, level + 1)
            else:
                try:
                    mb = e.stat().st_size / 1048576
                    stamp = datetime.fromtimestamp(e.stat().st_mtime).strftime("%m-%d %H:%M")
                    L.append("  " * level + f"   {e.name}  ({mb:.1f}MB, {stamp})")
                except OSError:
                    L.append("  " * level + f"   {e.name}")

    L.append(f"  {path}")
    walk(path, 1)


# ------------------------------------------------------------------ 仓库对齐诊断（核心）
def _behind_ahead(ref: str) -> tuple[int, int] | None:
    """返回 (落后 ref 的提交数, 领先 ref 的提交数)；ref 不可达返回 None。"""
    out = run(["git", "rev-list", "--left-right", "--count", f"{ref}...HEAD"])
    parts = out.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return None


def _git_head_fallback() -> None:
    gitdir = ROOT / ".git"
    if not gitdir.is_dir():
        L.append("  <无 .git 目录，且 git 不在 PATH——无法诊断对齐>")
        return
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            L.append(f"  分支: {ref.split('/')[-1]}（读自 .git/HEAD，git 不在 PATH）")
        else:
            L.append(f"  HEAD(detached): {head}")
    except Exception as exc:  # noqa: BLE001
        L.append(f"  <读取 .git 失败: {exc}>")
    L.append("  ⚠ git 不在 PATH：无法比对与真源分支的领先/落后。把 Git 加入系统 PATH 可获完整对齐诊断。")


def diagnose_alignment() -> None:
    sec("仓库对齐诊断（本地 vs main vs 真源 v0.5）")
    if not _have_git():
        _git_head_fallback()
        return

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    head = run(["git", "log", "--oneline", "-1"])
    L.append(f"  当前分支: {branch}")
    L.append(f"  HEAD: {head}")
    L.append(f"  真源分支应为: {TRUTH_BRANCH}（软件全套源码所在；main 只有文档）")

    # 与真源、main 的领先/落后（依赖远程跟踪引用，bat 已先 git fetch）
    for label, ref in (("真源 v0.5", f"origin/{TRUTH_BRANCH}"), ("main", "origin/main")):
        ba = _behind_ahead(ref)
        if ba is None:
            L.append(f"  vs {label}（{ref}）: <该引用不可达，先 `git fetch origin`>")
        else:
            behind, ahead = ba
            verdict = "✅ 完全一致" if (behind == 0 and ahead == 0) else \
                      f"落后 {behind} / 领先 {ahead}"
            L.append(f"  vs {label}（{ref}）: {verdict}")

    # 明确结论
    has_source = (ROOT / "source" / "dub_align_studio" / "launcher.py").exists()
    ba_truth = _behind_ahead(f"origin/{TRUTH_BRANCH}")
    L.append("")
    if not has_source:
        L.append("  ❗ 本地缺 source/dub_align_studio —— 极可能停在 main（只有文档）。")
        L.append(f"     对齐命令：git fetch origin && git checkout {TRUTH_BRANCH} && git pull")
    elif ba_truth == (0, 0):
        L.append("  ✅ 本地已与真源 v0.5 完全对齐，且含全套源码。可直接开发。")
    elif ba_truth and ba_truth[0] > 0:
        L.append(f"  ⚠ 本地落后真源 {ba_truth[0]} 个提交。执行：git pull origin {TRUTH_BRANCH}")
    else:
        L.append("  ℹ 本地含源码但不在真源 tip（可能在本次对齐工作分支或有本地提交），请人工确认。")

    status = run(["git", "status", "--short"])
    L.append("  未提交改动: " + ("（干净）" if not status else "\n    " + status.replace("\n", "\n    ")))
    L.append("  近5次提交:")
    L.append("    " + run(["git", "log", "--oneline", "-5"]).replace("\n", "\n    "))


# ------------------------------------------------------------------ 仓库深扫（深扒本地仓）
def _dir_stat(path: Path, suffixes: tuple[str, ...] | None = None) -> tuple[int, float]:
    """递归统计文件数与总大小(MB)。suffixes 给定则只统计这些后缀。"""
    n, size = 0, 0
    if not path.is_dir():
        return 0, 0.0
    for p in path.rglob("*"):
        if p.is_file() and (suffixes is None or p.suffix.lower() in suffixes):
            n += 1
            try:
                size += p.stat().st_size
            except OSError:
                pass
    return n, size / 1048576


def deep_scan_repo() -> None:
    sec("仓库深扫（本地仓结构与规模）")
    src = ROOT / "source"
    das = src / "dub_align_studio"
    iw = src / "integrated_workbench"
    tests = ROOT / "tests"

    has_source = das.exists()
    L.append(f"  含软件源码 source/: {'是 ✅' if has_source else '否 ❗（仅文档，未对齐真源）'}")

    for label, d in (("dub_align_studio", das), ("integrated_workbench", iw), ("tests", tests)):
        n, mb = _dir_stat(d, (".py",) if label != "tests" else (".py",))
        L.append(f"  {label}: {n} 个 .py, {mb:.1f}MB" if d.exists() else f"  {label}: （不存在）")

    # 关键文件在否（对齐纪律的骨架）
    key = {
        "launcher.py（打包入口）": das / "launcher.py",
        "render_b.py（帧收口）": das / "render_b.py",
        "timing.py（尺子接口）": das / "timing.py",
        "settings.py（数据总目录）": das / "settings.py",
        "web/index.html（界面）": das / "web" / "index.html",
        "engines/dots_local.py": das / "engines" / "dots_local.py",
        "edit_compose.py（五档裁剪）": iw / "edit_compose.py",
    }
    L.append("  关键文件:")
    for name, p in key.items():
        L.append(f"    {'✅' if p.exists() else '❌'} {name}")

    sec("仓库根目录（深度1）")
    tree(ROOT, depth=1, exclude={"git", ".git", "build", "dist", "__pycache__", "发布包", "整合离线包"})

    sec("docs/（口径与交接文档）")
    tree(ROOT / "docs", depth=1, max_entries=30)


# ------------------------------------------------------------------ Python 环境
def python_env() -> None:
    sec("Python 环境")
    L.append(f"  python: {sys.executable}")
    L.append(f"  版本: {sys.version.split()[0]}")
    L.append(f"  frozen(打包版): {getattr(sys, 'frozen', False)}")
    for pkg in ("torch", "torchaudio", "torchvision", "torchcodec",
                "transformers", "accelerate", "dots-tts", "soundfile", "pyinstaller"):
        info = run([sys.executable, "-m", "pip", "show", pkg])
        ver = next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                    if ln.startswith("Version:")), None)
        L.append(f"  {pkg}: {ver or '未安装'}")


# ------------------------------------------------------------------ 数据总目录
def data_dir_checkup() -> None:
    sec("数据总目录（模型/音色/字体）")
    data_dir: Path | None = None
    src = ROOT / "source"
    if (src / "dub_align_studio" / "settings.py").exists():
        try:
            sys.path.insert(0, str(src))
            from dub_align_studio.settings import data_root  # type: ignore
            data_dir = Path(data_root())
            L.append(f"  软件当前指向(data_root): {data_dir}")
        except Exception as exc:  # noqa: BLE001
            L.append(f"  <settings.data_root 读取失败: {exc}>")
    if data_dir is None:
        data_dir = Path(EXPECTED_DATA_DIR)
        L.append(f"  （无法从 settings 读取，回退到换机约定路径）{data_dir}")

    if str(data_dir) != EXPECTED_DATA_DIR:
        L.append(f"  ⚠ 与换机约定路径不一致，约定为: {EXPECTED_DATA_DIR}")
        L.append("     在软件『工具箱自检』页把数据总目录指向约定路径，即可复用已下载模型/音色。")

    tree(data_dir, depth=2, max_entries=25, exclude={"__pycache__"})

    # 音色库参考音频体检（超 30s 标红：dots.tts 只吃 5–30s）
    vroot = data_dir / "音色库"
    if vroot.is_dir():
        L.append("  音色库参考体检:")
        for d in sorted(vroot.iterdir()):
            if not d.is_dir():
                continue
            ref = next((p for p in d.iterdir()
                        if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac")), None)
            if ref is None:
                L.append(f"    📁 {d.name}: <未找到参考音频>")
                continue
            info = _audio_seconds(ref)
            L.append(f"    📁 {d.name}/{ref.name}: {info}")


def _audio_seconds(path: Path) -> str:
    size_mb = path.stat().st_size / 1048576 if path.exists() else 0.0
    dur = None
    try:
        import soundfile as sf  # 可选
        dur = sf.info(str(path)).duration
    except Exception:
        import shutil as _sh
        exe = _sh.which("ffprobe")
        if exe:
            out = run([exe, "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", str(path)])
            try:
                dur = float(out)
            except Exception:
                pass
    if dur is None:
        return f"{size_mb:.1f}MB · 时长未知"
    flag = "  ⚠ 超 30s：dots.tts 只吃 5–30s，过长会劣化克隆" if dur > 30 else ""
    return f"{size_mb:.1f}MB · 时长 {dur:.1f}s{flag}"


# ------------------------------------------------------------------ ffmpeg
def ffmpeg_checkup() -> None:
    sec("ffmpeg / ffprobe")
    import shutil
    for name in ("ffmpeg", "ffprobe"):
        hit = shutil.which(name)
        if hit:
            L.append(f"  {name}: {hit}（在 PATH）")
            continue
        # 找已知落点
        for cand in (ROOT / f"{name}.exe",
                     ROOT / "dist" / "水星配音对齐工作室" / f"{name}.exe",
                     Path(EXPECTED_DATA_DIR) / f"{name}.exe"):
            if cand.exists():
                L.append(f"  {name}: {cand}（非 PATH，已在已知目录）")
                break
        else:
            L.append(f"  {name}: ❌ 未找到（渲染/量时长需要它；放 exe 同目录、数据总目录或 PATH）")


# ------------------------------------------------------------------ 编译自检
def compile_check() -> None:
    sec("编译自检（compileall source）")
    if not (ROOT / "source").is_dir():
        L.append("  （无 source/，跳过——未对齐真源）")
        return
    out = run([sys.executable, "-m", "compileall", "-q", "source"], timeout=120)
    L.append("  " + ("✅ 编译全绿" if not out or out == "" else out.replace("\n", "\n  ")))


# ------------------------------------------------------------------ 追加既有引擎深探
def append_engine_probe() -> None:
    legacy = ROOT / "环境快照.py"
    if not legacy.exists():
        return
    sec("dots.tts 引擎/API 深探（调用既有 环境快照.py）")
    L.append("  （以下由仓库既有 环境快照.py 生成，含引擎 probe 与 generate 真实签名）")
    # 既有脚本把结果写进它默认的 环境快照.txt（与本脚本同名）。这里在最终写盘前先跑它、
    # 读出其引擎相关段附加进来；随后本脚本整体重写 环境快照.txt，得到"对齐 + 引擎"合并版。
    try:
        run([sys.executable, str(legacy)], timeout=240)
        produced = ROOT / "环境快照.txt"
        if produced.exists():
            text = produced.read_text(encoding="utf-8", errors="replace")
            idx = text.find("引擎探测")  # 从引擎段开始截，跳过既有脚本自己的头部
            snippet = text[idx - 9:] if idx > 9 else text
            for ln in snippet.splitlines():
                L.append("  " + ln)
    except Exception as exc:  # noqa: BLE001
        L.append(f"  <既有引擎深探失败: {exc}>")


def main() -> int:
    L.append(f"AU&PR · 环境快照（对齐版）  {datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append(f"仓库: {ROOT}")

    diagnose_alignment()
    deep_scan_repo()
    python_env()
    data_dir_checkup()
    ffmpeg_checkup()
    compile_check()
    append_engine_probe()

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"[OK] 快照已生成：{OUT}（{OUT.stat().st_size // 1024}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
