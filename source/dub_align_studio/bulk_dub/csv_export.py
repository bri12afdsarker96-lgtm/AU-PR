"""结果 CSV 导出。流式写，稳定万级记录。"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Iterable

from .store import TaskRow

COLUMNS = [
    "Excel 行号", "输入视频", "文案摘要", "输出视频", "状态", "失败原因",
    "视频原始时长(s)", "TTS 时长(s)", "最终时长(s)",
    "音色名称", "voice ID", "语速", "是否保留原声", "尝试次数",
    "开始时间", "完成时间",
]


def _row_to_csv(row: TaskRow) -> list[str]:
    def fmt_time(ts: float) -> str:
        if ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    preview = row.text.strip().replace("\r\n", " ").replace("\n", " ")
    if len(preview) > 60:
        preview = preview[:56] + "…"
    return [
        str(row.excel_row),
        row.input_video,
        preview,
        row.output_path,
        row.status,
        row.error_detail,
        f"{row.video_duration:.3f}",
        f"{row.tts_duration:.3f}",
        f"{row.final_duration:.3f}",
        row.voice_name,
        row.voice_id,
        f"{row.speed:.2f}",
        "是" if row.keep_original_audio else "否",
        str(row.attempts),
        fmt_time(row.started_at),
        fmt_time(row.finished_at),
    ]


def write_csv(rows: Iterable[TaskRow], target: Path | str | io.IOBase) -> int:
    close = False
    if isinstance(target, (str, Path)):
        fh = open(target, "w", newline="", encoding="utf-8-sig")
        close = True
    else:
        fh = target
    try:
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        n = 0
        for r in rows:
            writer.writerow(_row_to_csv(r))
            n += 1
        return n
    finally:
        if close:
            fh.close()


def to_bytes(rows: Iterable[TaskRow]) -> bytes:
    buf = io.StringIO()
    write_csv(rows, buf)
    return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
