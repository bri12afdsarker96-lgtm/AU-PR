# -*- coding: utf-8 -*-
"""环境快照：把本机目录结构 + Python 环境 + 组件状态抓成一个文本文件（环境快照.txt）。

用途：配合 环境快照.bat 一键生成并推送到 GitHub，开发者直接读文件即可看到
本机真实状态（目录/版本/依赖/引擎探测），不用来回截图。纯标准库，任何一步
失败都只记录错误、不中断。输出做了限幅，不会把大目录整棵吐出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parent / "环境快照.txt"
ROOT = Path(__file__).resolve().parent
L: list[str] = []


def sec(title: str) -> None:
    L.append("")
    L.append("═" * 8 + " " + title + " " + "═" * 8)


def run(cmd: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT,
                                       timeout=timeout, encoding="utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        return f"<失败: {exc}>"


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


def main() -> int:
    L.append(f"水星配音对齐工作室 · 环境快照  {datetime.now():%Y-%m-%d %H:%M:%S}")

    sec("Python 环境")
    L.append(f"  python: {sys.executable}")
    L.append(f"  版本: {sys.version.split()[0]}")
    for pkg in ("torch", "torchaudio", "torchvision", "torchcodec",
                "transformers", "accelerate", "dots-tts", "soundfile", "pyinstaller"):
        info = run([sys.executable, "-m", "pip", "show", pkg])
        ver = next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                    if ln.startswith("Version:")), None)
        L.append(f"  {pkg}: {ver or '未安装'}")

    sec("引擎探测（dots.tts probe）")
    probe = run([sys.executable, "-c",
                 "import sys;sys.path.insert(0,'source');"
                 "from dub_align_studio.engines.dots_local import DotsLocalEngine as E;"
                 "s=E().probe();print('available=',s.available);print(s.detail)"], timeout=120)
    L.append("  " + probe.replace("\n", "\n  "))

    sec("仓库状态")
    L.append("  分支: " + run(["git", "rev-parse", "--abbrev-ref", "HEAD"]))
    L.append("  HEAD: " + run(["git", "log", "--oneline", "-1"]))
    L.append("  近5次提交:")
    L.append("    " + run(["git", "log", "--oneline", "-5"]).replace("\n", "\n    "))
    status = run(["git", "status", "--short"])
    L.append("  未提交改动: " + ("（干净）" if not status else "\n    " + status.replace("\n", "\n    ")))

    sec("仓库根目录（深度1）")
    tree(ROOT, depth=1, exclude={"git", ".git", "build", "__pycache__"})

    sec("父目录（检查散落的旧脚本副本）")
    tree(ROOT.parent, depth=1, max_entries=30)

    sec("dist 产物")
    tree(ROOT / "dist", depth=2, max_entries=15)

    sec("发布包")
    tree(ROOT / "发布包", depth=1, max_entries=15)

    sec("整合离线包")
    tree(ROOT / "整合离线包", depth=2, max_entries=15)

    sec("数据总目录")
    data_dir = None
    try:
        sys.path.insert(0, str(ROOT / "source"))
        from dub_align_studio.settings import data_root
        data_dir = data_root()
        L.append(f"  软件当前指向: {data_dir}")
    except Exception as exc:  # noqa: BLE001
        L.append(f"  <读取失败: {exc}>")
    if data_dir:
        tree(Path(data_dir), depth=3, max_entries=25, exclude={"__pycache__"})

    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"✅ 快照已生成：{OUT}（{OUT.stat().st_size // 1024}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
