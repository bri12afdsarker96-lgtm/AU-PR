"""Dub Align Studio M3 测试：行时长分配纯逻辑 / SRT 解析 / 计时表契约 / 尺子与引擎降级。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio.aligners import Cue, WhisperAligner, allocate_line_durations, parse_srt_cues
from dub_align_studio.engines import FishLocalEngine, MockEngine, SynthesisOptions
from dub_align_studio.timing import LineTiming, read_timing_table, total_duration, write_timing_table


class AllocateLineDurationsTests(unittest.TestCase):
    def test_even_lines_split_evenly(self):
        # 两行等字数，线索均匀铺满 0~10s → 各 5s。
        cues = [Cue(0.0, 10.0, "一二三四五六七八")]
        out = allocate_line_durations(cues, ["一二三四", "五六七八"], 10.0)
        self.assertEqual([t.duration for t in out], [5.0, 5.0])
        self.assertAlmostEqual(total_duration(out), 10.0)

    def test_boundary_interpolates_inside_cue(self):
        # 一条线索跨两行：行 1 占 1/4 字符 → 边界在线索 1/4 处。
        cues = [Cue(2.0, 10.0, "一二三四五六七八")]
        out = allocate_line_durations(cues, ["一二", "三四五六七八"], 12.0)
        self.assertAlmostEqual(out[0].duration, 4.0, places=3)  # 2.0 + 8*(2/8)=4.0
        self.assertAlmostEqual(out[1].duration, 8.0, places=3)  # 12 - 4
        self.assertAlmostEqual(total_duration(out), 12.0)

    def test_gap_between_cues_belongs_to_previous_line(self):
        # 行 1 恰好耗尽线索 1（0~4s），线索间静音 4~6s 归行 1（气口跟前句）。
        cues = [Cue(0.0, 4.0, "第一句话啊"), Cue(6.0, 10.0, "第二句话啊")]
        out = allocate_line_durations(cues, ["第一句话啊", "第二句话啊"], 10.0)
        self.assertAlmostEqual(out[0].duration, 4.0, places=3)
        self.assertAlmostEqual(out[1].duration, 6.0, places=3)

    def test_punctuation_ignored_in_ratio(self):
        cues = [Cue(0.0, 10.0, "你好，世界！你好，世界！")]
        out = allocate_line_durations(cues, ["你好，世界！", "你好、世界。"], 10.0)
        self.assertAlmostEqual(out[0].duration, 5.0, places=3)

    def test_sum_always_equals_total(self):
        # 线索时长与 total 不一致（whisper 尾部漏识别）→ 末行吸收尾差。
        cues = [Cue(0.0, 7.0, "一二三四五六")]
        out = allocate_line_durations(cues, ["一二三", "四五六"], 11.5)
        self.assertAlmostEqual(total_duration(out), 11.5)

    def test_rejects_empty_inputs(self):
        with self.assertRaises(RuntimeError):
            allocate_line_durations([], ["一句"], 5.0)
        with self.assertRaises(ValueError):
            allocate_line_durations([Cue(0, 5, "字")], ["！！！"], 5.0)
        with self.assertRaises(ValueError):
            allocate_line_durations([Cue(0, 5, "字")], ["行"], 0.0)


class ParseSrtTests(unittest.TestCase):
    def test_parses_blocks_with_index_and_text(self):
        srt = (
            "1\n00:00:00,000 --> 00:00:04,500\n第一句台词\n\n"
            "2\n00:00:05,000 --> 00:00:09,250\n第二句台词\n"
        )
        cues = parse_srt_cues(srt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 0.0)
        self.assertAlmostEqual(cues[0].end, 4.5)
        self.assertEqual(cues[1].text, "第二句台词")
        self.assertAlmostEqual(cues[1].start, 5.0)

    def test_skips_malformed_blocks(self):
        srt = "垃圾块\n没有时间轴\n\n1\n00:00:00,000 --> 00:00:02,000\n有效\n"
        cues = parse_srt_cues(srt)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].text, "有效")

    def test_empty_content(self):
        self.assertEqual(parse_srt_cues(""), [])


class TimingTableTests(unittest.TestCase):
    def test_write_read_roundtrip_with_cumulative_start_end(self):
        workdir = Path(tempfile.mkdtemp(prefix="timing_table_"))
        try:
            timings = [
                LineTiming(1, "第一句", 6.0),
                LineTiming(2, "第二句", 7.5),
                LineTiming(3, "第三句", 5.0),
            ]
            path = write_timing_table(workdir / "配音计时表.csv", timings)
            content = path.read_text(encoding="utf-8-sig")
            self.assertIn("6.000", content)
            self.assertIn("13.500", content)  # 第二行 end = 6+7.5
            back = read_timing_table(path)
            self.assertEqual([t.duration for t in back], [6.0, 7.5, 5.0])
            self.assertEqual(back[1].text, "第二句")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_read_rejects_empty_table(self):
        workdir = Path(tempfile.mkdtemp(prefix="timing_table_"))
        try:
            empty = workdir / "空表.csv"
            empty.write_text("index,text,start,end,duration\n", encoding="utf-8-sig")
            with self.assertRaises(RuntimeError):
                read_timing_table(empty)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class DegradationTests(unittest.TestCase):
    """无 whisper 组件 / 无 fish 服务环境：明确降级，不静默卡死。"""

    def test_whisper_probe_gives_guidance_when_missing(self):
        status = WhisperAligner().probe()
        if not status.available:
            self.assertTrue(status.detail)

    def test_whisper_measure_raises_when_unavailable(self):
        aligner = WhisperAligner()
        if aligner.probe().available:
            self.skipTest("whisper 可用，跳过降级路径测试")
        with self.assertRaises(RuntimeError):
            aligner.measure(Path("不存在.wav"), ["一句"])

    def test_fish_probe_reports_server_offline(self):
        engine = FishLocalEngine(base_url="http://127.0.0.1:1")  # 必然连不上的端口
        status = engine.probe()
        self.assertFalse(status.available)
        self.assertIn("fish", status.detail.lower() + status.key)


class SynthesisOptionsTests(unittest.TestCase):
    def test_defaults_match_integration_panel(self):
        options = SynthesisOptions()
        self.assertEqual(options.num_steps, 10)
        self.assertAlmostEqual(options.guidance_scale, 1.2)
        self.assertAlmostEqual(options.speed, 1.0)
        self.assertAlmostEqual(options.max_pause_seconds, 0.0)
        self.assertEqual(options.seed, 42)

    def test_options_recorded_in_master_metadata(self):
        import json

        workdir = Path(tempfile.mkdtemp(prefix="options_meta_"))
        try:
            options = SynthesisOptions(max_pause_seconds=0.2, seed=7)
            master = MockEngine(durations=[5.0]).synthesize_full("一句", None, workdir / "m.wav", options)
            meta = json.loads(master.metadata_path().read_text(encoding="utf-8"))
            self.assertEqual(meta["options"]["seed"], 7)
            self.assertAlmostEqual(meta["options"]["max_pause_seconds"], 0.2)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
