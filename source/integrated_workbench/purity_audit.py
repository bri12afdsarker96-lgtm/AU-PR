from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


APP_NAME = "水星剪辑"
VERSION = "v2026.07.09.1"


def user_facing_docs() -> tuple[str, ...]:
    """允许随安装包分发的 docs 白名单（默认拒绝：未列出的一律不入包）。

    仅包含客户端 UI 运行时会打开展示的文件；验收证据、机器代验、开发路线、
    加固说明、授权对接说明等内部资料一律排除，避免客户端层面泄密。
    """
    return (
        f"运营操作手册_{VERSION}.md",
        "github_tool_radar.md",
        "github_research_matrix.md",
        "github_live_repository_analysis.md",
        "能力组件自检_latest.md",
    )

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "vendor_tools",
}
AUDIT_OUTPUT_DIRS = {
    "purity_audit",
}
BINARY_EXTENSIONS = {
    ".7z",
    ".aac",
    ".avi",
    ".dll",
    ".exe",
    ".ico",
    ".jpg",
    ".jpeg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pyd",
    ".png",
    ".wav",
    ".webm",
    ".zip",
}


@dataclass
class AuditCheck:
    group: str
    item: str
    level: str
    message: str
    evidence: str = ""
    fix: str = ""


@dataclass
class PurityAuditReport:
    app_name: str
    version: str
    generated_at: str
    version_root: str
    overall_level: str
    counts: dict[str, int]
    checks: list[AuditCheck]


@dataclass
class PurityAuditFiles:
    report: PurityAuditReport
    markdown_path: Path
    json_path: Path
    csv_path: Path


LEVEL_LABELS = {"ok": "通过", "warn": "提醒", "error": "需处理"}


def default_version_root() -> Path:
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "source" / "integrated_workbench").exists() and (candidate / "docs").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def forbidden_terms() -> list[str]:
    return [
        "p" + "angxie",
        "P" + "angxie",
        "c" + "rab",
        "C" + "rab",
        "leg" + "acy" + "_software",
        "螃" + "蟹剪辑",
        "螃" + "蟹融合",
        "螃" + "蟹短剧",
        "遇" + "见发布",
        "旧" + "剪辑",
        "旧" + "入口",
    ]


def run_purity_audit(version_root: str | Path | None = None) -> PurityAuditReport:
    root = Path(version_root) if version_root else default_version_root()
    checks: list[AuditCheck] = []

    _check_brand_names(checks, root)
    _check_no_compat_dirs(checks, root)
    _check_text_purity(checks, root)
    _check_release_artifacts(checks, root)
    _check_latest_verification(checks, root)
    _check_payload_docs_allowlist(checks, root)

    counts = {
        "ok": sum(1 for check in checks if check.level == "ok"),
        "warn": sum(1 for check in checks if check.level == "warn"),
        "error": sum(1 for check in checks if check.level == "error"),
        "total": len(checks),
    }
    overall = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
    return PurityAuditReport(
        app_name=APP_NAME,
        version=VERSION,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        version_root=str(root),
        overall_level=overall,
        counts=counts,
        checks=checks,
    )


def write_purity_audit_report(
    version_root: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> PurityAuditFiles:
    root = Path(version_root) if version_root else default_version_root()
    report = run_purity_audit(root)
    target = Path(output_dir) if output_dir else root / "docs" / "purity_audit"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = target / f"水星纯净版终审_{stamp}.md"
    json_path = target / f"水星纯净版终审_{stamp}.json"
    csv_path = target / f"水星纯净版终审_{stamp}.csv"

    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "item", "level", "message", "evidence", "fix"])
        writer.writeheader()
        for check in report.checks:
            writer.writerow(asdict(check))
    (target / "水星纯净版终审_latest.md").write_text(_markdown(report), encoding="utf-8")
    return PurityAuditFiles(report, markdown_path, json_path, csv_path)


