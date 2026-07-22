"""Dub Align Studio 命令行。

    python -m dub_align_studio.cli verify [--workdir DIR] [--keep]
    python -m dub_align_studio.cli engines

verify：端到端自检（L2→L3→L5）——MockEngine 整篇合成连贯 master（纯 Python），
MockAligner 喂入变长逐行时长（≥5s），ffmpeg 合成样例分镜画面并跑真实 B 渲染，断言：
    · 成片总帧数 == 各行帧数之和（帧收口）；
    · |画面总长 − master 总长| ≤ 1 帧；
    · 成片含音频流（整轨 master 已叠加）。
engines：探测各配音引擎可用性（capability-check 风格；真引擎不可用时给出指引）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from .aligners import WhisperAligner
from .capcut_draft import export_capcut_package
from .engines import DotsLocalEngine, FishLocalEngine, MockEngine
from .render_b import RenderConfig, render_b, _run
from .subtitles import SubtitleStyle
from .timing import MockAligner, floor_violations, read_timing_table, total_duration, write_timing_table


# 样例：变长逐行时长（≥5s 业务下限）与对应画面源长度，覆盖三种画面对齐分支。
_SAMPLE_LINES = [
    "第一句：主角登场，画面素材偏长需要裁剪。",   # 6.0s 音频 / 10s 画面 → 裁剪
    "第二句：情绪推进，画面素材偏短需要放慢补足。",  # 7.5s 音频 / 4s 画面 → 放慢/克隆
    "第三句：收尾定格，画面与配音基本等长。",       # 5.0s 音频 / 5s 画面 → 基本原速
]
_SAMPLE_DURATIONS = [6.0, 7.5, 5.0]
_SAMPLE_SRC_LEN = [10.0, 4.0, 5.0]


def _gen_clip(config: RenderConfig, path: Path, seconds: float, index: int) -> None:
    # 带运动的测试图（stand-in for 分镜画面），尺寸各异以检验 scale+pad。
    size = ["1280x720", "720x1280", "640x640"][index % 3]
    _run(
        [config.ffmpeg, "-y", "-f", "lavfi", "-i",
         f"testsrc=size={size}:rate=30:duration={seconds:.3f}",
         "-pix_fmt", "yuv420p", str(path)],
        f"生成样例画面 {path.name}",
    )


def verify(workdir: Path | None = None, keep: bool = False) -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("[跳过] 未找到 ffmpeg/ffprobe，无法做真实 B 渲染自检。")
        return 2

    config = RenderConfig()
    tmp_created = workdir is None
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="dub_align_b_"))
    workdir.mkdir(parents=True, exist_ok=True)
    print(f"[工作目录] {workdir}")

    try:
        master = workdir / "master.wav"
        engine = MockEngine(durations=_SAMPLE_DURATIONS)
        master_audio = engine.synthesize_full("\n".join(_SAMPLE_LINES), None, master)
        print(f"[配音引擎] {master_audio.engine} → {master_audio.seconds:.3f}s，元数据 {master_audio.metadata_path().name}")
        videos = []
        for i, src_len in enumerate(_SAMPLE_SRC_LEN):
            clip = workdir / f"shot_{i + 1:03d}.mp4"
            _gen_clip(config, clip, src_len, i)
            videos.append(clip)

        aligner = MockAligner(durations=_SAMPLE_DURATIONS)
        timings = aligner.measure(master, _SAMPLE_LINES)
        table = write_timing_table(workdir / "配音计时表.csv", timings)
        print(f"[计时表] {table.name}")

        violations = floor_violations(timings)
        if violations:
            print(f"[警告] 以下行低于 {5.0}s 业务下限：{violations}")

        output = workdir / "成片.mp4"
        result = render_b(master, timings, videos, output, config, subtitle_style=SubtitleStyle())

        print("\n=== 逐行画面对齐 ===")
        for shot in result.shots:
            print(
                f"  {shot.index:>2} | 音频{shot.target_seconds:>5.2f}s "
                f"| 画面源{shot.src_seconds:>5.2f}s | {shot.frames:>4}帧 | {shot.strategy}"
            )
        print("\n=== 收口断言 ===")
        print(f"  预期总帧数        : {result.expected_frames}")
        print(f"  成片总帧数        : {result.total_frames}")
        print(f"  master 总长       : {result.master_seconds:.3f}s")
        print(f"  画面总长          : {result.video_seconds:.3f}s")
        print(f"  帧收口(≤1帧)      : {result.frame_locked}")
        print(f"  含音频流(整轨)    : {result.has_audio}")
        print(f"  逐行时长之和      : {total_duration(timings):.3f}s")
        print(f"  字幕              : {result.subtitle_note or '无'}"
              f"{'，SRT: ' + result.srt_path.name if result.srt_path else ''}")
        print(f"\n结果：{'✅ 通过' if result.ok else '❌ 未通过'} → {result.output_path}")
        return 0 if result.ok else 1
    finally:
        if tmp_created and not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def engines_status() -> int:
    """探测配音引擎与尺子可用性（capability-check 风格）。"""
    print("=== 配音引擎 ===")
    for status in (MockEngine().probe(), DotsLocalEngine().probe(), FishLocalEngine().probe()):
        mark = "✅" if status.available else "⛔"
        print(f"  {mark} {status.key:<12} {status.detail}")
    print("=== 计时尺子 ===")
    aligner = WhisperAligner().probe()
    mark = "✅" if aligner.available else "⛔"
    print(f"  {mark} {aligner.key:<12} {aligner.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dub_align_studio", description="配音对齐工作室 CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p_verify = sub.add_parser("verify", help="MockEngine + Mock 尺子 + 真实 B 渲染端到端自检")
    p_verify.add_argument("--workdir", type=Path, default=None, help="样例与产物目录（默认临时目录）")
    p_verify.add_argument("--keep", action="store_true", help="保留工作目录（默认自检后清理）")
    sub.add_parser("engines", help="探测配音引擎可用性（dots.tts 等）")
    p_web = sub.add_parser("web", help="启动浏览器版界面（推荐）")
    p_web.add_argument("--port", type=int, default=8760)
    p_web.add_argument("--no-browser", action="store_true", help="只起服务，不自动打开浏览器")
    p_capcut = sub.add_parser("capcut", help="按计时表导出剪映草稿交接包")
    p_capcut.add_argument("--timing-table", type=Path, required=True, help="配音计时表.csv")
    p_capcut.add_argument("--segments-dir", type=Path, required=True, help="逐行分镜段目录（按文件名序配对）")
    p_capcut.add_argument("--master", type=Path, required=True, help="整轨 master 音频")
    p_capcut.add_argument("--output-dir", type=Path, required=True, help="交接包输出目录")
    p_capcut.add_argument("--font-size", type=int, default=64, help="字幕像素字号（默认 64）")
    args = parser.parse_args(argv)

    if args.command == "verify":
        return verify(workdir=args.workdir, keep=args.keep)
    if args.command == "engines":
        return engines_status()
    if args.command == "web":
        from .web_server import main as web_main

        return web_main(port=args.port, open_browser=not args.no_browser)
    if args.command == "capcut":
        timings = read_timing_table(args.timing_table)
        # 只认 001.mp4 式分镜段命名，排除工作目录里的中间产物（如 _full_silent.mp4）。
        segments = sorted(
            [p for p in Path(args.segments_dir).iterdir()
             if p.suffix.lower() == ".mp4" and p.stem.isdigit()],
            key=lambda p: int(p.stem),
        )
        package = export_capcut_package(
            timings, segments, args.master, args.output_dir,
            style=SubtitleStyle(font_size_px=args.font_size),
        )
        print(package.message)
        print(f"交接包：{package.package_dir}")
        return 0
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
