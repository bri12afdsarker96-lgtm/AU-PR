from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from .proc import run_silent
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .product_identity import APP_ID_MODE, SOFTWARE_APP_ID


APP_NAME = "水星剪辑"
VERSION = "v2026.07.09.1"


@dataclass
class ReleaseCheck:
    group: str
    item: str
    level: str
    message: str
    evidence: str = ""
    fix: str = ""


@dataclass
class ReleaseVerificationReport:
    app_name: str
    version: str
    generated_at: str
    version_root: str
    overall_level: str
    counts: dict[str, int]
    checks: list[ReleaseCheck]


@dataclass
class ReleaseVerificationFiles:
    report: ReleaseVerificationReport
    markdown_path: Path
    json_path: Path
    csv_path: Path


LEVEL_LABELS = {"ok": "通过", "warn": "提醒", "error": "需处理"}

REQUIRED_SOURCE_MODULES = [
    "app.py",
    "models.py",
    "project.py",
    "assemble.py",
    "auto_cut.py",
    "editing_engine.py",
    "episode_pipeline.py",
    "scene_detect.py",
    "video_engine.py",
    "production_line.py",
    "production_state.py",
    "publish_feedback.py",
    "social_clipper.py",
    "title_engine.py",
    "visual_package.py",
    "publish_queue.py",
    "capability_check.py",
    "purity_audit.py",
    "release_verifier.py",
    "workflow_verifier.py",
]

UI_FORBIDDEN_TEXT = [
    "螃" + "蟹融合工作台",
    "螃" + "蟹剪辑",
    "螃" + "蟹短剧精选",
    "旧" + "剪辑工具",
    "旧" + "剪辑交接",
    "旧" + "剪辑软件",
    "遇" + "见发布",
    "p" + "angxie",
    "c" + "rab",
    "leg" + "acy" + "_software",
    "项目中心",
    "二创生成页",
    "审核中心",
    "插件页",
    "设置页",
    "GitHub 工具",
    "插件清单",
]


def default_version_root() -> Path:
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "source" / "integrated_workbench").exists() and (candidate / "docs").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def verify_release(version_root: str | Path | None = None, smoke: bool = False) -> ReleaseVerificationReport:
    root = Path(version_root) if version_root else default_version_root()
    checks: list[ReleaseCheck] = []

    payload = root / "payload"
    release_dir = root / "release" / f"{APP_NAME}_{VERSION}"
    main_exe = payload / f"{APP_NAME}.exe"
    installer = release_dir / f"{APP_NAME}_安装引导.exe"
    zip_path = root / "release" / f"{APP_NAME}_{VERSION}.zip"

    _check_file(checks, "发布产物", "绿色版主程序", main_exe, min_bytes=10_000_000)
    _check_file(checks, "发布产物", "安装引导", installer, min_bytes=5_000_000)
    _check_file(checks, "发布产物", "发布压缩包", zip_path, min_bytes=20_000_000)
    _check_file(checks, "发布产物", "发布清单", release_dir / "release_manifest.json", min_bytes=100)
    _check_file(checks, "发布产物", "主程序 SHA256", main_exe.with_suffix(main_exe.suffix + ".sha256"), min_bytes=64)

    _check_manifest(checks, release_dir / "release_manifest.json", root, main_exe, installer, release_dir)
    _check_brand_assets(checks, root, payload)
    _check_integrity_files(checks, main_exe, release_dir / "release_manifest.json")
    _check_payload_structure(checks, payload)
    _check_commercial_secrecy(checks, payload, release_dir)
    _check_no_git_dirs(checks, payload / "vendor_tools")
    _check_forbidden_text(checks, payload)
    if smoke:
        _check_exe_smoke(checks, main_exe)
    else:
        checks.append(
            ReleaseCheck(
                "启动烟测",
                "主程序启动",
                "warn",
                "本次未执行启动烟测。",
                str(main_exe),
                "需要完整验收时加 --smoke。",
            )
        )

    counts = {
        "ok": sum(1 for check in checks if check.level == "ok"),
        "warn": sum(1 for check in checks if check.level == "warn"),
        "error": sum(1 for check in checks if check.level == "error"),
        "total": len(checks),
    }
    overall = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
    return ReleaseVerificationReport(
        app_name=APP_NAME,
        version=VERSION,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        version_root=str(root),
        overall_level=overall,
        counts=counts,
        checks=checks,
    )


