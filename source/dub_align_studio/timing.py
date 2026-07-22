"""逐行计时：把「整篇 master 音频」量成「每行时长」。纯逻辑，可单元测试。

边界（构思锁定）：
    - 音频整条不切；这里量出的 duration 只是「这一句多长」的时长数字，
      用来约束对应画面的时长，不是「在哪剪音频」的切点——所以不做静音吸附。
    - 每行时长是变长的（whisper 量多少就是多少），业务约束下限 5 秒。
    - Aligner 产出的各行时长之和 == master 总时长（对时间轴做完整划分），
      这是 B 方案「整轨叠加」能逐帧收口的前提。

M1.5 提供 MockAligner（假尺子，直接给定每行时长），验证 B 渲染几何；
真实实现由 WhisperAligner（whisper-cli 字级时间戳 → 按脚本行量时长）替换，接口不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


# 每句脚本音频最低时长（秒）——业务约束：一句一画面，每镜 ≥5s，不会一闪而过。
LINE_DURATION_FLOOR = 5.0


@dataclass(frozen=True)
class LineTiming:
    """一行脚本对应的计时。index 从 1 起；duration 为该行音频真实时长（秒，变长）。"""

    index: int
    text: str
    duration: float


@runtime_checkable
class Aligner(Protocol):
    """尺子接口：给定整篇 master 音频与逐行脚本，量出每行时长。

    实现须保证 sum(t.duration) == master 总时长（对时间轴做完整划分）。
    """

    def measure(self, master_wav: Path, lines: list[str]) -> list[LineTiming]:
        ...


@dataclass
class MockAligner:
    """M1.5 假尺子：直接给定每行时长，用于验证 B 渲染几何（不依赖 whisper / fish-speech）。

    durations 与 lines 一一对应；真实链路里这些数字由 whisper 从 master 量出。
    """

    durations: list[float]

    def measure(self, master_wav: Path, lines: list[str]) -> list[LineTiming]:
        if len(self.durations) != len(lines):
            raise ValueError(
                f"MockAligner 时长数({len(self.durations)})与脚本行数({len(lines)})不一致。"
            )
        return [
            LineTiming(index=i, text=text, duration=float(duration))
            for i, (text, duration) in enumerate(zip(lines, self.durations), start=1)
        ]


def total_duration(timings: list[LineTiming]) -> float:
    """各行时长之和（应等于 master 总时长）。"""
    return round(sum(item.duration for item in timings), 3)


# ------------------------------------------------------------------ 配音计时表契约
TIMING_TABLE_NAME = "配音计时表.csv"
_TIMING_COLUMNS = ["index", "text", "start", "end", "duration"]


def write_timing_table(path: Path, timings: list[LineTiming]) -> Path:
    """落盘配音计时表（UTF-8-SIG）。start/end 由 duration 累加得出（音频不切，仅记录）。"""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cursor = 0.0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_TIMING_COLUMNS)
        for item in timings:
            start = cursor
            cursor = round(cursor + item.duration, 3)
            writer.writerow([item.index, item.text, f"{start:.3f}", f"{cursor:.3f}", f"{item.duration:.3f}"])
    return path


def read_timing_table(path: Path) -> list[LineTiming]:
    """读回配音计时表；只信 duration 列（start/end 是派生记录）。"""
    import csv

    path = Path(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    timings: list[LineTiming] = []
    for row_number, row in enumerate(rows, start=1):
        try:
            timings.append(
                LineTiming(
                    index=int(float(row.get("index") or row_number)),
                    text=str(row.get("text") or "").strip(),
                    duration=float(row["duration"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"计时表第 {row_number} 行无效：{row}") from exc
    if not timings:
        raise RuntimeError(f"计时表为空：{path}")
    return timings


def floor_violations(timings: list[LineTiming], floor: float = LINE_DURATION_FLOOR) -> list[int]:
    """返回时长低于业务下限的行号（正常应为空）。供校验/告警用，不在此处强改数据。"""
    return [item.index for item in timings if item.duration < floor - 1e-6]
