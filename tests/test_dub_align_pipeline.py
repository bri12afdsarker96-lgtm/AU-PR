"""Dub Align Studio 编排层与应用外壳测试：胶水纯逻辑 + ffmpeg 门控全流程 + GUI 冒烟（tk 门控）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio import studio_pipeline as pipeline
from dub_align_studio.timing import LineTiming


class ShotListingTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="shots_"))

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_numeric_names_sort_by_value(self):
        for name in ("10.mp4", "2.mp4", "1.mp4", "备用.mov", "说明.txt"):
            (self.workdir / name).write_bytes(b"x")
        names = [p.name for p in pipeline.list_shot_videos(self.workdir)]
        self.assertEqual(names, ["1.mp4", "2.mp4", "10.mp4", "备用.mov"])

    def test_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            pipeline.list_shot_videos(self.workdir / "不存在")


class EvenSplitTests(unittest.TestCase):
    def test_split_sums_to_total_last_absorbs(self):
        timings = pipeline.even_split_timings(["一", "二", "三"], 17.0)
        self.assertEqual(len(timings), 3)
        self.assertAlmostEqual(sum(t.duration for t in timings), 17.0, places=3)
        self.assertAlmostEqual(timings[0].duration, round(17.0 / 3, 3))

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            pipeline.even_split_timings([], 10.0)
        with self.assertRaises(ValueError):
            pipeline.even_split_timings(["一"], 0.0)


class AspectConfigTests(unittest.TestCase):
    def test_five_common_aspects_map_to_canvas(self):
        expect = {"9:16 竖屏": (1080, 1920), "16:9 横屏": (1920, 1080), "1:1 方形": (1080, 1080),
                  "4:3": (1440, 1080), "3:4": (1080, 1440)}
        self.assertEqual(pipeline.ASPECT_KEYS, list(expect))
        for name, (w, h) in expect.items():
            config = pipeline.make_render_config(name)
            self.assertEqual((config.width, config.height), (w, h), name)

    def test_unknown_aspect_falls_back_to_default(self):
        config = pipeline.make_render_config("不存在")
        self.assertEqual((config.width, config.height), (1080, 1920))


class SegmentsFromOutputTests(unittest.TestCase):
    def test_reads_back_numbered_segments(self):
        root = Path(tempfile.mkdtemp(prefix="segs_"))
        try:
            seg_dir = root / "成片_segments"
            seg_dir.mkdir()
            for name in ("001.mp4", "002.mp4", "_full_silent.mp4"):
                (seg_dir / name).write_bytes(b"x")
            segments = pipeline.segments_from_output(root, 2)
            self.assertEqual([p.name for p in segments], ["001.mp4", "002.mp4"])
            with self.assertRaises(FileNotFoundError):
                pipeline.segments_from_output(root, 3)  # 数量不足
            with self.assertRaises(FileNotFoundError):
                pipeline.segments_from_output(root / "无", 1)  # 目录缺失
        finally:
            shutil.rmtree(root, ignore_errors=True)


class EngineFactoryTests(unittest.TestCase):
    def test_known_keys(self):
        for key in pipeline.ENGINE_KEYS:
            self.assertEqual(pipeline.make_engine(key).key, key)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            pipeline.make_engine("不存在")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class RunAllTests(unittest.TestCase):
    """一键全流程（mock 引擎 + 均分兜底）：master → 计时表 → 成片 → 剪映包。"""

    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="run_all_"))
        self.shots = self.workdir / "分镜"
        self.shots.mkdir()
        from dub_align_studio.render_b import RenderConfig, _run

        config = RenderConfig()
        for i in (1, 2):
            _run([config.ffmpeg, "-y", "-f", "lavfi", "-i",
                  "testsrc=size=640x360:rate=30:duration=6",
                  "-pix_fmt", "yuv420p", str(self.shots / f"{i}.mp4")], "样例分镜")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_full_pipeline_with_capcut(self):
        out = self.workdir / "输出"
        run = pipeline.run_all(
            "第一句台词。\n第二句台词。", "mock", "均分兜底",
            self.shots, out, export_capcut=True,
        )
        self.assertTrue(run.ok, str(run.result))
        self.assertTrue((out / pipeline.MASTER_NAME).exists())
        self.assertTrue((out / "配音计时表.csv").exists())
        self.assertTrue(run.result.output_path.exists())
        self.assertTrue(run.capcut and run.capcut.package_dir.exists())
        self.assertTrue(any("均分兜底" in note for note in run.notes))
        # 分步恢复：load_timings 能读回
        loaded = pipeline.load_timings(out)
        self.assertEqual(len(loaded), 2)

    def test_insufficient_videos_raise(self):
        (self.shots / "2.mp4").unlink()
        with self.assertRaises(ValueError):
            pipeline.run_all("一\n二", "mock", "均分兜底", self.shots, self.workdir / "x")


class VoiceDeleteTests(unittest.TestCase):
    def test_delete_is_idempotent(self):
        import wave

        from dub_align_studio import voice_library

        root = Path(tempfile.mkdtemp(prefix="voice_del_"))
        try:
            sample = root / "s.wav"
            with wave.open(str(sample), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(8000)
                handle.writeframes(b"\x00\x00" * 800)
            entry = voice_library.register_voice(root, "测试音色", sample)
            self.assertEqual(len(voice_library.list_voices(root)), 1)
            voice_library.delete_voice(root, entry.voice_id)
            self.assertEqual(voice_library.list_voices(root), [])
            voice_library.delete_voice(root, entry.voice_id)  # 再删不报错
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
