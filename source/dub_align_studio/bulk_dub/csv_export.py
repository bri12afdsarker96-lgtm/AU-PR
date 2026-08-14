"""结果 CSV 导出。真正流式（生成器 + 分块），稳定万级/十万级记录。

R11-8 加列：warnings / error_type / encoder_used / hw_fallback_used / batch_id / task_id。
"""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .store import TaskRow

COLUMNS = [
    "Excel 行号", "输入视频", "文案摘要", "输出视频", "状态", "失败原因", "错误类型",
    "警告列表", "视频原始时长(s)", "TTS 时长(s)", "最终时长(s)",
    "音色名称", "voice ID", "语速", "是否保留原声", "尝试次数",
    "编码器", "硬件回退",
    "开始时间", "完成时间", "batch_id", "task_id",
]


def _row_to_csv(row: TaskRow) -> list[str]:
    def fmt_time(ts: float) -> str:
        if ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    preview = row.text.strip().replace("\r\n", " ").replace("\n", " ")
    if len(preview) > 60:
        preview = preview[:56] + "…"
    warnings_str = " | ".join(str(w) for w in (row.warnings or []))
    return [
        str(row.excel_row),
        row.input_video,
        preview,
        row.output_path,
        row.status,
        row.error_detail,
        row.error_type,
        warnings_str,
        f"{row.video_duration:.3f}",
        f"{row.tts_duration:.3f}",
        f"{row.final_duration:.3f}",
        row.voice_name,
        row.voice_id,
        f"{row.speed:.2f}",
        "是" if row.keep_original_audio else "否",
        str(row.attempts),
        row.encoder_used,
        "是" if row.hw_fallback_used else "否",
        fmt_time(row.started_at),
        fmt_time(row.finished_at),
        row.batch_id,
        row.task_id,
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


def iter_csv_chunks(rows: Iterable[TaskRow], *, chunk_rows: int = 200) -> Iterator[bytes]:
    """R11-8 真正流式：生成器分批产出 bytes。

    首块含 BOM + 表头；后续块只有行。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    yield "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
    buf.seek(0); buf.truncate(0)
    counter = 0
    for r in rows:
        writer.writerow(_row_to_csv(r))
        counter += 1
        if counter >= chunk_rows:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate(0)
            counter = 0
    if counter:
        yield buf.getvalue().encode("utf-8")


def to_bytes(rows: Iterable[TaskRow]) -> bytes:
    """兼容旧调用——**仅小样本**用；万级建议 iter_csv_chunks。"""
    buf = io.StringIO()
    write_csv(rows, buf)
    return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")
