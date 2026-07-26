"""编排胶水层：把「引擎 → 尺子 → 渲染 → 剪映导出」串成可被 GUI/CLI 调用的四步。

保持简单：一个输出目录就是一次成片工程，产物平铺其中：
    输出目录/
      master.wav + master.json     ① 生成配音
      配音计时表.csv                ② 量时长
      成片.mp4 + 成片.srt           ③ 渲染成片（分镜段在 成片_segments/）
      剪映草稿包_*/                 ④ 导出剪映草稿

纯编排不造轮子；每步都可独立调用（GUI 分步按钮）也可 run_all 一键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from integrated_workbench.edit_compose import aspect_canvas
from integrated_workbench.semantic_match import parse_script

from .aligners import WhisperAligner
from .audio_mix import AudioMix
from .capcut_draft import CapcutPackage, export_capcut_package
from .engines import (
    DotsLocalEngine,
    DotsRemoteEngine,
    DubEngine,
    FishLocalEngine,
    MasterAudio,
    MockEngine,
    SynthesisOptions,
)
from . import settings as studio_settings
from .engines.voice_ref import VoiceRef
from .overlays import OverlayText
from .progressbar import ProgressBar
from .render_b import DubBResult, RenderConfig, render_b
from .watermark import Watermark
from .subtitles import SubtitleStyle
from .timing import (
    LineTiming,
    MockAligner,
    TIMING_TABLE_NAME,
    read_timing_table,
    write_timing_table,
)


MASTER_NAME = "master.wav"
FILM_NAME = "成片.mp4"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}

ENGINE_KEYS = ["mock", "dots_local", "dots_remote", "fish_local"]
ALIGNER_KEYS = ["whisper", "均分兜底"]

# 五种常见画面比例（名称取自内核 edit_compose.ASPECT_RATIOS，画布由 aspect_canvas 计算）
ASPECT_KEYS = ["9:16 竖屏", "16:9 横屏", "1:1 方形", "4:3", "3:4"]
DEFAULT_ASPECT = ASPECT_KEYS[0]


def make_render_config(aspect: str | None = None) -> RenderConfig:
    """按画面比例名生成渲染配置（宽高来自内核 aspect_canvas，基准 1080）。"""
    name = aspect if aspect in ASPECT_KEYS else DEFAULT_ASPECT
    width, height = aspect_canvas(name, base=1080)
    return RenderConfig(width=width, height=height)


def segments_from_output(output_dir: Path, count: int) -> list[Path]:
    """从输出目录读回渲染产出的逐行分镜段（NNN.mp4）。

    供「④ 剪映导出」在软件重启后仍可工作：不依赖内存里的渲染结果，
    只要 ③ 的产物还在磁盘上就能导出。数量不足时报明确错误。"""
    seg_dir = Path(output_dir) / f"{Path(FILM_NAME).stem}_segments"
    if not seg_dir.is_dir():
        raise FileNotFoundError(f"找不到分镜段目录：{seg_dir}（请先执行「③ 渲染成片」）")
    segments = sorted(
        [p for p in seg_dir.iterdir() if p.suffix.lower() == ".mp4" and p.stem.isdigit()],
        key=lambda p: int(p.stem),
    )
    if len(segments) < count:
        raise FileNotFoundError(
            f"分镜段不完整：需要 {count} 段，{seg_dir} 里只有 {len(segments)} 段（请重新执行「③ 渲染成片」）")
    return segments[:count]


def select_shot_videos(shots_dir: Path, lines: list[str], material_mode: str,
                       seed: int, output_dir: Path, log=None) -> list[Path]:
    """按素材模式取每行的视频。flat=平铺旧口径；folder_order/keyword 走 material_select，
    选片结果落 选片清单.csv（重渲染复用同一份，删除该文件即重新选片）。"""
    from . import material_select as ms

    if material_mode in ("", "flat"):
        videos = list_shot_videos(shots_dir)
        if len(videos) < len(lines):
            raise ValueError(f"分镜视频不足：文案 {len(lines)} 行，目录里只有 {len(videos)} 个视频。")
        return videos[: len(lines)]
    reused = ms.read_selection(Path(output_dir), len(lines))
    if reused is not None:
        if log:
            log(f"  复用现有选片清单（{ms.SELECTION_CSV}）；想重新选片就删除该文件再跑。")
        return reused
    shots = ms.select_videos(Path(shots_dir), lines, material_mode, seed)
    ms.write_selection(Path(output_dir), shots)
    if log:
        for s in shots:
            extra = f"（{s.note}）" if s.note else ""
            log(f"  行{s.index} →「{s.folder}」→ {s.file.name}{extra}")
        log(f"  选片清单已写入输出目录（{ms.SELECTION_CSV}），重渲染将复用。")
    return [s.file for s in shots]


def make_engine(key: str) -> DubEngine:
    if key == "mock":
        return MockEngine()
    if key == "dots_local":
        return DotsLocalEngine()
    if key == "dots_remote":
        return DotsRemoteEngine()
    if key == "fish_local":
        return FishLocalEngine()
    raise KeyError(f"未知引擎：{key}（可选：{'、'.join(ENGINE_KEYS)}）")


def list_shot_videos(directory: Path) -> list[Path]:
    """分镜目录里按文件名自然序收视频（数字名优先按数值排）。

    排除本工具生成的 成片.mp4：输出目录默认就是分镜目录（① 需求），上一轮渲染的
    成片会留在目录里，若不剔除会被当成一个分镜——轻则虚增计数掩盖缺片，重则把整段
    旧成片选成某一行的画面。
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"分镜目录不存在：{directory}")
    videos = [p for p in directory.iterdir()
              if p.suffix.lower() in VIDEO_EXTENSIONS and p.name != FILM_NAME]

    def sort_key(path: Path):
        stem = path.stem
        return (0, int(stem)) if stem.isdigit() else (1, stem)

    return sorted(videos, key=sort_key)


