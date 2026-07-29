"""Dub Align Studio M2a 测试：引擎接口 / MockEngine / dots 探测降级 / 音色库（纯逻辑为主）。"""

import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from dub_align_studio import voice_library
from dub_align_studio.engines import DotsLocalEngine, DubEngine, EngineUnavailable, MockEngine
from dub_align_studio.engines.base import wav_seconds


class MockEngineTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="dub_engines_"))

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_full_synthesis_writes_master_and_metadata(self):
        engine = MockEngine(durations=[6.0, 7.5, 5.0])
        master = engine.synthesize_full("一\n二\n三", None, self.workdir / "master.wav")
        self.assertTrue(master.path.exists())
        self.assertAlmostEqual(master.seconds, 18.5)
        self.assertAlmostEqual(wav_seconds(master.path), 18.5, places=3)
        meta = json.loads(master.metadata_path().read_text(encoding="utf-8"))
        self.assertEqual(meta["engine"], "mock")
        self.assertEqual(meta["sample_rate"], 44100)
        self.assertAlmostEqual(meta["seconds"], 18.5)

    def test_default_durations_use_five_second_floor(self):
        master = MockEngine().synthesize_full("一\n二", None, self.workdir / "m.wav")
        self.assertAlmostEqual(master.seconds, 10.0)

    def test_line_count_mismatch_and_empty_text(self):
        with self.assertRaises(ValueError):
            MockEngine(durations=[5.0]).synthesize_full("一\n二", None, self.workdir / "m.wav")
        with self.assertRaises(ValueError):
            MockEngine().synthesize_full("   \n  ", None, self.workdir / "m.wav")

    def test_satisfies_engine_protocol(self):
        self.assertIsInstance(MockEngine(), DubEngine)
        self.assertIsInstance(DotsLocalEngine(), DubEngine)


class DotsLocalDegradationTests(unittest.TestCase):
    """无 dots.tts / 无 GPU 环境下的确定性行为：明确降级，不静默卡死。"""

    def test_probe_reports_unavailable_with_guidance(self):
        status = DotsLocalEngine().probe()
        # 本仓库 CI/开发容器无 dots.tts 与 GPU：探测必须给出可读原因。
        if not status.available:
            self.assertTrue(status.detail)
            self.assertEqual(status.key, "dots_local")

    def test_synthesize_raises_engine_unavailable_when_absent(self):
        engine = DotsLocalEngine()
        if engine.probe().available:  # 真机装了 dots.tts 则跳过（非确定组件不进单测）
            self.skipTest("dots.tts 可用，跳过降级路径测试")
        with self.assertRaises(EngineUnavailable):
            engine.synthesize_full("你好", None, Path(tempfile.gettempdir()) / "dots_master.wav")


class VoiceLibraryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="voice_lib_"))
        self.sample = self.root / "sample.wav"
        with wave.open(str(self.sample), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_register_list_get_roundtrip(self):
        entry = voice_library.register_voice(self.root, "主播甲", self.sample, transcript="参考句子")
        self.assertTrue(entry.reference_wav.exists())
        listed = voice_library.list_voices(self.root)
        self.assertEqual([v.voice_id for v in listed], [entry.voice_id])
        got = voice_library.get_voice(self.root, entry.voice_id)
        self.assertEqual(got.name, "主播甲")
        self.assertEqual(got.transcript, "参考句子")
        ref = got.to_ref()
        self.assertEqual(ref.voice_id, entry.voice_id)
        self.assertEqual(ref.transcript, "参考句子")

    def test_mp3_reference_keeps_suffix(self):
        mp3 = self.root / "ref.mp3"
        mp3.write_bytes(b"\xff\xfb" + b"\x00" * 100)
        entry = voice_library.register_voice(self.root, "mp3音色", mp3)
        self.assertEqual(entry.reference_wav.suffix, ".mp3")
        self.assertEqual(voice_library.get_voice(self.root, entry.voice_id).reference_wav.suffix, ".mp3")

    def test_duplicate_names_get_unique_ids(self):
        first = voice_library.register_voice(self.root, "主播", self.sample)
        second = voice_library.register_voice(self.root, "主播", self.sample)
        self.assertNotEqual(first.voice_id, second.voice_id)
        self.assertEqual(len(voice_library.list_voices(self.root)), 2)

    def test_missing_voice_and_missing_reference(self):
        with self.assertRaises(KeyError):
            voice_library.get_voice(self.root, "不存在")
        with self.assertRaises(FileNotFoundError):
            voice_library.register_voice(self.root, "缺文件", self.root / "无.wav")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class EnginePipelineTests(unittest.TestCase):
    """L2→L3→L5 全链路：MockEngine 出 master → MockAligner 量时长 → B 渲染收口。"""

    def test_master_from_engine_feeds_render_pipeline(self):
        from dub_align_studio.render_b import RenderConfig, render_b
        from dub_align_studio.timing import MockAligner

        workdir = Path(tempfile.mkdtemp(prefix="engine_pipe_"))
        try:
            lines = ["第一句台词内容。", "第二句台词内容。"]
            durations = [6.0, 5.5]
            master = MockEngine(durations=durations).synthesize_full(
                "\n".join(lines), None, workdir / "master.wav"
            )
            timings = MockAligner(durations=durations).measure(master.path, lines)

            config = RenderConfig()
            videos = []
            for i in range(2):
                clip = workdir / f"clip_{i}.mp4"
                from dub_align_studio.render_b import _run

                _run(
                    [config.ffmpeg, "-y", "-f", "lavfi", "-i",
                     "testsrc=size=640x360:rate=30:duration=4",
                     "-pix_fmt", "yuv420p", str(clip)],
                    "生成测试画面",
                )
                videos.append(clip)

            result = render_b(master.path, timings, videos, workdir / "out.mp4", config)
            self.assertTrue(result.ok, f"收口断言未通过：{result}")
            self.assertAlmostEqual(result.master_seconds, 11.5, places=2)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
