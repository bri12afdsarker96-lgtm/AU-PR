"""Dub Align Studio M1.5 测试：帧收口 / 单段滤镜 / Mock 尺子（纯逻辑）+ 端到端 B 渲染（ffmpeg 门控）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio import frames, timing
from dub_align_studio.render_b import _staged_font, shot_video_filter


class StagedFontTests(unittest.TestCase):
    """字幕字体落到含 & / 非 ASCII 的路径时，Windows drawtext 加载失败→□□□。
    _staged_font 应把它复制到一条纯 ASCII、不含 & 的路径再交给 ffmpeg。"""

    def test_ascii_font_returned_asis(self):
        base = Path(tempfile.mkdtemp())
        f = base / "font.ttf"
        f.write_bytes(b"FONTDATA")
        # 纯 ASCII 且无 & → 原样返回，不复制
        self.assertEqual(_staged_font(f, base), f)

    def test_ampersand_path_is_restaged_to_safe_path(self):
        root = Path(tempfile.mkdtemp())
        bad = root / "AU&PR" / "字体"
        bad.mkdir(parents=True)
        src = bad / "SmileySans.ttf"
        src.write_bytes(b"FONTDATA123")
        work = root / "out" / "成片_segments"
        work.mkdir(parents=True)
        staged = _staged_font(src, work)
        self.assertNotEqual(staged, src)
        self.assertNotIn("&", str(staged))     # 关键：坏字符已去除
        self.assertTrue(str(staged).isascii())  # 关键：落点纯 ASCII
        self.assertEqual(staged.read_bytes(), b"FONTDATA123")  # 内容一致


class QuantizeToFramesTests(unittest.TestCase):
    def test_sum_locks_to_master_total(self):
        # 三行变长时长，master 实测 18.5s @30fps → 总帧数必须精确 555。
        f = frames.quantize_to_frames([6.0, 7.5, 5.0], 30, total_seconds=18.5)
        self.assertEqual(sum(f), 555)
        self.assertEqual(f, [180, 225, 150])

    def test_last_segment_absorbs_rounding_residual(self):
        # 逐行含舍入误差，末段吸收残差使总帧数对齐 round(total*fps)。
        f = frames.quantize_to_frames([5.017, 5.017, 5.017], 30, total_seconds=15.051)
        self.assertEqual(sum(f), round(15.051 * 30))

    def test_every_segment_at_least_one_frame(self):
        f = frames.quantize_to_frames([0.0, 0.0, 5.0], 30, total_seconds=5.0)
        self.assertTrue(all(n >= 1 for n in f))
        self.assertEqual(sum(f), 150)

    def test_empty_and_fps_guard(self):
        self.assertEqual(frames.quantize_to_frames([], 30), [])
        with self.assertRaises(ValueError):
            frames.quantize_to_frames([5.0], 0)


class ShotVideoFilterTests(unittest.TestCase):
    def test_trim_branch_has_no_slowdown(self):
        # 画面比音频长 → 裁剪，不变速（无 setpts 慢放因子）。
        parts, note = shot_video_filter(10.0, 6.0, 1080, 1920, 30, "裁剪多余画面")
        joined = ",".join(parts)
        self.assertIn("setpts=PTS-STARTPTS", joined)
        self.assertNotIn("/0.", joined)  # 无放慢
        self.assertIn("scale=1080:1920", joined)
        self.assertIn("pad=1080:1920", joined)
        self.assertIn("setsar=1", joined)
        self.assertIn("fps=30", joined)

    def test_short_source_slows_down(self):
        # 画面比音频短 → 放慢补足，出现 setpts 除以 speed。
        parts, note = shot_video_filter(4.0, 7.5, 1080, 1920, 30, "裁剪多余画面")
        joined = ",".join(parts)
        self.assertIn("setpts=(PTS-STARTPTS)/", joined)
        self.assertIn("tpad=stop_mode=clone", joined)

    def test_always_normalizes_canvas(self):
        for src, tgt in [(10.0, 6.0), (4.0, 7.5), (5.0, 5.0)]:
            parts, _ = shot_video_filter(src, tgt, 720, 1280, 30)
            joined = ",".join(parts)
            self.assertIn("scale=720:1280", joined)
            self.assertIn("fps=30", joined)


class MockAlignerTests(unittest.TestCase):
    def test_measure_produces_per_line_timings(self):
        aligner = timing.MockAligner(durations=[6.0, 7.5, 5.0])
        out = aligner.measure(Path("master.wav"), ["一", "二", "三"])
        self.assertEqual([t.duration for t in out], [6.0, 7.5, 5.0])
        self.assertEqual([t.index for t in out], [1, 2, 3])
        self.assertEqual(out[1].text, "二")

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            timing.MockAligner(durations=[6.0]).measure(Path("m.wav"), ["一", "二"])

    def test_total_and_floor(self):
        out = timing.MockAligner(durations=[6.0, 7.5, 5.0]).measure(Path("m"), ["a", "b", "c"])
        self.assertAlmostEqual(timing.total_duration(out), 18.5)
        self.assertEqual(timing.floor_violations(out), [])
        low = timing.MockAligner(durations=[3.0, 7.5, 5.0]).measure(Path("m"), ["a", "b", "c"])
        self.assertEqual(timing.floor_violations(low), [1])  # 3s < 5s 下限


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class EndToEndBRenderTests(unittest.TestCase):
    def test_verify_renders_frame_locked_film_with_audio(self):
        from dub_align_studio import cli

        workdir = Path(tempfile.mkdtemp(prefix="dub_align_b_test_"))
        try:
            self.assertEqual(cli.verify(workdir=workdir, keep=True), 0)
            out = workdir / "成片.mp4"
            self.assertTrue(out.exists())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
