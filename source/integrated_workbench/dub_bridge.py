"""配音桥：对接外部 VoxCPM2 配音软件，把"文本行 ↔ 逐句音频 ↔ 分镜片段"对齐成统一计划。

边界说明：配音由外部软件（主模型 openbmb/VoxCPM2）完成——它按上传文本逐行生成音频文件；
本软件不做 TTS，只负责：
  1) 把上传文本导出为配音脚本（一行一句），交给 VoxCPM2 软件生成逐句音频；
  2) 摄入其产出的逐句音频（配合 dub_sync 的配音清单约定）；
  3) 把每句「文本 + 音频时长 + 分镜片段」对齐，并挂上语义匹配的画面处理与音效；
  4) 逐句做音画同步——**音频长度定义画面时长**：配音是主轴，画面被裁剪/变速对齐到
     该句配音的长度（shot_duration = audio_duration），成片总时长 = 各句配音之和。
纯逻辑，可单元测试。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .edit_compose import SyncPlan, match_video_to_audio
from .semantic_match import match_sentence, parse_script


DUB_SCRIPT_NAME = "配音脚本.csv"
DUB_MANIFEST_TEMPLATE_NAME = "配音清单模板.csv"
DUB_MANIFEST_COLUMNS = ["index", "text", "audio_file", "duration_seconds"]


def write_dub_manifest_template(output_dir: Path, from_text: str = "") -> Path:
    """生成可下载的配音清单模板：表头 + 示例行 + 同目录填写说明。

    用户下载后填入 text/audio_file（时长可留空由软件实测），保存为
    “配音清单.csv”放进配音包目录即可被识别（dub_sync 也认 配音清单*.csv）。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / DUB_MANIFEST_TEMPLATE_NAME

    lines = parse_script(from_text)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(DUB_MANIFEST_COLUMNS)
        if lines:
            for i, line in enumerate(lines, start=1):
                writer.writerow([i, line, f"{i:03d}.wav", ""])
        else:
            writer.writerow([1, "这里填第一句台词（示例，替换或删除）", "001.wav", ""])
            writer.writerow([2, "这里填第二句台词", "002.wav", ""])

    note = output_dir / "配音清单填写说明.txt"
    note.write_text(
        "\n".join(
            [
                "配音清单模板填写说明",
                "",
                "1. 一行一句：每行对应一句配音、一个音频文件、一段分镜画面。",
                "2. 列含义：",
                "   - index：行号/序号（1、2、3…，用于文本、音频、分镜三者对齐）。",
                "   - text：该句台词文本。",
                "   - audio_file：音频文件名（如 001.wav），与本清单放在同一配音包目录。",
                "   - duration_seconds：音频时长（秒，可留空，软件会自动实测）。",
                "3. 填好后把本文件另存为 “配音清单.csv”，与音频一起放进配音包目录，",
                "   在软件“配音对齐成片”里选择该目录即可。",
                "4. 音频由外部配音软件（VoxCPM2）按本清单逐行生成。",
            ]
        ),
        encoding="utf-8",
    )
    return path


def validate_dub_manifest(path: Path) -> list[str]:
    """校验用户回传的配音清单，返回问题列表（空列表表示通过）。"""
    path = Path(path)
    issues: list[str] = []
    if not path.exists():
        return [f"文件不存在：{path}"]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception as exc:
        return [f"无法读取 CSV：{exc}"]
    if not rows:
        return ["清单为空，请至少填写一行。"]
    header = {key.strip() for key in rows[0].keys() if key}
    aliases = {"text", "文案", "台词", "字幕"}
    audio_aliases = {"audio_file", "音频文件", "wav", "file"}
    if not (header & aliases):
        issues.append("缺少台词列（text/文案/台词）。")
    if not (header & audio_aliases):
        issues.append("缺少音频文件列（audio_file/音频文件）。")
    empty_audio = sum(
        1 for r in rows if not any((r.get(k) or "").strip() for k in audio_aliases)
    )
    if empty_audio:
        issues.append(f"有 {empty_audio} 行未填音频文件名。")
    return issues


@dataclass(frozen=True)
class SentencePlan:
    index: int
    text: str
    emotion: str
    effect: str
    transition: str
    sound_effect: str
    audio_duration: float  # 该句配音时长（主轴）
    video_duration: float  # 原分镜画面时长
    sync: SyncPlan
    # 关键：画面最终时长 = 配音时长。画面被裁剪/变速对齐到配音，配音是主轴。
    shot_duration: float = 0.0


def write_dub_script(text: str, output_dir: Path) -> Path:
    """把上传文本导出为逐句配音脚本（index, text），交给外部 VoxCPM2 软件逐句生成音频。"""
    lines = parse_script(text)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / DUB_SCRIPT_NAME
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "text", "audio_file", "duration_seconds"])
        for i, line in enumerate(lines, start=1):
            # audio_file / duration 由 VoxCPM2 软件回填
            writer.writerow([i, line, f"{i:03d}.wav", ""])
    return path


def build_aligned_plan(
    text: str,
    audio_durations: list[float],
    video_durations: list[float],
    genre: str,
    audio_match_mode: str = "裁剪多余画面",
) -> tuple[list[SentencePlan], list[str]]:
    """按顺序把文本行 ↔ 逐句音频 ↔ 分镜片段对齐；每句挂语义匹配 + 音画同步。

    返回 (逐句计划, 提示信息)。三者长度不一致时按最短对齐，其余记入提示。
    """
    lines = parse_script(text)
    pair_count = min(len(lines), len(audio_durations), len(video_durations))
    notes: list[str] = []
    if len({len(lines), len(audio_durations), len(video_durations)}) > 1:
        notes.append(
            f"文本 {len(lines)} 句、音频 {len(audio_durations)} 个、分镜 {len(video_durations)} 段，"
            f"按 {pair_count} 组对齐，多余部分未配对。"
        )

    plan: list[SentencePlan] = []
    for i in range(pair_count):
        line = lines[i]
        audio_d = float(audio_durations[i])
        video_d = float(video_durations[i])
        match = match_sentence(i + 1, line, genre)
        sync = match_video_to_audio(video_d, audio_d, audio_match_mode)
        plan.append(
            SentencePlan(
                index=i + 1,
                text=line,
                emotion=match.emotion,
                effect=match.effect,
                transition=match.transition,
                sound_effect=match.sound_effect,
                audio_duration=round(audio_d, 3),
                video_duration=round(video_d, 3),
                sync=sync,
                shot_duration=round(audio_d, 3),  # 画面时长由配音时长定义
            )
        )
    return plan, notes


def plan_total_duration(plan: list[SentencePlan]) -> float:
    """成片总时长 = 各句配音时长之和（音画同步后画面对齐到配音）。"""
    return round(sum(item.audio_duration for item in plan), 3)


def build_arrangement(
    text: str,
    audio_durations: list[float],
    video_durations: list[float],
    genre: str,
    audio_match_mode: str = "裁剪多余画面",
):
    """一步到位：对齐 → 编排（每镜转场/特效/音效及时间位置） → 可渲染 spec。

    返回 (逐句计划, 编排方案 Arrangement, 渲染 spec)。这是"选完赛道自动剪辑"的编排落点。
    """
    from .edit_arrange import arrange_aligned, to_render_spec

    plan, notes = build_aligned_plan(text, audio_durations, video_durations, genre, audio_match_mode)
    arrangement = arrange_aligned(plan, genre)
    spec = to_render_spec(arrangement)
    return plan, arrangement, spec, notes