def even_split_timings(lines: list[str], total_seconds: float) -> list[LineTiming]:
    """均分兜底：whisper 不可用时按行数均分 master 总长（末行吸收余数）。"""
    if not lines:
        raise ValueError("没有脚本行。")
    if total_seconds <= 0:
        raise ValueError(f"master 总时长必须为正：{total_seconds}")
    share = round(total_seconds / len(lines), 3)
    timings = [LineTiming(i, line, share) for i, line in enumerate(lines[:-1], start=1)]
    consumed = share * (len(lines) - 1)
    timings.append(LineTiming(len(lines), lines[-1], round(total_seconds - consumed, 3)))
    return timings


# ------------------------------------------------------------------ 四步
def step_dub(
    text: str,
    engine_key: str,
    output_dir: Path,
    voice: VoiceRef | None = None,
    options: SynthesisOptions | None = None,
    log=None,
    per_line: bool = True,
    progress=None,
    heartbeat=None,
) -> MasterAudio:
    """① 逐行克隆并拼接 master。per_line=True（默认）时一行一段，分镜时长按单行音频精确对齐。

    progress(done, total)：每完成一行回调，供 UI 进度条实时前进（配音是最耗时一步）。
    heartbeat(stage)：模型加载/每行开始等不动百分比的时刻刷新看门狗心跳，防冷启动误判卡死。
    mock 引擎按每行 5s 生成假音频（供无 GPU 环境走通全流程）。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(engine_key)
    from .engines.longform import synthesize_long

    # 逐行一段：一行=一段音频=一个分镜时长，精确对齐、避免截断/漂移；同一音色参考锚定音色。
    max_chars = int(getattr(engine, "max_chars", 1_000_000))
    master = synthesize_long(engine, text, voice, output_dir / MASTER_NAME, options, max_chars,
                             log=log, per_line=per_line, progress=progress, heartbeat=heartbeat)
    _archive_clone(master, voice)
    return master


def _timings_from_line_audio(master_wav: Path, lines: list[str]) -> list[LineTiming] | None:
    """逐行分段成片时，直接用每段音频的真实时长做逐行计时（精确，无需 whisper 估边界）。

    仅当分段清单与脚本行 1:1 对应时启用；否则返回 None，回退到 whisper/均分。
    """
    from .engines.longform import read_manifest

    manifest = read_manifest(Path(master_wav))
    if not manifest:
        return None
    chunks = sorted(manifest.get("chunks") or [], key=lambda c: int(c.get("index", 0)))
    if len(chunks) != len(lines) or not chunks:
        return None
    timings = []
    for i, (line, chunk) in enumerate(zip(lines, chunks), start=1):
        seconds = float(chunk.get("seconds") or 0.0)
        if seconds <= 0:
            return None  # 某段时长缺失/异常 → 不用精确逐行，回退
        timings.append(LineTiming(index=i, text=line, duration=round(seconds, 3)))
    return timings


def _archive_clone(master: MasterAudio, voice: VoiceRef | None) -> None:
    """克隆音频存档：master 副本进 总目录/克隆音频/（时间戳_引擎_音色.wav），可复用。"""
    import shutil
    from datetime import datetime

    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = voice.voice_id if voice else "默认声线"
        target = studio_settings.clones_dir() / f"{stamp}_{master.engine}_{label}{master.path.suffix}"
        shutil.copy2(master.path, target)
        meta = master.metadata_path()
        if meta.exists():
            shutil.copy2(meta, target.with_suffix(".json"))
    except Exception:
        pass  # 存档失败不阻塞成片流程


def step_timing(
    text: str,
    master_wav: Path,
    aligner_key: str,
    output_dir: Path,
) -> tuple[list[LineTiming], list[str]]:
    """② 逐行量时长并落计时表。返回 (计时, 提示列表)。"""
    lines = parse_script(text)
    if not lines:
        raise ValueError("整篇文案为空。")
    notes: list[str] = []
    exact = _timings_from_line_audio(master_wav, lines)
    if exact is not None:
        # 逐行分段成片：每行时长=该行克隆音频真实时长，最精确，优先于任何估算尺子
        write_timing_table(Path(output_dir) / TIMING_TABLE_NAME, exact)
        notes.append("逐行音频精确对齐：每个分镜时长按该行克隆音频实际时长。")
        return exact, notes
    if aligner_key == "whisper":
        timings = WhisperAligner().measure(master_wav, lines)
    elif aligner_key == "均分兜底":
        from .engines.base import wav_seconds

        timings = even_split_timings(lines, wav_seconds(Path(master_wav)))
        notes.append("使用均分兜底（未用 whisper 实测，行边界为估算）。")
    else:
        raise KeyError(f"未知尺子：{aligner_key}（可选：{'、'.join(ALIGNER_KEYS)}）")

    write_timing_table(Path(output_dir) / TIMING_TABLE_NAME, timings)
    return timings, notes


def step_render(
    master_wav: Path,
    timings: list[LineTiming],
    videos: list[Path],
    output_dir: Path,
    subtitle_style: SubtitleStyle | None = None,
    config: RenderConfig | None = None,
    overlays: list[OverlayText] | None = None,
    audio_mix: "AudioMix | None" = None,
    progress=None,
    progress_bar: "ProgressBar | None" = None,
    watermark: "Watermark | None" = None,
) -> DubBResult:
    """③ B 渲染成片（逐行裁/变速 + 整轨叠加 + 帧收口；可选烧字幕 + 文本框 + 进度条 + 动态水印 + BGM/音效混流）。"""
    return render_b(
        Path(master_wav), timings, [Path(v) for v in videos],
        Path(output_dir) / FILM_NAME, config, subtitle_style, overlays, audio_mix, progress,
        progress_bar=progress_bar, watermark=watermark,
    )


def step_capcut(
    timings: list[LineTiming],
    result: DubBResult | None,
    master_wav: Path,
    output_dir: Path,
    style: SubtitleStyle | None = None,
    canvas: tuple[int, int] = (1080, 1920),
) -> CapcutPackage:
    """④ 导出剪映草稿交接包（素材=渲染产出的逐行分镜段，与成片同一时间线）。

    result 为 None 时（软件重启后）从输出目录磁盘读回分镜段，照常导出。"""
    if result is not None:
        segments = [shot.segment_file for shot in result.shots if shot.segment_file]
    else:
        segments = segments_from_output(output_dir, len(timings))
    return export_capcut_package(timings, segments, Path(master_wav), Path(output_dir), style,
                                 canvas=canvas)


@dataclass
class StudioRun:
    master: MasterAudio
    timings: list[LineTiming]
    result: DubBResult
    capcut: CapcutPackage | None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result.ok


def run_all(
    text: str,
    engine_key: str,
    aligner_key: str,
    shots_dir: Path,
    output_dir: Path,
    voice: VoiceRef | None = None,
    options: SynthesisOptions | None = None,
    subtitle_style: SubtitleStyle | None = None,
    export_capcut: bool = False,
    config: RenderConfig | None = None,
    overlays: list[OverlayText] | None = None,
    audio_mix: AudioMix | None = None,
    progress_bar: "ProgressBar | None" = None,
    watermark: "Watermark | None" = None,
) -> StudioRun:
    """一键全流程：①配音 → ②量时长 → ③渲染 →（可选）④剪映导出。"""
    lines = parse_script(text)
    videos = list_shot_videos(shots_dir)
    if len(videos) < len(lines):
        raise ValueError(f"分镜视频不足：文案 {len(lines)} 行，目录里只有 {len(videos)} 个视频。")
    videos = videos[: len(lines)]

    config = config or make_render_config(DEFAULT_ASPECT)
    master = step_dub(text, engine_key, output_dir, voice, options)
    timings, notes = step_timing(text, master.path, aligner_key, output_dir)
    result = step_render(master.path, timings, videos, output_dir, subtitle_style, config,
                         overlays, audio_mix, progress_bar=progress_bar, watermark=watermark)
    capcut = (step_capcut(timings, result, master.path, output_dir, subtitle_style,
                          canvas=(config.width, config.height))
              if export_capcut else None)
    return StudioRun(master=master, timings=timings, result=result, capcut=capcut, notes=notes)


def load_timings(output_dir: Path) -> list[LineTiming]:
    """读回已有计时表（GUI 分步操作时跨步恢复）。"""
    return read_timing_table(Path(output_dir) / TIMING_TABLE_NAME)


def _dir_size(path: Path) -> int:
    total = 0
    for p in Path(path).rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def cleanup_intermediates(output_dir: Path) -> tuple[list[str], int]:
    """删除成片生成过程中的可再生中间产物，保留成片与各交接包。返回 (已删名单, 释放字节)。

    删除：
      · master_chunks/   逐行段音频 + 分段清单（仅「重配此段」用）
      · 成片_segments/   逐行无声分镜段 + 字幕临时 txt + 拼接中间件（_concat.txt/_full_silent.mp4）
    保留：成片.mp4/.srt、master.wav/.json、配音计时表.csv、选片清单.csv、
         剪映草稿包_*/（自包含）、Premiere工程.xml + Premiere工程_素材/（自包含）。
    清理后「重配此段/重新导出草稿或工程」不可用（需要中间产物），故应在确认成片与交接包无误后再清理。
    """
    import shutil
    from .engines.longform import chunks_dir_for

    output_dir = Path(output_dir)
    targets = [
        chunks_dir_for(output_dir / MASTER_NAME),          # master_chunks/
        output_dir / f"{Path(FILM_NAME).stem}_segments",   # 成片_segments/
    ]
    deleted: list[str] = []
    freed = 0
    for target in targets:
        if target.is_dir():
            freed += _dir_size(target)
            shutil.rmtree(target, ignore_errors=True)
            deleted.append(target.name)
    return deleted, freed
