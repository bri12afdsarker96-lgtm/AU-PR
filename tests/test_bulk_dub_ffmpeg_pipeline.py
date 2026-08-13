"""ffmpeg 视频管线测试。覆盖点 8-19（部分依赖真 ffmpeg，找不到时用 skip）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import ffmpeg_pipeline as vp  # noqa: E402
from dub_align_studio.bulk_dub.ffmpeg_pipeline import (  # noqa: E402
    build_filter_chain, build_output_filename, voice_short_name,
)


def test_8_original_first_mirror_second():
    lines, _ = build_filter_chain(width=1080, height=1920, final_seconds=10)
    joined = ";".join(lines)
    assert "[v0][v1]concat=n=2:v=1" in joined


def test_9_mirror_branch_hflip():
    lines, _ = build_filter_chain(width=1080, height=1920, final_seconds=10)
    joined = ";".join(lines)
    assert "hflip" in joined


def test_10_mirror_branch_zoom130_and_center_crop():
    lines, _ = build_filter_chain(width=1080, height=1920, zoom_percent=130,
                                    final_seconds=10)
    joined = ";".join(lines)
    assert "scale=1404:2496" in joined
    assert "crop=1080:1920:(in_w-1080)/2:(in_h-1920)/2" in joined


def test_11_odd_dimensions_snapped_even():
    lines, _ = build_filter_chain(width=100, height=101, zoom_percent=130,
                                    final_seconds=5)
    joined = ";".join(lines)
    assert "scale=130:132" in joined


def test_14_final_seconds_maps_to_t_arg():
    _, args = build_filter_chain(width=100, height=100, final_seconds=7.35)
    assert "-t" in args
    idx = args.index("-t")
    assert args[idx + 1] == "7.350"


def test_15_default_no_original_audio():
    _, args = build_filter_chain(width=100, height=100, final_seconds=5,
                                    keep_original_audio=False)
    assert "1:a:0" in args
    assert not any("amix" in a for a in args)


def test_16_keep_original_audio_uses_amix():
    lines, args = build_filter_chain(width=100, height=100, final_seconds=5,
                                        keep_original_audio=True)
    joined = ";".join(lines)
    assert "amix=inputs=2" in joined
    assert "alimiter" in joined
    assert "[aout]" in args


def test_18_no_subtitle_filter_in_chain():
    lines, _ = build_filter_chain(width=100, height=100, final_seconds=5)
    joined = ";".join(lines)
    for token in ("subtitles=", "ass=", "drawtext="):
        assert token not in joined


def test_19_no_atempo_or_speed_filter():
    lines, args = build_filter_chain(width=100, height=100, final_seconds=5,
                                        keep_original_audio=True)
    joined = ";".join(lines) + " " + " ".join(args)
    assert "atempo" not in joined
    assert "setpts=" not in joined


def test_voice_short_name_extracts_before_paren():
    assert voice_short_name("晓双（女·青春）") == "晓双"
    assert voice_short_name("晓双(女·青春)") == "晓双"
    assert voice_short_name("") == "配音"


def test_build_output_filename_uses_stem_and_voice():
    assert build_output_filename("C:/vids/001.mp4", "晓双") == "001_晓双_带货.mp4"


def test_43_next_unique_path_avoids_overwrite(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    from dub_align_studio.bulk_dub.ffmpeg_pipeline import _next_unique_path
    p = _next_unique_path(tmp_path / "a.mp4")
    assert p.name == "a_2.mp4"
    p.write_bytes(b"x")
    p2 = _next_unique_path(tmp_path / "a.mp4")
    assert p2.name == "a_3.mp4"


def test_cleanup_staging_refuses_outside_bulkdub(tmp_path):
    """安全护栏：cleanup_staging 只清理 批量带货/staging 白名单下的目录，
    避免因参数被污染而误删 CWD 或数据总目录。"""
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "file.txt").write_bytes(b"important")
    vp.cleanup_staging(str(victim))
    assert victim.exists() and (victim / "file.txt").exists(), \
        "cleanup_staging 不得删除非白名单目录"


def test_cleanup_staging_clears_bulkdub_staging(tmp_path):
    staging = tmp_path / "批量带货" / "staging" / "batch1" / "task1"
    staging.mkdir(parents=True)
    (staging / "tts.wav").write_bytes(b"x")
    vp.cleanup_staging(str(staging))
    assert not staging.exists()


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_ffprobe_video_reads_generated_sample(tmp_path):
    import subprocess
    sample = tmp_path / "s.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=2:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(sample),
    ], check=True)
    probe = vp.ffprobe_video(sample)
    assert probe.width == 320 and probe.height == 240
    assert 1.9 <= probe.duration <= 2.2
    assert probe.has_audio is True


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_render_single_video_shorter_than_tts_trims_audio(tmp_path):
    """13/14: TTS 较长时裁旁白，最终时长 = min(2V, TTS)。"""
    import subprocess
    from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe

    video = tmp_path / "v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    tts = tmp_path / "tts.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5", str(tts),
    ], check=True)

    encoder = EncoderProbe("cpu", "libx264", [], True, "")
    out = tmp_path / "out.mp4"
    staging = tmp_path / "staging"
    result = vp.render_single(
        input_video=video, tts_audio=tts, output_path=out,
        staging_dir=staging, encoder=encoder,
    )
    assert 1.8 <= result.final_duration <= 2.2


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_render_single_tts_shorter_than_video_trims_video(tmp_path):
    """12/14: 视频较长时裁视频。"""
    import subprocess
    from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe

    video = tmp_path / "v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=3:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    tts = tmp_path / "tts.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(tts),
    ], check=True)

    encoder = EncoderProbe("cpu", "libx264", [], True, "")
    out = tmp_path / "out.mp4"
    staging = tmp_path / "staging"
    result = vp.render_single(
        input_video=video, tts_audio=tts, output_path=out,
        staging_dir=staging, encoder=encoder,
    )
    assert 1.8 <= result.final_duration <= 2.4


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_17_no_audio_video_still_succeeds_with_warning(tmp_path):
    """17: 输入视频无音轨时，勾选保留原声也能出片，只带旁白，且带黄色警告。"""
    import subprocess
    from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe

    silent_video = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=2:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(silent_video),
    ], check=True)
    tts = tmp_path / "tts.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(tts),
    ], check=True)

    encoder = EncoderProbe("cpu", "libx264", [], True, "")
    out = tmp_path / "out.mp4"
    staging = tmp_path / "staging"
    result = vp.render_single(
        input_video=silent_video, tts_audio=tts, output_path=out,
        staging_dir=staging, encoder=encoder,
        keep_original_audio=True,
    )
    assert Path(result.output_path).is_file()
    assert any("原声" in w or "旁白" in w for w in result.warnings)
