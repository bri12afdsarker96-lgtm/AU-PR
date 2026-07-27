"""版本口径：基础版本手工维护；构建戳由 打包.bat 生成（_build_info.py，不入库）。

界面右上角 / 页脚 / 启动横幅 / /api/state 均展示 full_version()，
每次打包的产物都能一眼区分（基础版本 + 打包时间 + git 提交号）。
"""

from __future__ import annotations

APP_NAME = "水星配音对齐工作室"
APP_VERSION = "0.7.66"


def build_stamp() -> str:
    """打包时由 打包.bat 写入 _build_info.py；源码直跑时为空。"""
    try:
        from ._build_info import BUILD_STAMP  # type: ignore[import-not-found]

        return str(BUILD_STAMP)
    except Exception:
        return ""


def full_version() -> str:
    stamp = build_stamp()
    return f"v{APP_VERSION}" + (f" · {stamp}" if stamp else " · 源码运行")
