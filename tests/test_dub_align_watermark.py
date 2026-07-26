"""动态水印测试（四角随机跳动，纯逻辑）。"""

import tempfile
import unittest
from pathlib import Path

from dub_align_studio.watermark import (
    Watermark,
    corner_sequence,
    watermark_filters,
    watermark_from_payload,
)


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="wm_"))
        self.font = self.work / "font.ttf"
        self.font.write_bytes(b"FONT")

    def test_empty_or_no_font_returns_empty(self):
        self.assertEqual(watermark_filters(Watermark(text=""), self.font, self.work, 1080, 1920, 10), [])
        self.assertEqual(watermark_filters(Watermark(text="x"), None, self.work, 1080, 1920, 10), [])

    def test_drawtext_switches_corners_over_time(self):
        fs = watermark_filters(Watermark(text="@水星", interval_seconds=3, seed=7),
                               self.font, self.work, 1080, 1920, 30)
        self.assertEqual(len(fs), 1)
        f = fs[0]
        self.assertTrue(f.startswith("drawtext="))
        self.assertIn("mod(floor(t/3.000)", f)     # 随时间切片
        self.assertIn("x='if(", f)                 # x/y 用单引号包住（逗号安全）
        self.assertIn("y='if(", f)
        self.assertIn("w-text_w-", f)              # 右角
        self.assertIn("h-text_h-", f)              # 下角

    def test_opacity_in_fontcolor(self):
        fs = watermark_filters(Watermark(text="x", opacity=0.6, color="#FFFFFF"),
                               self.font, self.work, 1080, 1920, 10)
        self.assertIn("fontcolor=0xFFFFFF@0.60", fs[0])

    def test_corner_sequence_uses_all_four_and_is_seed_stable(self):
        seq = corner_sequence(7)
        self.assertEqual(set(seq), {(1, 0), (1, 1), (0, 0), (0, 1)})   # 四角都出现
        self.assertEqual(corner_sequence(7), corner_sequence(7))       # 同 seed 稳定
        self.assertNotEqual(corner_sequence(1), corner_sequence(2))    # 不同 seed 不同

    def test_from_payload(self):
        self.assertIsNone(watermark_from_payload({"watermark": {"enabled": False}}))
        self.assertIsNone(watermark_from_payload({"watermark": {"enabled": True, "text": "  "}}))
        wm = watermark_from_payload({"watermark": {"enabled": True, "text": "@我", "opacity": 0.7,
                                                    "interval_seconds": 2, "unknown": "x"}})
        self.assertIsInstance(wm, Watermark)
        self.assertEqual(wm.text, "@我")
        self.assertEqual(wm.interval_seconds, 2)


if __name__ == "__main__":
    unittest.main()
