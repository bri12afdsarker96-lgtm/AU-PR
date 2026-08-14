"""音频混流：master 配音 + 可选 BGM（循环）+ 若干音效（定点）+ **原视频音效**，各自可调音量。

需求（用户 2026-07-22 第五轮 · 2026-08 补第六路：原视频音效可调音量并保留到成片）：
    - master 配音音量可调；
    - 选一首 BGM，音量可调，视频比 BGM 长时**循环**播放到片尾；
    - 在音轨任意位置放音效，位置即触发时刻，单个音效音量可调；
    - **原视频音效**（即拼接后的分镜视频自带音轨）音量可调；默认 100% 保留——旧版
      `_render_silent_segment` 一律 `-an` 剥掉，导致成片只剩配音，用户抱怨「视频原有
      的音效被剪辑掉」+「音量控件失效（怎么拖都拉不出原音）」。此路开通即修复；
    - 软件内「试听」要能听到 master+BGM(循环)+音效 的完整预览（前端 Web Audio 合成，
      与此处 ffmpeg 混流口径一致：同样的 volume/at/loop 语义）。

本模块只构造 ffmpeg 滤镜字符串（纯逻辑，可单测）；真实混流在 render_b 的叠加步骤执行。
amix 用 normalize=0，避免路数增多导致整体音量被压低——各路音量完全由用户滑杆决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _clamp_vol(value: float) -> float:
    """音量倍率钳到 0~4（0=静音，1=原音量，4=+12dB 上限，防止爆音过头）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(4.0, v))


@dataclass(frozen=True)
class BgmTrack:
    path: Path
    volume: float = 0.35   # BGM 默认压到 35%，人声为主
    loop: bool = True      # 视频比 BGM 长时循环到片尾


@dataclass(frozen=True)
class SfxCue:
    path: Path
    at_seconds: float = 0.0   # 触发时刻（音轨上的位置）
    volume: float = 1.0


@dataclass(frozen=True)
class AudioMix:
    master_volume: float = 1.0
    bgm: BgmTrack | None = None
    sfx: list[SfxCue] = field(default_factory=list)
    # 原视频音效（拼接后的分镜视频自带音轨）音量。0=静音（等同旧行为的 -an），
    # 1=原声原音量；默认 0.0 保持旧版行为，前端 UI 默认下发 1.0（保留原音）。
    orig_video_volume: float = 0.0

    def is_trivial(self) -> bool:
        """无 BGM、无音效、无原视频音效、master 原音量 → 走原来的直接映射路径（不进 filter_complex）。"""
        return (self.bgm is None and not self.sfx
                and abs(self.master_volume - 1.0) < 1e-3
                and self.orig_video_volume <= 1e-3)


def build_audio_inputs(mix: AudioMix) -> list[list[str]]:
    """额外音频输入（master 之后）：BGM 循环用 -stream_loop -1，音效各占一路。

    返回每个输入的参数片段列表，顺序即 ffmpeg 输入序号顺序：
    master 为输入 1，故 BGM 为输入 2，音效从 3 起。"""
    inputs: list[list[str]] = []
    if mix.bgm is not None:
        args = ["-i", str(mix.bgm.path)]
        if mix.bgm.loop:
            args = ["-stream_loop", "-1"] + args
        inputs.append(args)
    for cue in mix.sfx:
        inputs.append(["-i", str(cue.path)])
    return inputs


