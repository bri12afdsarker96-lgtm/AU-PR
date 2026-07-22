"""工具箱组件商店：缺什么，点击即可下载/安装。

沿用水星轻量安装纪律：任何重资产都不进安装包，全部按需获取——
    - 下载类（download）：whisper-cli 运行时 + ggml 模型，复用水星
      model_registry（URL/size/SHA256）与 component_download（多源+断点续传+校验）；
    - 安装类（pip）：dots.tts / pyCapCut，调本机 pip 安装，输出逐行进日志；
    - 指引类（manual）：fish-speech（需按官方文档部署本地 server，无法一键）。

install_component 在后台任务里执行（web_server JOB），log 回调逐行汇报进度。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

from integrated_workbench.component_download import download_verified_file
from integrated_workbench.model_registry import (
    model_path,
    model_status,
    registry_entry,
    runtime_status,
    runtime_zip_path,
    whisper_root,
)

LogFn = Callable[[str], None]

# key → 展示与行为定义（顺序即工具箱展示顺序）
COMPONENTS: list[dict] = [
    {"key": "whisper_cli", "name": "whisper-cli 运行时", "kind": "download",
     "purpose": "计时尺子：逐行量出配音时长（约 8MB）"},
    {"key": "tiny", "name": "ggml-tiny 模型", "kind": "download",
     "purpose": "whisper 最小模型（约 75MB，速度最快）"},
    {"key": "base", "name": "ggml-base 模型", "kind": "download",
     "purpose": "whisper 均衡模型（约 142MB，推荐）"},
    {"key": "small", "name": "ggml-small 模型", "kind": "download",
     "purpose": "whisper 高精模型（约 466MB）"},
    {"key": "dots_tts", "name": "dots.tts 配音引擎", "kind": "pip", "package": "dots.tts",
     "purpose": "整篇声音克隆（2B/48kHz，需 NVIDIA GPU ≥6GB 显存）"},
    {"key": "pycapcut", "name": "pyCapCut 草稿组件", "kind": "pip", "package": "pycapcut",
     "purpose": "本机直接生成真实剪映草稿（缺失时交接包照常导出）"},
    {"key": "fish_speech", "name": "fish-speech 引擎", "kind": "manual",
     "purpose": "整篇声音克隆备选（本地 HTTP 服务）",
     "guide": "需按官方文档部署：git clone https://github.com/fishaudio/fish-speech，"
              "启动本地 server（默认 127.0.0.1:8080）后本软件自动识别。"},
]


def _pip_installed(package: str) -> bool:
    module = package.replace("-", "_").replace(".", "_")
    for name in (module, package):
        try:
            if importlib.util.find_spec(name) is not None:
                return True
        except (ImportError, ValueError):
            continue
    return False


def component_statuses() -> list[dict]:
    """工具箱清单：每项含 installed 与人类可读 detail（capability-check 口径）。"""
    result: list[dict] = []
    for item in COMPONENTS:
        entry = dict(item)
        if item["kind"] == "download":
            status = runtime_status() if item["key"] == "whisper_cli" else model_status(item["key"])
            entry["installed"] = status.usable
            entry["detail"] = status.label if status.usable else status.detail
        elif item["kind"] == "pip":
            installed = _pip_installed(str(item["package"]))
            entry["installed"] = installed
            entry["detail"] = "已安装。" if installed else f"未安装（点击安装：pip install {item['package']}）。"
        else:
            from .engines import FishLocalEngine

            probe = FishLocalEngine().probe()
            entry["installed"] = probe.available
            entry["detail"] = probe.detail
        result.append(entry)
    return result


def install_component(key: str, log: LogFn) -> None:
    """执行下载/安装；进度与结果逐行写入 log。失败抛异常（信息面向用户）。"""
    item = next((c for c in COMPONENTS if c["key"] == key), None)
    if item is None:
        raise KeyError(f"未知组件：{key}")
    if item["kind"] == "manual":
        raise RuntimeError(str(item.get("guide") or "该组件需手动部署。"))
    if item["kind"] == "pip":
        _pip_install(str(item["package"]), log)
        return
    if key == "whisper_cli":
        _download_whisper_runtime(log)
    else:
        _download_model(key, log)


def _progress(log: LogFn):
    milestones = {25, 50, 75, 100}

    def callback(done: int, total: int | None) -> None:
        if not total:
            return
        percent = int(done * 100 / total)
        hit = {m for m in milestones if percent >= m}
        if hit:
            milestones.difference_update(hit)
            log(f"  下载进度 {percent}%（{done // (1024 * 1024)}MB / {total // (1024 * 1024)}MB）")

    return callback


def _download_whisper_runtime(log: LogFn) -> None:
    entry = registry_entry("whisper_cli")
    target = runtime_zip_path()
    log(f"开始下载 {entry['filename']}（SHA256 校验，支持断点续传）…")
    result = download_verified_file(
        list(entry["urls"]), target, _progress(log),
        int(entry["size_bytes"]), str(entry["sha256"]),
    )
    log(f"  {result.message}")
    log("解压运行时…")
    with zipfile.ZipFile(target) as bundle:
        bundle.extractall(whisper_root())
    status = runtime_status()
    if not status.usable:
        raise RuntimeError(f"解压后仍未就绪：{status.detail}")
    log("✅ whisper-cli 运行时就绪。")


def _download_model(key: str, log: LogFn) -> None:
    entry = registry_entry(key)
    target = model_path(key)
    log(f"开始下载 {entry['filename']}（SHA256 校验，支持断点续传）…")
    result = download_verified_file(
        list(entry["urls"]), target, _progress(log),
        int(entry["size_bytes"]), str(entry["sha256"]),
    )
    log(f"  {result.message}")
    status = model_status(key)
    if not status.usable:
        raise RuntimeError(f"下载后校验未通过：{status.detail}")
    log(f"✅ 模型 {entry['name']} 就绪。")


def _pip_install(package: str, log: LogFn) -> None:
    log(f"pip install {package} …（使用当前 Python 环境）")
    process = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", package],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        line = line.rstrip()
        if line:
            log("  " + line[-160:])
    code = process.wait()
    if code != 0:
        raise RuntimeError(f"pip 安装失败（退出码 {code}）。请检查网络或手动执行：pip install {package}")
    if not _pip_installed(package):
        raise RuntimeError(f"安装完成但导入检测未通过，请重启软件后再试（pip install {package}）。")
    log(f"✅ {package} 安装完成。")
