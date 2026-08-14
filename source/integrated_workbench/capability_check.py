from __future__ import annotations

import csv
import json
import shutil
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .plugins import plugin_statuses, version_root
from .scene_detect import scenedetect_available
from .transcription import whisper_available


@dataclass
class CapabilityCheck:
    group: str
    name: str
    level: str
    status: str
    fix: str
    evidence: str = ""


@dataclass
class CapabilityReport:
    generated_at: str
    version_root: str
    overall_level: str
    counts: dict[str, int]
    checks: list[CapabilityCheck]


@dataclass
class CapabilityReportFiles:
    report: CapabilityReport
    markdown_path: Path
    json_path: Path
    csv_path: Path


LEVEL_LABELS = {"ok": "可用", "warn": "待启用", "error": "需处理"}


def _run_version(command: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        completed = run_silent(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except Exception as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    if output and output[0].startswith("Traceback") and len(output) > 1:
        detail = output[-1]
    else:
        detail = output[0] if output else f"退出码 {completed.returncode}"
    return completed.returncode == 0, detail


def _add_executable_check(checks: list[CapabilityCheck], name: str, path: Path, command_args: list[str], fix: str) -> None:
    if not path.exists():
        checks.append(CapabilityCheck("底层引擎", name, "error", "文件不存在", fix, str(path)))
        return
    ok, detail = _run_version([str(path), *command_args])
    checks.append(
        CapabilityCheck(
            "底层引擎",
            name,
            "ok" if ok else "warn",
            detail if ok else "已找到文件，但运行自检未通过",
            "" if ok else fix,
            str(path),
        )
    )


def collect_capability_report() -> CapabilityReport:
    root = version_root()
    checks: list[CapabilityCheck] = []

    ffmpeg_dir = root / "assets" / "ffmpeg" / "bin"
    _add_executable_check(checks, "FFmpeg", ffmpeg_dir / "ffmpeg.exe", ["-version"], "确认 ffmpeg.exe 位于 assets/ffmpeg/bin，或在工具箱为项目指定可用路径。")
    _add_executable_check(checks, "FFprobe", ffmpeg_dir / "ffprobe.exe", ["-version"], "确认 ffprobe.exe 位于 assets/ffmpeg/bin，或在工具箱为项目指定可用路径。")

    yt_dlp = root / "tools_bin" / "yt-dlp.exe"
    if not yt_dlp.exists():
        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        yt_dlp = Path(found) if found else yt_dlp
    _add_executable_check(checks, "授权素材下载组件", yt_dlp, ["--version"], "把 yt-dlp.exe 放入 tools_bin，或安装到系统 PATH。")

    scene_ok, scene_detail = scenedetect_available()
    checks.append(
        CapabilityCheck(
            "运行依赖",
            "镜头切点检测库",
            "ok" if scene_ok else "warn",
            f"PySceneDetect 可用：{scene_detail}" if scene_ok else f"PySceneDetect 待启用：{scene_detail}",
            "" if scene_ok else "开发环境执行 pip install scenedetect[opencv]；未安装时软件自动使用 FFmpeg 兜底。",
            "scenedetect",
        )
    )

    for item in plugin_statuses():
        if item.get("key") == "whisper_cpp":
            ok, detail = whisper_available()
            checks.append(
                CapabilityCheck(
                    "能力组件",
                    item["name"],
                    "ok" if ok else "warn",
                    f"真实语音转写可用：{detail}" if ok else f"真实语音转写待启用：{detail}",
                    "" if ok else "在工具箱模型库下载 whisper-cli 和 tiny/base/small 模型；模型需 sha256 校验通过。",
                    item.get("entry_path") or item.get("source_path") or item.get("github", ""),
                )
            )
            continue
        ready = bool(item.get("entry_ready") or item.get("python_import_ready") or item.get("path_ready"))
        downloaded = bool(item.get("downloaded"))
        if ready:
            level = "ok"
            status = "可直接调用"
            fix = ""
        elif downloaded:
            level = "warn"
            status = "源码或组件已纳入，仍需编译、安装依赖或保持为参考能力"
            fix = item.get("install_note", "")
        else:
            level = "warn"
            status = "未下载到本地组件目录"
            fix = "需要该能力时再下载或手工放入 vendor_tools；核心流程不强制依赖。"
        evidence = item.get("entry_path") or item.get("source_path") or item.get("github", "")
        checks.append(CapabilityCheck("能力组件", item["name"], level, status, fix, evidence))

    counts = {
        "ok": sum(1 for check in checks if check.level == "ok"),
        "warn": sum(1 for check in checks if check.level == "warn"),
        "error": sum(1 for check in checks if check.level == "error"),
        "total": len(checks),
    }
    overall = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
    return CapabilityReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        version_root=str(root),
        overall_level=overall,
        counts=counts,
        checks=checks,
    )


def format_capability_report(report: CapabilityReport) -> str:
    label = LEVEL_LABELS.get(report.overall_level, report.overall_level)
    lines = [
        f"能力组件自检：{label}",
        f"可用 {report.counts['ok']} 项，待启用 {report.counts['warn']} 项，需处理 {report.counts['error']} 项，共 {report.counts['total']} 项。",
    ]
    issues = [check for check in report.checks if check.level != "ok"]
    if issues:
        lines.append("优先处理：")
        for check in issues[:6]:
            lines.append(f"- {check.name}：{check.status}。{check.fix}")
    return "\n".join(lines)


def write_capability_report(output_dir: Path | None = None) -> CapabilityReportFiles:
    report = collect_capability_report()
    root = output_dir or (version_root() / "docs")
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = root / f"能力组件自检_{stamp}.md"
    json_path = root / f"能力组件自检_{stamp}.json"
    csv_path = root / f"能力组件自检_{stamp}.csv"

    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "name", "level", "status", "fix", "evidence"])
        writer.writeheader()
        for check in report.checks:
            writer.writerow(asdict(check))

    latest = root / "能力组件自检_latest.md"
    latest.write_text(_markdown(report), encoding="utf-8")
    return CapabilityReportFiles(report, markdown_path, json_path, csv_path)


def _markdown(report: CapabilityReport) -> str:
    lines = [
        "# 能力组件自检",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 版本目录：`{report.version_root}`",
        f"- 总体状态：**{LEVEL_LABELS.get(report.overall_level, report.overall_level)}**",
        f"- 可用：{report.counts['ok']}，待启用：{report.counts['warn']}，需处理：{report.counts['error']}，总数：{report.counts['total']}",
        "",
        "| 分组 | 能力 | 状态 | 说明 | 修复建议 | 证据 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(
            f"| {check.group} | {check.name} | {LEVEL_LABELS.get(check.level, check.level)} | {check.status} | {check.fix or '-'} | `{check.evidence}` |"
        )
    lines.append("")
    return "\n".join(lines)
