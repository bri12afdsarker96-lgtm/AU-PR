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
    """R14-FIX-4d：v0 现在被 setsar=1 归一化成 [v0s]，concat 变成 [v0s][v1]。
    concat 后 format=yuv420p 强制 8bit 输出避免 encoder 侧冲突。"""
    lines, _ = build_filter_chain(width=1080, height=1920, final_seconds=10)
    joined = ";".join(lines)
    assert "[v0s][v1]concat=n=2:v=1" in joined
    assert "[v0]setsar=1[v0s]" in joined
    assert "format=yuv420p" in joined


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


def test_cleanup_staging_refuses_outside_bulkdub(tmp_path):
    """安全护栏：cleanup_staging 只清理白名单路径。"""
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "file.txt").write_bytes(b"important")
    assert vp.cleanup_staging(str(victim)) is False
    assert victim.exists() and (victim / "file.txt").exists()


def test_cleanup_staging_clears_bulkdub_staging(tmp_path, monkeypatch):
    """真正符合白名单：<data_root>/批量带货/staging/<batch>/<task>/。"""
    fake_data_root = tmp_path / "data"
    (fake_data_root / "批量带货" / "staging").mkdir(parents=True)
    from dub_align_studio import settings as studio_settings
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake_data_root)
    vp._is_relative_to  # sanity
    batch_id = "a1b2c3d4e5f6"   # 12 hex → 通过 is_safe_id
    task_id = "0" * 16
    staging = fake_data_root / "批量带货" / "staging" / batch_id / task_id
    staging.mkdir(parents=True)
    (staging / "tts.wav").write_bytes(b"x")
    assert vp.cleanup_staging(str(staging)) is True
    assert not staging.exists()


def test_cleanup_staging_refuses_root_itself(tmp_path, monkeypatch):
    from dub_align_studio import settings as studio_settings
    fake = tmp_path / "d"
    (fake / "批量带货" / "staging").mkdir(parents=True)
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    root = fake / "批量带货" / "staging"
    assert vp.cleanup_staging(str(root)) is False


def test_cleanup_staging_refuses_symlink(tmp_path, monkeypatch):
    from dub_align_studio import settings as studio_settings
    fake = tmp_path / "d"
    (fake / "批量带货" / "staging").mkdir(parents=True)
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    real = tmp_path / "real_target"
    real.mkdir()
    (real / "victim.txt").write_bytes(b"live")
    # 建符号链接指向白名单外的目录
    batch_id = "a" * 12
    task_id = "b" * 16
    link_parent = fake / "批量带货" / "staging" / batch_id
    link_parent.mkdir(parents=True)
    link = link_parent / task_id
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("环境不支持 symlink")
    assert vp.cleanup_staging(str(link)) is False
    assert (real / "victim.txt").exists(), "符号链接指向的真实目录不得被删"


def test_cleanup_staging_refuses_unsafe_id(tmp_path, monkeypatch):
    from dub_align_studio import settings as studio_settings
    fake = tmp_path / "d"
    (fake / "批量带货" / "staging").mkdir(parents=True)
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    # batch_id 含 "..." 或非 hex → 拒绝
    bad = fake / "批量带货" / "staging" / "..%2fetc" / ("0" * 16)
    bad.mkdir(parents=True, exist_ok=True)
    assert vp.cleanup_staging(str(bad)) is False
    assert bad.exists()


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
        input_video=video, tts_audio=tts, reserved_output=out,
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
        input_video=video, tts_audio=tts, reserved_output=out,
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
        input_video=silent_video, tts_audio=tts, reserved_output=out,
        staging_dir=staging, encoder=encoder,
        keep_original_audio=True,
    )
    assert Path(result.output_path).is_file()
    assert any("原声" in w or "旁白" in w for w in result.warnings)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_render_single_refuses_to_overwrite_existing_output(tmp_path):
    """P0-3：预留路径已被占用时拒绝覆盖。"""
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
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(tts),
    ], check=True)

    encoder = EncoderProbe("cpu", "libx264", [], True, "")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"OLD-CONTENT-DO-NOT-OVERWRITE")
    staging = tmp_path / "staging"
    with pytest.raises(vp.VideoError):
        vp.render_single(
            input_video=video, tts_audio=tts, reserved_output=out,
            staging_dir=staging, encoder=encoder,
        )
    # 旧文件内容不变
    assert out.read_bytes() == b"OLD-CONTENT-DO-NOT-OVERWRITE"
