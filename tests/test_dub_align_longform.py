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


class EngineLimitTests(unittest.TestCase):
    def test_engines_declare_max_chars(self):
        from dub_align_studio.engines import DotsLocalEngine, FishLocalEngine

        self.assertLessEqual(DotsLocalEngine().max_chars, 200)   # 保守，避免截断
        self.assertLessEqual(FishLocalEngine().max_chars, 300)
        self.assertGreater(MockEngine().max_chars, 100000)       # mock 不分块，测试口径不变


if __name__ == "__main__":
    unittest.main()
