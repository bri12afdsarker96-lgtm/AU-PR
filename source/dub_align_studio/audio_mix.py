"""音频混流：master 配音 + 可选 BGM（循环）+ 若干音效（定点），各自可调音量。

需求（用户 2026-07-22 第五轮）：
    - master 配音音量可调；
    - 选一首 BGM，音量可调，视频比 BGM 长时**循环**播放到片尾；
    - 在音轨任意位置放音效，位置即触发时刻，单个音效音量可调；
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

    def is_trivial(self) -> bool:
        """无 BGM、无音效、master 原音量 → 走原来的直接映射路径（不进 filter_complex）。"""
        return self.bgm is None and not self.sfx and abs(self.master_volume - 1.0) < 1e-3


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


def build_audio_filtergraph(mix: AudioMix, master_input: int = 1) -> tuple[str, str]:
    """构造音频 filter_complex，返回 (filtergraph, out_label)。

    - master：input=master_input，音量 master_volume；
    - BGM：紧随 master 的输入号，音量 bgm.volume（已 -stream_loop 循环）；
    - 音效：依次其后，adelay 到 at_seconds、音量 sfx.volume；
    - amix duration=first → 输出长度对齐 master（BGM 循环被裁到片尾，音效自动补静音）。
    统一 aformat=44100/stereo，避免 amix 因格式不一致失败。"""
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    chains: list[str] = []
    labels: list[str] = []

    chains.append(f"[{master_input}:a]volume={_clamp_vol(mix.master_volume):.3f},{fmt}[am]")
    labels.append("[am]")

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


def mix_from_payload(payload: dict, resolve: "callable[[str], Path | None]") -> AudioMix:
    """从前端 JSON 还原 AudioMix。resolve(name)->Path 把资产文件名映射到磁盘路径
    （不存在的资产静默跳过，成片不因缺一个音效而失败）。"""
    if not isinstance(payload, dict):
        return AudioMix()
    master_volume = _clamp_vol(payload.get("master_volume", 1.0))
    bgm = None
    bgm_raw = payload.get("bgm") or {}
    if isinstance(bgm_raw, dict) and bgm_raw.get("file"):
        path = resolve(str(bgm_raw["file"]))
        if path is not None:
            bgm = BgmTrack(path=path, volume=_clamp_vol(bgm_raw.get("volume", 0.35)),
                           loop=bool(bgm_raw.get("loop", True)))
    sfx: list[SfxCue] = []
    for row in payload.get("sfx") or []:
        if not isinstance(row, dict) or not row.get("file"):
            continue
        path = resolve(str(row["file"]))
        if path is None:
            continue
        sfx.append(SfxCue(path=path, at_seconds=max(0.0, float(row.get("at", 0.0) or 0.0)),
                          volume=_clamp_vol(row.get("volume", 1.0))))
    return AudioMix(master_volume=master_volume, bgm=bgm, sfx=sfx)