def write_release_verification_report(
    version_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    smoke: bool = False,
) -> ReleaseVerificationFiles:
    root = Path(version_root) if version_root else default_version_root()
    report = verify_release(root, smoke=smoke)
    target = Path(output_dir) if output_dir else root / "docs" / "release_verification"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = target / f"发布包验收_{stamp}.md"
    json_path = target / f"发布包验收_{stamp}.json"
    csv_path = target / f"发布包验收_{stamp}.csv"

    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "item", "level", "message", "evidence", "fix"])
        writer.writeheader()
        for check in report.checks:
            writer.writerow(asdict(check))

    latest = target / "发布包验收_latest.md"
    latest.write_text(_markdown(report), encoding="utf-8")
    return ReleaseVerificationFiles(report, markdown_path, json_path, csv_path)


def format_release_verification_report(report: ReleaseVerificationReport) -> str:
    label = LEVEL_LABELS.get(report.overall_level, report.overall_level)
    lines = [
        f"发布包验收：{report.app_name} {report.version}",
        f"总体状态：{label}",
        f"通过 {report.counts['ok']} 项，提醒 {report.counts['warn']} 项，需处理 {report.counts['error']} 项，共 {report.counts['total']} 项。",
    ]
    issues = [check for check in report.checks if check.level != "ok"]
    if issues:
        lines.append("优先处理：")
        for check in issues[:8]:
            lines.append(f"- {check.group}/{check.item}：{check.message} {check.fix}".strip())
    return "\n".join(lines)


def _check_file(checks: list[ReleaseCheck], group: str, item: str, path: Path, min_bytes: int = 1) -> None:
    if not path.exists():
        checks.append(ReleaseCheck(group, item, "error", "文件不存在。", str(path), "重新打包或检查复制规则。"))
        return
    size = path.stat().st_size
    level = "ok" if size >= min_bytes else "error"
    checks.append(
        ReleaseCheck(
            group,
            item,
            level,
            f"文件大小 {size} 字节。",
            str(path),
            "" if level == "ok" else f"文件小于预期阈值 {min_bytes} 字节，请重新打包。",
        )
    )


def _check_manifest(
    checks: list[ReleaseCheck],
    manifest_path: Path,
    root: Path,
    main_exe: Path,
    installer: Path,
    release_dir: Path,
) -> None:
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        checks.append(ReleaseCheck("发布清单", "JSON 格式", "error", str(exc), str(manifest_path), "重新生成发布清单。"))
        return
    expected = {
        "app_name": APP_NAME,
        "version": VERSION,
        "single_file_setup": True,
        "models_embedded": False,
        "components_embedded": False,
        "model_download_mode": "in_app_model_library",
        "app_id": SOFTWARE_APP_ID,
        "app_id_sha256": SOFTWARE_APP_ID,
        "app_id_mode": APP_ID_MODE,
    }
    for key, value in expected.items():
        actual = data.get(key)
        checks.append(
            ReleaseCheck(
                "发布清单",
                key,
                "ok" if actual == value else "error",
                f"{actual}",
                str(manifest_path),
                "" if actual == value else f"应为 {value}。",
            )
        )
    _check_manifest_path(checks, manifest_path, data, "version_root", root)
    _check_manifest_path(checks, manifest_path, data, "main_exe", main_exe)
    _check_manifest_path(checks, manifest_path, data, "installer_exe", installer)
    _check_manifest_path(checks, manifest_path, data, "release_dir", release_dir)


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _check_manifest_path(
    checks: list[ReleaseCheck],
    manifest_path: Path,
    data: dict,
    key: str,
    expected: Path,
) -> None:
    raw = str(data.get(key) or "").strip()
    if not raw:
        checks.append(
            ReleaseCheck(
                "发布清单路径",
                key,
                "error",
                "清单缺少路径字段。",
                str(manifest_path),
                "重新打包生成 release_manifest.json。",
            )
        )
        return

    actual = Path(raw)
    if not actual.exists():
        checks.append(
            ReleaseCheck(
                "发布清单路径",
                key,
                "error",
                f"清单路径不存在：{actual}",
                str(manifest_path),
                "迁移后需要重新打包或重新生成发布清单。",
            )
        )
        return

    matches = _same_resolved_path(actual, expected)
    checks.append(
        ReleaseCheck(
            "发布清单路径",
            key,
            "ok" if matches else "error",
            "清单路径指向当前版本产物。" if matches else f"清单路径指向 {actual}，当前应为 {expected}。",
            str(manifest_path),
            "" if matches else "迁移后需要重新打包或重新生成发布清单。",
        )
    )


