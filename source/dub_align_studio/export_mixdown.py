"""导出辅助层：从最终成片 MP4 提取"成片同款混音单轨"WAV，供剪映/Premiere 交接包使用。

产品契约（v0.7.71 P1-1）：
    · 剪映草稿包 / Premiere 工程的 A1 音频轨用**混音 WAV**（配音 + BGM + SFX + 原视频
      音效已在成片渲染时按用户音量全部叠好），保证打开工程后听感 == 成片；
    · BGM/SFX 暂**不承诺**独立可编辑轨道（要拆需要另做一版原始素材导出，未实现）；
    · 分镜段视频轨去音轨，避免与 A1 混音重复播放（防"配音+BGM"叠出双声）；
    · 提取失败必须明确报错，禁止静默回退到 master 冒充成功。

实现细节：
    · ffmpeg 硬指定 PCM16 / 48000Hz / stereo（与 Premiere 时间线声明一致）；
    · 临时文件 `.part.wav` → `Path.replace` 原子替换；失败清临时、不动旧 output；
    · 走项目现有 settings.ffmpeg_tool + integrated_workbench.proc.run_silent。
"""

from __future__ import annotations

from pathlib import Path

from integrated_workbench.proc import run_silent

from . import settings as studio_settings


MIXDOWN_SAMPLE_RATE = 48000     # Premiere 时间线常见 48kHz；与 render_b 音频输出保持一致
MIXDOWN_CHANNELS = 2
MIXDOWN_NAME = "mixdown.wav"    # 交接包里的固定文件名


class MixdownError(RuntimeError):
    """混音提取失败——上层给用户明确中文错，不静默降级到 master。"""


def _ffmpeg() -> str:
    """解析 ffmpeg 绝对路径；缺失时抛 MixdownError 给友好错。"""
    import shutil
    tool = studio_settings.ffmpeg_tool("ffmpeg")
    if tool and (Path(tool).is_file() or tool != "ffmpeg"):
        return tool
    which = shutil.which("ffmpeg")
    if which:
        return which
    raise MixdownError("未找到 ffmpeg（导出剪映/Premiere 需要它从成片提取混音单轨）。")


def extract_mixdown_wav(film_mp4: Path, out_wav: Path,
                        sample_rate: int = MIXDOWN_SAMPLE_RATE,
                        channels: int = MIXDOWN_CHANNELS) -> Path:
    """从 film_mp4 提取音轨为 PCM16/{sample_rate}/{channels}ch WAV，写入 out_wav。

    原子替换：先写 `<out_wav>.part.wav`，成功后 replace 覆盖旧 out_wav；失败清临时、
    不动旧文件（用户重导时不误删）。film_mp4 缺失、无音轨、ffmpeg 失败一律抛 MixdownError。"""
    film_mp4 = Path(film_mp4)
    out_wav = Path(out_wav)
    if not film_mp4.is_file():
        raise MixdownError(f"成片文件不存在：{film_mp4}（请先成功生成成片再导出交接包）")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg()
    tmp = out_wav.with_suffix(out_wav.suffix + ".part.wav")

    def _clean() -> None:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(film_mp4),
        "-vn",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-acodec", "pcm_s16le",
        str(tmp),
    ]
    try:
        try:
            completed = run_silent(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise MixdownError(f"调用 ffmpeg 失败：{exc}") from exc
        if getattr(completed, "returncode", 1) != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise MixdownError(f"从成片提取混音失败：{detail or '未知错误'}")
        if not tmp.is_file() or tmp.stat().st_size < 44:  # 至少一个 WAV 头
            raise MixdownError("成片提取出的音频为空——请检查成片是否有声音。")
        tmp.replace(out_wav)
    except Exception:
        _clean()
        raise
    finally:
        _clean()
    return out_wav


def make_silent_video(src_mp4: Path, dst_mp4: Path) -> Path:
    """把 src_mp4 复制成"无音轨"版本 dst_mp4，供剪映/Premiere 视频轨引用——
    这样时间线上 A1 混音 + V1 无音视频 = 只出一份声，不重叠。

    直接 `-c:v copy -an`：不重编码，秒级完成；失败抛 MixdownError。"""
    src_mp4 = Path(src_mp4)
    dst_mp4 = Path(dst_mp4)
    if not src_mp4.is_file():
        raise MixdownError(f"分镜段不存在：{src_mp4}")
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg()
    tmp = dst_mp4.with_suffix(dst_mp4.suffix + ".part.mp4")

    def _clean() -> None:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src_mp4),
        "-c:v", "copy", "-an",
        str(tmp),
    ]
    try:
        try:
            completed = run_silent(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise MixdownError(f"调用 ffmpeg 失败：{exc}") from exc
        if getattr(completed, "returncode", 1) != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise MixdownError(f"生成无音分镜段失败：{detail or '未知错误'}")
        # replace 前先检查临时文件真的写成了且非空（ffmpeg 有时返回 0 但输出损坏）
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise MixdownError(f"生成无音分镜段失败：临时文件为空 {tmp}")
        tmp.replace(dst_mp4)
    except Exception:
        _clean()
        raise
    finally:
        _clean()
    return dst_mp4
