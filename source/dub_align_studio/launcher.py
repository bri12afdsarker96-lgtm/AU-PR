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
    # 让「放 ffmpeg.exe 旁边即可用」在两种启动方式下都成立（轻量安装纪律）：
    #   - 冻结 exe：app_dir 就是 exe 目录；
    #   - 整合离线包：启动.bat 跑 source\dub_align_studio\launcher.py，app_dir 是深层包目录，
    #     真正让人放 ffmpeg 的是「启动.bat 旁」= 包根目录（parents[2]），也一并进 PATH。
    extra_dirs = [app_dir]
    if not getattr(sys, "frozen", False):
        pkg_root = Path(__file__).resolve().parents[2]  # …\<整合包>\source\dub_align_studio → 包根
        extra_dirs.append(pkg_root)
        extra_dirs.append(pkg_root / "bin")             # 也支持放进 bin\ 子目录
    prefix = os.pathsep.join(str(d) for d in extra_dirs if d)
    os.environ["PATH"] = prefix + os.pathsep + os.environ.get("PATH", "")

    try:
        from dub_align_studio import syspy

        note = syspy.bridge_site_packages()  # 打包 exe：桥接系统 Python 的依赖（dots.tts/torch）
        if note:
            print(note)

        # 优先原生窗口（pywebview + Edge WebView2）
        # 不可用则 return None，降级为浏览器模式（老流程）
        from dub_align_studio.native_window import run_with_native_window
        rc = run_with_native_window()
        if rc is not None:
            return rc

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
        # 窗口化 exe（console=False）里 input() 立刻 EOFError → 一闪而过；
        # 用 Windows 原生消息框弹一下，用户至少看到错误后再退出。
        try:
            import ctypes
            head = "水星配音对齐工作室 · 启动失败"
            body = (
                f"启动失败。详情已写入：\n{log_path}\n\n"
                f"最后 500 字：\n{detail[-500:] if detail else '（无）'}\n\n"
                "常见原因：\n"
                "  • 端口 8760~8779 全被占用（换个端口或关掉占用进程）\n"
                "  • WebView2 运行时缺失（下 Edge WebView2 Runtime 装上）\n"
                "  • 依赖缺失（重装或对照 启动错误.log 补包）\n"
            )
            # MB_OK=0 | MB_ICONERROR=0x10 | MB_TOPMOST=0x40000
            ctypes.windll.user32.MessageBoxW(0, body, head, 0x00040010)
        except Exception:  # noqa: BLE001
            try:
                input("按回车键退出…")
            except EOFError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