def _check_brand_assets(checks: list[ReleaseCheck], root: Path, payload: Path) -> None:
    for base, label in [(root / "assets" / "brand", "版本目录品牌资产"), (payload / "assets" / "brand", "payload 品牌资产")]:
        for filename, min_bytes in [
            ("shuixing_icon.png", 10_000),
            ("shuixing_icon.ico", 10_000),
            ("shuixing_logo.png", 10_000),
            ("shuixing_banner.png", 10_000),
            ("technical_support_qr.png", 10_000),
            ("logo_design.md", 100),
        ]:
            _check_file(checks, label, filename, base / filename, min_bytes)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_integrity_files(checks: list[ReleaseCheck], main_exe: Path, manifest_path: Path) -> None:
    sidecar = main_exe.with_suffix(main_exe.suffix + ".sha256")
    if not main_exe.exists() or not sidecar.exists():
        return
    actual = _sha256_file(main_exe).lower()
    expected = sidecar.read_text(encoding="utf-8", errors="ignore").strip().lower().split()[0]
    checks.append(
        ReleaseCheck(
            "完整性保护",
            "EXE SHA256",
            "ok" if actual == expected else "error",
            "主程序 hash 与校验文件一致。" if actual == expected else "主程序 hash 与校验文件不一致。",
            str(sidecar),
            "" if actual == expected else "重新打包生成 exe 和 .sha256。",
        )
    )
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_hash = str(data.get("main_exe_sha256") or "").lower()
            manifest_app_id = str(data.get("app_id") or data.get("app_id_sha256") or "").lower()
        except Exception:
            manifest_hash = ""
            manifest_app_id = ""
        checks.append(
            ReleaseCheck(
                "完整性保护",
                "发布清单 EXE SHA256",
                "ok" if manifest_hash == actual else "error",
                "发布清单主程序 hash 与当前 EXE 一致。" if manifest_hash == actual else "发布清单主程序 hash 与当前 EXE 不一致。",
                str(manifest_path),
                "" if manifest_hash == actual else "检查 build_release.py 中 main_exe_sha256 生成逻辑。",
            )
        )
        checks.append(
            ReleaseCheck(
                "授权身份",
                "固定 APP ID",
                "ok" if manifest_app_id == SOFTWARE_APP_ID else "error",
                "APP ID 使用固定软件身份，不随版本和重新打包变化。" if manifest_app_id == SOFTWARE_APP_ID else "APP ID 未使用固定软件身份。",
                str(manifest_path),
                "" if manifest_app_id == SOFTWARE_APP_ID else "检查 product_identity.py 和 build_release.py 的 app_id 配置。",
            )
        )


def _check_payload_structure(checks: list[ReleaseCheck], payload: Path) -> None:
    for relative in [
        f"{APP_NAME}.exe",
        f"{APP_NAME}.exe.sha256",
        "ffmpeg/bin/ffmpeg.exe",
        "ffmpeg/bin/ffprobe.exe",
        "docs",
    ]:
        path = payload / relative
        exists = path.exists()
        checks.append(
            ReleaseCheck(
                "payload 结构",
                relative,
                "ok" if exists else "error",
                "存在。" if exists else "不存在。",
                str(path),
                "" if exists else "检查 build_release.py 的复制规则。",
            )
        )


def _check_commercial_secrecy(checks: list[ReleaseCheck], payload: Path, release_dir: Path) -> None:
    forbidden_paths = [
        payload / "source",
        payload / "build_scripts",
        payload / "vendor_tools",
        release_dir / "payload",
    ]
    for path in forbidden_paths:
        checks.append(
            ReleaseCheck(
                "商用防泄露",
                path.name,
                "ok" if not path.exists() else "error",
                "未随包携带。" if not path.exists() else "不应随商用包携带。",
                str(path),
                "" if not path.exists() else "从 build_release.py 的商用复制规则中移除。",
            )
        )
    model_bins = list((payload / "vendor_tools").rglob("ggml-*.bin")) if (payload / "vendor_tools").exists() else []
    checks.append(
        ReleaseCheck(
            "商用防泄露",
            "模型文件不内置",
            "ok" if not model_bins else "error",
            "未发现内置 ggml 模型。" if not model_bins else f"发现 {len(model_bins)} 个模型文件。",
            str(payload / "vendor_tools"),
            "" if not model_bins else "模型应由安装向导勾选下载，不打进 Setup。",
        )
    )


def _check_no_git_dirs(checks: list[ReleaseCheck], root: Path) -> None:
    if not root.exists():
        checks.append(ReleaseCheck("第三方组件", ".git 缓存", "ok", "轻量安装包未携带第三方组件源码。", str(root), ""))
        return
    matches = [item for item in root.rglob(".git") if item.is_dir()]
    checks.append(
        ReleaseCheck(
            "第三方组件",
            ".git 缓存",
            "ok" if not matches else "warn",
            "未发现 .git 目录。" if not matches else f"发现 {len(matches)} 个 .git 目录。",
            str(root),
            "" if not matches else "发布包应排除 .git 目录以减少体积和文件锁风险。",
        )
    )


