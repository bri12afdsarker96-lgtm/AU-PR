from __future__ import annotations

import json
import os
import shutil
from .proc import run_silent
import sys
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

from .component_download import ProgressCallback, download_verified_file
from .model_registry import (
    MODEL_REGISTRY,
    best_verified_model,
    model_path,
    registry_entry,
    runtime_zip_path,
    whisper_cli_path,
    whisper_models_dir,
    whisper_root,
)
from .models import DIR_A, DIR_AUTO_EDIT_INPUT, ProjectConfig
from .plugins import plugin_catalog
from .project import log_line


@dataclass
class PluginRunResult:
    plugin_key: str
    action: str
    ok: bool
    output_dir: str
    command: list[str]
    message: str


def _run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> PluginRunResult:
    completed = run_silent(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_dir = str(cwd or Path.cwd())
    if completed.returncode == 0:
        return PluginRunResult("", "", True, output_dir, command, completed.stdout[-2000:].strip())
    detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
    return PluginRunResult("", "", False, output_dir, command, detail.strip())


def _catalog_item(key: str):
    for item in plugin_catalog():
        if item.key == key:
            return item
    raise KeyError(key)


def _executable_for(key: str, path_name: str) -> str:
    plugin = _catalog_item(key)
    executable = plugin.executable_path()
    if executable and executable.exists():
        return str(executable)
    found = shutil.which(path_name)
    if found:
        return found
    return path_name


def _write_result(path: Path, result: PluginRunResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")


def install_whisper_runtime(
    model: str = "tiny",
    with_model: bool = True,
    project_root: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> PluginRunResult:
    """Install whisper.cpp Windows runtime and an optional ggml model."""
    plugin = _catalog_item("whisper_cpp")
    root = whisper_root()
    download_dir = root / "runtime_downloads"
    bin_dir = root / "build" / "bin" / "Release"
    model_dir = whisper_models_dir()
    for directory in [download_dir, bin_dir, model_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    runtime = registry_entry("whisper_cli", project_root)
    runtime_zip = runtime_zip_path()
    downloaded: list[str] = []
    try:
        runtime_result = download_verified_file(
            list(runtime["urls"]),
            runtime_zip,
            progress=progress,
            size_bytes=int(runtime["size_bytes"]),
            sha256=str(runtime["sha256"]),
            manual_target_dir=download_dir,
        )
        downloaded.append(str(runtime_result.target))
        extract_dir = download_dir / "whisper-bin-x64"
        if not (extract_dir / "whisper-cli.exe").exists():
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(runtime_zip) as archive:
                archive.extractall(extract_dir)
        copied = 0
        for path in extract_dir.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".exe", ".dll"}:
                shutil.copy2(path, bin_dir / path.name)
                copied += 1
        model_target = model_path(model) if model in MODEL_REGISTRY else model_dir / f"ggml-{model}.bin"
        if with_model:
            model_entry = registry_entry(model, project_root)
            model_result = download_verified_file(
                list(model_entry["urls"]),
                model_target,
                progress=progress,
                size_bytes=int(model_entry["size_bytes"]),
                sha256=str(model_entry["sha256"]),
                manual_target_dir=model_dir,
            )
            downloaded.append(str(model_result.target))
        exe = bin_dir / "whisper-cli.exe"
        ok = exe.exists() and (not with_model or model_target.exists())
        message = f"whisper.cpp 运行时已安装：{exe}，复制 {copied} 个运行文件。"
        if with_model:
            message += f" 模型：{model_target}"
        result = PluginRunResult(plugin.key, "install_whisper_runtime", ok, str(root), list(runtime["urls"]), message)
    except Exception as exc:
        result = PluginRunResult(plugin.key, "install_whisper_runtime", False, str(root), list(runtime["urls"]), str(exc))
    _write_result(root / "runtime_install_result.json", result)
    return result


def detect_scenes(config: ProjectConfig, video_path: str | Path, output_dir: str | Path) -> PluginRunResult:
    """Use PySceneDetect when available to generate scene cut metadata."""
    plugin = _catalog_item("pyscenedetect")
    video = Path(video_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    source = plugin.source_path()
    if source.exists():
        env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")

    command = [
        sys.executable,
        "-m",
        "scenedetect",
        "-i",
        str(video),
        "detect-content",
        "list-scenes",
        "-o",
        str(out),
    ]
    result = _run(command, cwd=out, env=env)
    result.plugin_key = plugin.key
    result.action = "detect_scenes"
    result.output_dir = str(out)
    if not result.ok and "No module named" in result.message:
        result.message = "PySceneDetect 源码已下载，但当前 Python 环境缺少运行依赖。请安装 click、numpy、opencv-python、platformdirs、tqdm 后重试。"
    _write_result(out / "pyscenedetect_result.json", result)
    log_line(config, f"场景检测：{video.name} -> {out}，ok={result.ok}")
    return result


def download_video(config: ProjectConfig, url: str, target: str = "原始视频") -> PluginRunResult:
    """Download an authorized source video with yt-dlp."""
    plugin = _catalog_item("yt_dlp")
    if target == "自动剪辑输入":
        out = config.root / DIR_AUTO_EDIT_INPUT
    else:
        out = config.root / DIR_A
    out.mkdir(parents=True, exist_ok=True)
    executable = _executable_for("yt_dlp", "yt-dlp")
    command = [
        executable,
        url,
        "-P",
        str(out),
        "-o",
        "%(title).80B_%(id)s.%(ext)s",
        "--no-playlist",
    ]
    result = _run(command, cwd=out)
    result.plugin_key = plugin.key
    result.action = "download_video"
    result.output_dir = str(out)
    if result.ok:
        result.message = result.message or f"下载完成：{out}"
    else:
        result.message = result.message or "下载失败，请确认链接可访问且你有使用授权。"
    _write_result(out / "yt_dlp_download_result.json", result)
    log_line(config, f"yt-dlp 下载：target={target} ok={result.ok}")
    return result


def build_silence_cut_command(input_video: str | Path, output_video: str | Path) -> PluginRunResult:
    """Prepare an Auto-Editor command. It only runs after auto-editor.exe or CLI is available."""
    plugin = _catalog_item("auto_editor")
    executable = shutil.which("auto-editor")
    if not executable:
        local_exe = plugin.source_path() / "auto-editor.exe"
        if local_exe.exists():
            executable = str(local_exe)

    command = [executable or "auto-editor", str(input_video), "-o", str(output_video)]
    ok = bool(executable)
    message = "Auto-Editor 可执行文件已找到。" if ok else "Auto-Editor 源码已下载，但还没有编译出 auto-editor.exe。"
    return PluginRunResult(plugin.key, "silence_cut", ok, str(Path(output_video).parent), command, message)


def build_transcribe_command(input_audio_or_video: str | Path, output_dir: str | Path, model_path: str | Path = "") -> PluginRunResult:
    """Prepare a whisper.cpp command for local transcription."""
    plugin = _catalog_item("whisper_cpp")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    executable = whisper_cli_path()
    model_text = str(model_path).strip() if model_path else ""
    model = Path(model_text) if model_text and model_text != "." else best_verified_model()
    command = [
        str(executable or "whisper-cli.exe"),
        "-m",
        str(model or plugin.source_path() / "models" / "ggml-small.bin"),
        "-f",
        str(input_audio_or_video),
        "-osrt",
        "-of",
        str(out / Path(input_audio_or_video).stem),
    ]
    ok = bool(executable and executable.exists() and model and model.exists())
    message = "whisper.cpp 可执行文件和模型已就绪。" if ok else "whisper.cpp 源码已下载；需要先编译 whisper-cli.exe 并下载模型。"
    return PluginRunResult(plugin.key, "transcribe", ok, str(out), command, message)


def build_subtitle_convert_command(input_subtitle: str | Path, output_subtitle: str | Path) -> PluginRunResult:
    """Prepare a Subtitle Edit command for subtitle conversion."""
    plugin = _catalog_item("subtitleedit")
    executable = plugin.executable_path()
    command = [str(executable or "SubtitleEdit.exe"), "/convert", str(input_subtitle), str(output_subtitle)]
    ok = bool(executable and executable.exists())
    message = "Subtitle Edit 可执行文件已就绪。" if ok else "Subtitle Edit 源码已下载；需要先编译 SubtitleEdit.exe。"
    return PluginRunResult(plugin.key, "subtitle_convert", ok, str(Path(output_subtitle).parent), command, message)
