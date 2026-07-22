"""模板中心：凡是需要用户填写的清单，都提供可下载模板 + 回传即用。

统一体验：账号分配 / 发布清单 / 回流数据 / 分镜脚本，均给带表头+示例的 CSV 模板。
纯逻辑，可单元测试。
"""

from __future__ import annotations

import csv
from pathlib import Path


TEMPLATES: dict[str, tuple[str, list[str], list[list[str]]]] = {
    "账号分配": (
        "账号分配模板.csv",
        ["account", "platform", "genre", "note"],
        [["账号A", "douyin", "悬疑惊悚", "示例，替换或删除"], ["账号B", "kuaishou", "都市情感", ""]],
    ),
    "发布清单": (
        "发布清单模板.csv",
        ["index", "video_file", "account", "title", "publish_at"],
        [["1", "成片001.mp4", "账号A", "标题示例", "2026-07-08 20:00"], ["2", "成片002.mp4", "账号B", "", ""]],
    ),
    "回流数据": (
        "回流数据模板.csv",
        ["video_file", "account", "play_count", "like_count", "finish_rate", "platform_url", "fail_reason"],
        [["成片001.mp4", "账号A", "12000", "800", "0.42", "https://...", ""], ["成片002.mp4", "账号B", "0", "0", "0", "", "疑似限流"]],
    ),
    "分镜脚本": (
        "分镜脚本模板.csv",
        ["index", "duration_seconds", "shot_desc", "line"],
        [["1", "3.5", "主角出场特写", "没想到她居然是卧底"], ["2", "2.0", "证据推近", "证据就在这里"]],
    ),
}


def template_names() -> list[str]:
    return list(TEMPLATES.keys())


def write_template(name: str, output_dir: Path) -> Path:
    """生成指定模板到目录，返回文件路径。"""
    if name not in TEMPLATES:
        raise KeyError(f"未知模板：{name}")
    filename, header, examples = TEMPLATES[name]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in examples:
            writer.writerow(row)
    return path


def validate_template(name: str, path: Path) -> list[str]:
    """校验回传：必需列是否齐全、是否有数据行。"""
    if name not in TEMPLATES:
        return [f"未知模板：{name}"]
    _, header, _ = TEMPLATES[name]
    path = Path(path)
    if not path.exists():
        return [f"文件不存在：{path}"]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:  # noqa: BLE001
        return [f"无法读取 CSV：{exc}"]
    if not rows:
        return ["清单为空，请至少填一行。"]
    present = {key.strip() for key in rows[0].keys() if key}
    missing = [col for col in header if col not in present]
    return [f"缺少列：{'、'.join(missing)}"] if missing else []
