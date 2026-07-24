"""长文分块合成测试：超长整篇不再被截断/漂移（分块≤上限、内容无丢失、拼接时长正确）+ xlsx 跳表头。"""

import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from dub_align_studio.engines import MockEngine, SynthesisOptions
from dub_align_studio.engines.longform import split_for_synthesis, synthesize_long


class SplitTests(unittest.TestCase):
    def test_no_split_when_short(self):
        self.assertEqual(split_for_synthesis("一句话。", 120), ["一句话。"])

    def test_packs_lines_under_limit_no_loss(self):
        lines = ["甲" * 40, "乙" * 40, "丙" * 40, "丁" * 40]  # 4 行各 40 字
        chunks = split_for_synthesis("\n".join(lines), 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks), [len(c) for c in chunks])
        self.assertGreater(len(chunks), 1)
        joined = "".join(chunks).replace("\n", "")
        self.assertEqual(joined, "".join(lines))  # 内容一字不丢

    def test_splits_single_overlong_line_by_punct(self):
        line = "第一句。第二句！第三句？第四句；第五句，第六句。" * 3
        chunks = split_for_synthesis(line, 20)
        self.assertTrue(all(len(c) <= 20 for c in chunks), [len(c) for c in chunks])
        self.assertEqual("".join(chunks).replace("\n", ""), line)

    def test_hard_split_when_no_punct(self):
        line = "啊" * 250  # 无标点超长
        chunks = split_for_synthesis(line, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("".join(chunks), line)


class ConcatSynthTests(unittest.TestCase):
    def test_chunked_concat_duration_and_format(self):
        work = Path(tempfile.mkdtemp(prefix="lf_"))
        try:
            text = "\n".join(f"第{i}句台词内容。" for i in range(1, 7))  # 6 行
            master = synthesize_long(MockEngine(), text, None, work / "master.wav",
                                     SynthesisOptions(), max_chars=8)  # 强制分块
            self.assertTrue(master.path.exists())
            with wave.open(str(master.path), "rb") as w:
                dur = w.getnframes() / w.getframerate()
                self.assertEqual(w.getnchannels(), 1)
            self.assertAlmostEqual(dur, 6 * 5.0, delta=0.4)  # 6 行 × 5s floor
            self.assertAlmostEqual(master.seconds, dur, delta=0.05)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_short_text_single_pass(self):
        work = Path(tempfile.mkdtemp(prefix="lf1_"))
        try:
            master = synthesize_long(MockEngine(), "只有一句。", None, work / "m.wav",
                                     SynthesisOptions(), max_chars=120)
            self.assertTrue(master.path.exists())
            # 未分块时不产生 _chunks 目录
            self.assertFalse((work / "m_chunks").exists())
        finally:
            shutil.rmtree(work, ignore_errors=True)


class ManifestRedubTests(unittest.TestCase):
    def test_manifest_written_and_redub_single_segment(self):
        from dub_align_studio.engines.longform import read_manifest, redub_chunk, chunks_dir_for
        work = Path(tempfile.mkdtemp(prefix="man_"))
        try:
            text = "\n".join(f"第{i}句台词内容。" for i in range(1, 6))  # 5 行
            master = synthesize_long(MockEngine(), text, None, work / "master.wav",
                                     SynthesisOptions(), max_chars=8)
            man = read_manifest(master.path)
            self.assertIsNotNone(man)
            self.assertEqual(len(man["chunks"]), 5)
            self.assertTrue(all("text" in c and "file" in c for c in man["chunks"]))
            self.assertTrue((chunks_dir_for(master.path) / "分段清单.json").exists())
            frames_before = wave.open(str(master.path), "rb").getnframes()
            redub_chunk(MockEngine(), master.path, 3, None, SynthesisOptions())
            frames_after = wave.open(str(master.path), "rb").getnframes()
            self.assertEqual(frames_before, frames_after)  # 单段重配后总时长不变
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_redub_bad_index_raises(self):
        from dub_align_studio.engines.longform import redub_chunk
        work = Path(tempfile.mkdtemp(prefix="man2_"))
        try:
            master = synthesize_long(MockEngine(), "\n".join(["甲。", "乙。", "丙。"]),
                                     None, work / "m.wav", SynthesisOptions(), max_chars=2)
            with self.assertRaises(ValueError):
                redub_chunk(MockEngine(), master.path, 99, None, SynthesisOptions())
        finally:
            shutil.rmtree(work, ignore_errors=True)


class PerLineTests(unittest.TestCase):
    def test_per_line_one_segment_per_line_and_progress(self):
        from dub_align_studio.engines.longform import read_manifest
        work = Path(tempfile.mkdtemp(prefix="pl_"))
        try:
            text = "\n".join(f"第{i}句台词。" for i in range(1, 5))  # 4 行
            seen: list = []
            master = synthesize_long(MockEngine(), text, None, work / "master.wav",
                                     SynthesisOptions(), max_chars=1_000_000,
                                     per_line=True, progress=lambda d, t: seen.append((d, t)))
            man = read_manifest(master.path)
            self.assertEqual(len(man["chunks"]), 4)          # 一行一段（即便 max_chars 极大也逐行）
            self.assertEqual([d for d, _ in seen], [1, 2, 3, 4])  # 进度逐行回调
            self.assertEqual(seen[-1], (4, 4))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_exact_timing_from_line_audio(self):
        from dub_align_studio.studio_pipeline import _timings_from_line_audio
        from dub_align_studio.engines.longform import read_manifest
        work = Path(tempfile.mkdtemp(prefix="plt_"))
        try:
            lines = [f"第{i}句台词。" for i in range(1, 4)]
            master = synthesize_long(MockEngine(), "\n".join(lines), None, work / "master.wav",
                                     SynthesisOptions(), max_chars=1_000_000, per_line=True)
            timings = _timings_from_line_audio(master.path, lines)
            self.assertIsNotNone(timings)
            self.assertEqual(len(timings), 3)
            man = read_manifest(master.path)
            for t, c in zip(timings, man["chunks"]):
                self.assertAlmostEqual(t.duration, c["seconds"], places=2)  # 行时长=段音频真实时长
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_no_manifest_returns_none(self):
        from dub_align_studio.studio_pipeline import _timings_from_line_audio
        work = Path(tempfile.mkdtemp(prefix="pln_"))
        try:
            # 单句单次合成 → 无分段清单 → 精确逐行不可用，回退（返回 None）
            master = synthesize_long(MockEngine(), "只有一句。", None, work / "m.wav",
                                     SynthesisOptions(), max_chars=120)
            self.assertIsNone(_timings_from_line_audio(master.path, ["只有一句。"]))
        finally:
            shutil.rmtree(work, ignore_errors=True)


class MockTimbreTests(unittest.TestCase):
    def test_params_change_mock_timbre(self):
        # 不同 seed/步数/引导 → 不同基频倍率（无 GPU 也能听出参数生效）
        s1 = MockEngine._timbre_shift(None, SynthesisOptions(seed=1))
        s2 = MockEngine._timbre_shift(None, SynthesisOptions(seed=2))
        s3 = MockEngine._timbre_shift(None, SynthesisOptions(num_steps=30))
        self.assertEqual(len({s1, s2, s3}), 3)
        self.assertTrue(all(0.6 <= x <= 1.7 for x in (s1, s2, s3)))


class EngineLimitTests(unittest.TestCase):
    def test_engines_declare_max_chars(self):
        from dub_align_studio.engines import DotsLocalEngine, FishLocalEngine

        self.assertLessEqual(DotsLocalEngine().max_chars, 200)   # 保守，避免截断
        self.assertLessEqual(FishLocalEngine().max_chars, 300)
        self.assertGreater(MockEngine().max_chars, 100000)       # mock 不分块，测试口径不变


if __name__ == "__main__":
    unittest.main()
