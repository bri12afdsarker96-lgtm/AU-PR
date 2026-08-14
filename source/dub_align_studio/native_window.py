# -*- coding: utf-8 -*-
"""原生窗口模式：用 pywebview（内嵌 Edge WebView2）替代浏览器。

出发点：
    - 现架构本地 HTTP server + 前端 HTML；发布形态一直是弹浏览器
    - 用户希望「像原生软件」：无地址栏、无浏览器 chrome、任务栏图标
      走本 app 而非 Chrome/Edge

策略：
    1) launcher 优先调 :func:`run_with_native_window`
    2) 拿到 web_server 起好的 http://127.0.0.1:<port>/ 后，
       在**主线程**上 webview.create_window + webview.start()（阻塞）
    3) 用户关窗 -> 停 server -> 退出 exe
    4) pywebview 未安装 / WebView2 runtime 未装 / 任何异常
       -> 打印告警并 return None，由调用方降级为浏览器模式

依赖：
    pip install pywebview
    Windows 需 Edge WebView2 Runtime（Windows 11 自带，Win10 一般都有；
    没装的会自动降级为浏览器）。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Optional


def _find_icon() -> Optional[Path]:
    """在 exe 同目录 / 上层找 app.ico。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
        for cand in (base / "app.ico", base / "_internal" / "app.ico"):
            if cand.exists():
                return cand
    here = Path(__file__).resolve()
    for cand in (
        here.parents[2] / "installer" / "app.ico",
        here.parents[2] / "app.ico",
    ):
        if cand.exists():
            return cand
    return None


def native_mode_disabled() -> bool:
    """env 显式禁用（调试时走浏览器）：DUB_ALIGN_UI_MODE=browser"""
    return os.environ.get("DUB_ALIGN_UI_MODE", "").strip().lower() == "browser"


def run_with_native_window(port: int = 8760) -> Optional[int]:
    """尝试原生窗口模式。成功返回 exit code；失败/不可用返回 None。

    调用方（launcher）拿到 None 就降级 web_main() 走浏览器。
    """
    if native_mode_disabled():
        return None

    try:
        import webview  # type: ignore
    except ImportError:
        print("[UI] pywebview 未安装 → 降级为浏览器模式（pip install pywebview 可启用原生窗口）")
        return None

    try:
        # 延迟导入，避免不用原生模式时也拉进 web_server 副作用
        from .web_server import serve as _serve
    except Exception as exc:  # noqa: BLE001
        print(f"[UI] web_server 导入失败：{exc} → 降级浏览器")
        return None

    # 1) 起 server（open_browser=False，浏览器交给原生窗口）
    try:
        server = _serve(port, open_browser=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[UI] server 启动失败：{exc} → 降级浏览器")
        return None

    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"

    # 2) server 放后台线程 serve_forever
    t = threading.Thread(target=server.serve_forever, daemon=True, name="web-server")
    t.start()

    # 3) 主线程创建窗口 + 阻塞
    icon = _find_icon()
    try:
        webview.create_window(
            title="水星配音对齐工作室",
            url=url,
            width=1400,
            height=900,
            min_size=(1024, 720),
            resizable=True,
            confirm_close=False,
        )
        # icon 只在部分平台生效（Windows 用 exe 图标就够），传上无害
        start_kwargs = {}
        if icon is not None:
            start_kwargs["icon"] = str(icon)
        webview.start(**start_kwargs)
    except Exception as exc:  # noqa: BLE001
        # WebView2 未装 / .NET 缺失 / 其他 UI 层异常 -> 降级
        print(f"[UI] 原生窗口启动失败：{exc} → 降级浏览器")
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # 显式降级：让浏览器起来看到内容（用户已在等）
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        # server 继续在后台跑，等 serve_forever 结束
        try:
            t.join()
        except Exception:  # noqa: BLE001
            pass
        return 0

    # 4) 用户关窗 -> 停 server
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass
    return 0