def format_purity_audit(report: PurityAuditReport) -> str:
    label = LEVEL_LABELS.get(report.overall_level, report.overall_level)
    lines = [
        f"水星纯净版终审：{report.app_name} {report.version}",
        f"总体状态：{label}",
        f"通过 {report.counts['ok']} 项，提醒 {report.counts['warn']} 项，需处理 {report.counts['error']} 项，共 {report.counts['total']} 项。",
    ]
    issues = [check for check in report.checks if check.level != "ok"]
    if issues:
        lines.append("优先处理：")
        for check in issues[:8]:
            lines.append(f"- {check.group}/{check.item}：{check.message} {check.fix}".strip())
    return "\n".join(lines)


def _check_brand_names(checks: list[AuditCheck], root: Path) -> None:
    manifest = root / "release" / f"{APP_NAME}_{VERSION}" / "release_manifest.json"
    if not manifest.exists():
        checks.append(AuditCheck("品牌与版本", "发布清单", "error", "发布清单不存在。", str(manifest), "重新打包。"))
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        checks.append(AuditCheck("品牌与版本", "发布清单", "error", str(exc), str(manifest), "重新生成发布清单。"))
        return
    checks.append(
        AuditCheck(
            "品牌与版本",
            "应用名称",
            "ok" if data.get("app_name") == APP_NAME else "error",
            str(data.get("app_name")),
            str(manifest),
            f"应为 {APP_NAME}。",
        )
    )
    checks.append(
        AuditCheck(
            "品牌与版本",
            "版本号",
            "ok" if data.get("version") == VERSION else "error",
            str(data.get("version")),
            str(manifest),
            f"应为 {VERSION}。",
        )
    )
    obfuscated = bool(data.get("client_obfuscated"))
    checks.append(
        AuditCheck(
            "商用加固",
            "客户端字节码混淆",
            "ok" if obfuscated else "warn",
            f"{data.get('obfuscation_tool') or '未启用'}（client_obfuscated={obfuscated}）",
            str(manifest),
            ""
            if obfuscated
            else "当前为 --optimize 2 字节码（已剥离 docstring/assert）。如需字节码混淆，"
            "注册 pyarmor 许可证后设 MERCURY_OBFUSCATE=1 重打包。",
        )
    )


def _check_payload_docs_allowlist(checks: list[AuditCheck], root: Path) -> None:
    """安装包 docs 目录只允许出现白名单文件，防止把内部资料泄给客户端。"""
    allowed = set(user_facing_docs())
    payload_docs_dirs = [
        root / "payload" / "docs",
        root / "release" / f"{APP_NAME}_{VERSION}" / "payload" / "docs",
    ]
    leaks: list[str] = []
    checked_any = False
    for docs_dir in payload_docs_dirs:
        if not docs_dir.exists():
            continue
        checked_any = True
        for path in docs_dir.rglob("*"):
            if path.is_file() and path.name not in allowed:
                leaks.append(str(path.relative_to(root)))
    if not checked_any:
        return
    checks.append(
        AuditCheck(
            "客户端泄密",
            "安装包文档白名单",
            "ok" if not leaks else "error",
            "仅含白名单文档。" if not leaks else f"发现 {len(leaks)} 个越界文档：" + "；".join(leaks[:8]),
            str(payload_docs_dirs[0]),
            "" if not leaks else "安装包 docs 只允许打入 user_facing_docs() 白名单文件，移除内部资料后重新打包。",
        )
    )


def _check_no_compat_dirs(checks: list[AuditCheck], root: Path) -> None:
    relative_paths = [
        "leg" + "acy" + "_software",
        f"payload/{'leg' + 'acy' + '_software'}",
        f"release/{APP_NAME}_{VERSION}/payload/{'leg' + 'acy' + '_software'}",
    ]
    findings = [str(root / relative) for relative in relative_paths if (root / relative).exists()]
    checks.append(
        AuditCheck(
            "目录纯净度",
            "旧兼容目录",
            "ok" if not findings else "error",
            "未发现旧兼容目录。" if not findings else "；".join(findings),
            str(root),
            "" if not findings else "移除旧兼容软件副本并重新打包。",
        )
    )


