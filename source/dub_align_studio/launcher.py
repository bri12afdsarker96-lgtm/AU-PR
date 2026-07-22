"""水星配音对齐工作室——打包/独立启动入口（PyInstaller 用这个文件打包）。

为什么不用 __main__.py 打包：PyInstaller 把入口当顶层脚本执行，相对导入
（from .web_server import …）会当场崩溃，黑窗一闪即逝，表现为「双击无响应」。
本文件只用绝对导入，并带三层护栏：
    1) exe 同目录加入 PATH——ffmpeg.exe/ffprobe.exe 放旁边即可被识别；
    2) 端口占用自动顺延（8760 起试 20 个）；
    3) 任何启动异常写入 exe 旁「启动错误.log」并停窗展示，绝不无声消失。
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    multiprocessing.freeze_support()  # Windows 冻结环境守则

    app_dir = _app_dir()
    # exe 同目录优先进 PATH：用户把 ffmpeg.exe 放旁边即可用（轻量安装纪律）
    os.environ["PATH"] = str(app_dir) + os.pathsep + os.environ.get("PATH", "")

    try:
        from dub_align_studio.web_server import main as web_main

        return web_main()
    except Exception:
        detail = traceback.format_exc()
        log_path = app_dir / "启动错误.log"
        try:
            log_path.write_text(detail, encoding="utf-8")
        except Exception:
            pass
        print("启动失败，错误详情已写入：", log_path)
        print(detail)
        try:
            input("按回车键退出…")
        except EOFError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