def build_audio_filtergraph(mix: AudioMix, master_input: int = 1,
                            video_input: int | None = 0) -> tuple[str, str]:
    """构造音频 filter_complex，返回 (filtergraph, out_label)。

    - master：input=master_input，音量 master_volume；
    - 原视频音效：input=video_input（默认 0，即拼接后的分镜视频），音量 orig_video_volume；
      要求该输入的音轨已存在（render_b 层保证每段视频都带音轨——无音则用 anullsrc 补静音）。
      orig_video_volume<=0 时**不**接入这一路，输入 0 的音轨会被 ffmpeg 自然忽略。
    - BGM：紧随 master 的输入号，音量 bgm.volume（已 -stream_loop 循环）；
    - 音效：依次其后，adelay 到 at_seconds、音量 sfx.volume；
    - amix duration=first → 输出长度对齐 master（BGM 循环被裁到片尾，音效/原音自动补静音）。
    统一 aformat=44100/stereo，避免 amix 因格式不一致失败。"""
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    chains: list[str] = []
    labels: list[str] = []

    chains.append(f"[{master_input}:a]volume={_clamp_vol(mix.master_volume):.3f},{fmt}[am]")
    labels.append("[am]")

    # 原视频音效：仅在音量 > 0 且给定 video_input 时接入
    orig_v = _clamp_vol(mix.orig_video_volume)
    if video_input is not None and orig_v > 1e-3:
        chains.append(f"[{video_input}:a]volume={orig_v:.3f},{fmt}[aov]")
        labels.append("[aov]")

    idx = master_input + 1
    if mix.bgm is not None:
        chains.append(f"[{idx}:a]volume={_clamp_vol(mix.bgm.volume):.3f},{fmt}[abg]")
        labels.append("[abg]")
        idx += 1
    for order, cue in enumerate(mix.sfx):
        delay_ms = max(0, int(round(float(cue.at_seconds) * 1000)))
        chains.append(
            f"[{idx}:a]adelay={delay_ms}|{delay_ms},volume={_clamp_vol(cue.volume):.3f},{fmt}[asfx{order}]"
        )
        labels.append(f"[asfx{order}]")
        idx += 1

    if len(labels) == 1:  # 仅 master（可能只调了总音量）
        return chains[0].replace("[am]", "[aout]"), "[aout]"
    mixed = "".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[aout]"
    return ";".join(chains) + ";" + mixed, "[aout]"


def mix_from_payload(payload: dict, resolve: "callable[[str], Path | None]",
                     warnings: list[str] | None = None) -> AudioMix:
    """从前端 JSON 还原 AudioMix。resolve(name)->Path 把资产文件名映射到磁盘路径
    （不存在的资产**跳过但记警告**，成片不因缺一个音效而失败；缺失文件名会被
    追加进 warnings 列表——上层负责往任务日志/UI 打黄色提示，避免"设置了但没声"
    的静默假成功）。warnings=None 时保持旧无回显行为（对现有调用方向后兼容）。"""
    if not isinstance(payload, dict):
        return AudioMix()
    master_volume = _clamp_vol(payload.get("master_volume", 1.0))
    # 前端未下发时按 0.0 兜底——保持旧脚本/旧成片重烧的行为完全不变；
    # 前端有滑杆时会稳定下发（默认值 100 → 1.0）。
    orig_video_volume = _clamp_vol(payload.get("orig_video_volume", 0.0))
    bgm = None
    bgm_raw = payload.get("bgm") or {}
    if isinstance(bgm_raw, dict) and bgm_raw.get("file"):
        name = str(bgm_raw["file"])
        path = resolve(name)
        if path is not None:
            bgm = BgmTrack(path=path, volume=_clamp_vol(bgm_raw.get("volume", 0.35)),
                           loop=bool(bgm_raw.get("loop", True)))
        elif warnings is not None:
            warnings.append(f"BGM 素材找不到：{name}（跳过，不影响其他音轨；请到"
                            f"「素材库」重新上传或检查文件名）")
    sfx: list[SfxCue] = []
    for row in payload.get("sfx") or []:
        if not isinstance(row, dict) or not row.get("file"):
            continue
        name = str(row["file"])
        path = resolve(name)
        if path is None:
            if warnings is not None:
                warnings.append(f"音效素材找不到：{name}（该音效跳过，其余照常）")
            continue
        # `row.get("at", 0.0) or 0.0` 会把合法 0.0 "或"成 0.0（结果对），但改成
        # 显式 None 判断更清晰、且防 row["at"] 为其它 falsy（如 ""）时的意外
        at_raw = row.get("at")
        at_val = 0.0 if at_raw is None else float(at_raw or 0.0)
        vol_raw = row.get("volume")
        vol_val = 1.0 if vol_raw is None else float(vol_raw)
        sfx.append(SfxCue(path=path, at_seconds=max(0.0, at_val),
                          volume=_clamp_vol(vol_val)))
    return AudioMix(master_volume=master_volume, bgm=bgm, sfx=sfx,
                    orig_video_volume=orig_video_volume)