def _check_text_purity(checks: list[AuditCheck], root: Path) -> None:
    own_roots = [
        root / "source",
        root / "build_scripts",
        root / "installer_source",
        root / "docs",
        root / "payload",
        root / "release" / f"{APP_NAME}_{VERSION}" / "payload",
    ]
    findings: list[str] = []
    terms = forbidden_terms()
    for base in own_roots:
        if not base.exists():
            continue
        for path in _iter_text_files(base):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for term in terms:
                if term in text:
                    findings.append(f"{path}:{term}")
                    break
            if len(findings) >= 40:
                break
    checks.append(
        AuditCheck(
            "字段纯净度",
            "自有源码与文档",
            "ok" if not findings else "error",
            "未发现禁用旧字段。" if not findings else "；".join(findings[:20]),
            str(root),
            "" if not findings else "清理命中的源码、文档或发布说明后重新打包。",
        )
    )


def _check_release_artifacts(checks: list[AuditCheck], root: Path) -> None:
    artifacts = [
        ("主程序", root / "payload" / f"{APP_NAME}.exe", 10_000_000),
        ("安装引导", root / "release" / f"{APP_NAME}_{VERSION}" / f"{APP_NAME}_安装引导.exe", 5_000_000),
        ("发布压缩包", root / "release" / f"{APP_NAME}_{VERSION}.zip", 100_000_000),
    ]
    for item, path, min_size in artifacts:
        exists = path.exists()
        size = path.stat().st_size if exists else 0
        checks.append(
            AuditCheck(
                "发布产物",
                item,
                "ok" if exists and size >= min_size else "error",
                f"文件大小 {size} 字节。" if exists else "文件不存在。",
                str(path),
                "" if exists and size >= min_size else "重新打包。",
            )
        )


def _check_latest_verification(checks: list[AuditCheck], root: Path) -> None:
    release_json = _latest(root / "docs" / "release_verification", "发布包验收_*.json")
    workflow_json = _latest(root / "docs" / "workflow_verification", "业务流程验收_*.json")
    _check_report_json(checks, "验收报告", "发布包验收", release_json, expect_warn_zero=True)
    _check_report_json(checks, "验收报告", "业务流程验收", workflow_json, expect_warn_zero=True)


def _check_report_json(
    checks: list[AuditCheck],
    group: str,
    item: str,
    path: Path | None,
    expect_warn_zero: bool,
) -> None:
    if not path:
        checks.append(AuditCheck(group, item, "error", "没有找到最新 JSON 报告。", "", "重新运行对应验收。"))
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        counts = data.get("counts", {})
    except Exception as exc:
        checks.append(AuditCheck(group, item, "error", str(exc), str(path), "重新生成报告。"))
        return
    errors = int(counts.get("error", 0))
    warns = int(counts.get("warn", 0))
    ok = errors == 0 and (warns == 0 if expect_warn_zero else True)
    checks.append(
        AuditCheck(
            group,
            item,
            "ok" if ok else "error",
            f"通过 {counts.get('ok', 0)} 项，提醒 {warns} 项，需处理 {errors} 项。",
            str(path),
            "" if ok else "处理提醒或错误后重新验收。",
        )
    )


def _iter_text_files(root: Path):
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if any(part in AUDIT_OUTPUT_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        yield path


def _latest(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    matches = sorted(root.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _markdown(report: PurityAuditReport) -> str:
    lines = [
        f"# 水星纯净版终审 - {report.app_name} {report.version}",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 版本目录：`{report.version_root}`",
        f"- 总体状态：**{LEVEL_LABELS.get(report.overall_level, report.overall_level)}**",
        f"- 统计：通过 {report.counts['ok']} 项，提醒 {report.counts['warn']} 项，需处理 {report.counts['error']} 项，共 {report.counts['total']} 项。",
        "",
        "## 检查明细",
        "",
        "| 分组 | 项目 | 状态 | 说明 | 证据 | 处理建议 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(
            f"| {check.group} | {check.item} | {LEVEL_LABELS.get(check.level, check.level)} | "
            f"{check.message} | `{check.evidence}` | {check.fix or '-'} |"
        )
    return "\n".join(lines) + "\n"