def _check_forbidden_text(checks: list[ReleaseCheck], payload: Path) -> None:
    files = [
        payload / "README.md",
        payload / "使用说明.txt",
    ]
    findings: list[str] = []
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for forbidden in UI_FORBIDDEN_TEXT:
            if forbidden in text:
                findings.append(f"{path.name}:{forbidden}")
    checks.append(
        ReleaseCheck(
            "界面命名",
            "品牌纯净度",
            "ok" if not findings else "error",
            "未发现禁用命名。" if not findings else "；".join(findings[:20]),
            str(payload),
            "" if not findings else "把用户可见文案统一为水星剪辑和当前业务入口。",
        )
    )


def _check_exe_smoke(checks: list[ReleaseCheck], exe: Path) -> None:
    if not exe.exists():
        checks.append(ReleaseCheck("启动烟测", "主程序启动", "error", "主程序不存在。", str(exe), "重新打包。"))
        return
    process: subprocess.Popen[bytes] | None = None
    started_at = datetime.now()
    try:
        process = subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(8)
        running_count = _count_processes_by_path_or_name(exe, started_at)
        alive = process.poll() is None or running_count > 0
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        remaining = _cleanup_processes_by_path_or_name(exe, started_at)
        if remaining:
            time.sleep(1)
            remaining = _cleanup_processes_by_path_or_name(exe, started_at)
        checks.append(
            ReleaseCheck(
                "启动烟测",
                "主程序启动",
                "ok" if alive and remaining == 0 else "error",
                "主程序可启动，测试进程已清理。" if alive and remaining == 0 else f"启动状态={alive}，残留进程={remaining}。",
                str(exe),
                "" if alive and remaining == 0 else "检查打包依赖、启动日志或进程清理逻辑。",
            )
        )
    except Exception as exc:
        checks.append(ReleaseCheck("启动烟测", "主程序启动", "error", str(exc), str(exe), "检查是否被安全软件拦截或文件损坏。"))
        if process and process.poll() is None:
            process.kill()
        _cleanup_processes_by_path_or_name(exe, started_at)


def _powershell_literal(value: str) -> str:
    return value.replace("'", "''")


def _process_match_setup(exe: Path, started_at: datetime, variable: str = "procs") -> str:
    target = _powershell_literal(str(exe))
    name = _powershell_literal(exe.stem)
    started = _powershell_literal(started_at.isoformat(timespec="seconds"))
    return (
        f"$target='{target}'; "
        f"$name='{name}'; "
        f"$started=[datetime]::Parse('{started}'); "
        f"${variable}=@(Get-Process | Where-Object {{ "
        "($_.Path -eq $target) -or "
        "($_.ProcessName -eq $name -and $_.StartTime -ge $started) "
        "}); "
    )


def _count_processes_by_path_or_name(exe: Path, started_at: datetime) -> int:
    command = (
        _process_match_setup(exe, started_at)
        + "$count=($procs | Measure-Object).Count; "
        "Write-Output $count"
    )
    completed = run_silent(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, errors="replace")
    try:
        return int((completed.stdout or "0").strip().splitlines()[-1])
    except Exception:
        return 0


def _cleanup_processes_by_path_or_name(exe: Path, started_at: datetime) -> int:
    command = (
        _process_match_setup(exe, started_at)
        +
        "if($procs){ $procs | Stop-Process -Force }; "
        "Start-Sleep -Milliseconds 500; "
        + _process_match_setup(exe, started_at, "leftProcs")
        + "$left=($leftProcs | Measure-Object).Count; "
        "Write-Output $left"
    )
    completed = run_silent(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, errors="replace")
    try:
        return int((completed.stdout or "0").strip().splitlines()[-1])
    except Exception:
        return 0


def _markdown(report: ReleaseVerificationReport) -> str:
    lines = [
        f"# 发布包验收 - {report.app_name} {report.version}",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 版本目录：`{report.version_root}`",
        f"- 总体状态：**{LEVEL_LABELS.get(report.overall_level, report.overall_level)}**",
        f"- 通过：{report.counts['ok']}，提醒：{report.counts['warn']}，需处理：{report.counts['error']}，总数：{report.counts['total']}",
        "",
        "| 分组 | 项目 | 状态 | 说明 | 证据 | 修复建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(
            f"| {check.group} | {check.item} | {LEVEL_LABELS.get(check.level, check.level)} | {check.message} | `{check.evidence}` | {check.fix or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)
