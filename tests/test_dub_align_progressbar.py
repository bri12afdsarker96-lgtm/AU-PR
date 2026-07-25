"""视频进度条烧录滤镜测试（纯逻辑，2026-07-25 需求）。"""

import tempfile
import unittest
from pathlib import Path

from dub_align_studio.progressbar import (
    ProgressBar,
    progressbar_filters,
    progressbar_from_payload,
)


class ProgressBarFilterTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="pb_"))
        self.font = self.work / "font.ttf"
        self.font.write_bytes(b"FONT")

    def test_track_fill_and_text_three_filters(self):
        bar = ProgressBar(text="全文在哪里看？ | 完整版", position="顶部", margin_px=24)
        fs = progressbar_filters(bar, self.font, self.work, 1080, 1920, 20.0)
        self.assertEqual(len(fs), 3)                       # 条带 + 进度线 + 文字
        self.assertTrue(fs[0].startswith("drawbox="))      # ① 条带
        self.assertTrue(fs[1].startswith("drawbox="))      # ② 进度线
        self.assertTrue(fs[2].startswith("drawtext="))     # ③ 文字

    def test_fill_width_is_time_driven_and_comma_escaped(self):
        bar = ProgressBar(text="x")
        fill = progressbar_filters(bar, self.font, self.work, 1080, 1920, 12.5)[1]
        self.assertIn("w=iw*min(1\\,t/12.500)", fill)      # 随 t 增长、片尾满；逗号已转义
        self.assertIn("t=fill", fill)

    def test_fill_is_full_height_layer_over_track(self):
        # 进度是「半透明整条高图层从左往右扫」，不是底边细线：fill 的 y/h 必须与底色条带一致
        fs = progressbar_filters(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 10.0)
        def geo(f):  # 取 drawbox 的 y 与 h
            y = f.split("y=")[1].split(":")[0]
            h = f.split("h=")[1].split(":")[0]
            return y, h
        self.assertEqual(geo(fs[0]), geo(fs[1]))           # 底色与进度图层同 y、同高（整条覆盖）

    def test_no_text_skips_drawtext(self):
        bar = ProgressBar(text="   ")                       # 空白文字
        fs = progressbar_filters(bar, self.font, self.work, 1080, 1920, 10.0)
        self.assertEqual(len(fs), 2)                        # 只有条带 + 进度线
        self.assertFalse(any(f.startswith("drawtext=") for f in fs))

    def test_font_missing_skips_text_but_keeps_bars(self):
        bar = ProgressBar(text="有字但没字体")
        fs = progressbar_filters(bar, None, self.work, 1080, 1920, 10.0)
        self.assertEqual(len(fs), 2)                        # 字体缺失→降级只画条

    def test_bottom_position_y_below_top(self):
        top = progressbar_filters(ProgressBar(text="a", position="顶部", margin_px=24),
                                  self.font, self.work, 1080, 1920, 10.0)[0]
        bottom = progressbar_filters(ProgressBar(text="a", position="底部", margin_px=24),
                                     self.font, self.work, 1080, 1920, 10.0)[0]
        y_top = int(top.split("y=")[1].split(":")[0])
        y_bottom = int(bottom.split("y=")[1].split(":")[0])
        self.assertEqual(y_top, 24)                         # 顶部=距顶 margin
        self.assertGreater(y_bottom, y_top)                 # 底部明显更靠下

    def test_zero_duration_does_not_crash(self):
        fs = progressbar_filters(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 0.0)
        self.assertIn("t/0.100", fs[1])                     # 兜底最小时长，避免除零

    def test_opacity_clamped(self):
        bar = ProgressBar(text="", track_opacity=5, fill_opacity=-1)
        fs = progressbar_filters(bar, self.font, self.work, 1080, 1920, 10.0)
        self.assertIn("@1.00", fs[0])                       # 5 → 1.0
        self.assertIn("@0.00", fs[1])                       # -1 → 0.0


class ProgressBarPayloadTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        self.assertIsNone(progressbar_from_payload({"progressbar": {"enabled": False}}))
        self.assertIsNone(progressbar_from_payload({}))

    def test_enabled_parses_fields(self):
        bar = progressbar_from_payload({"progressbar": {
            "enabled": True, "text": "看全集", "position": "底部",
            "font_size_px": 48, "fill_color": "#FFC24B", "track_opacity": 0.5,
            "unknown_field": "ignored"}})
        self.assertIsInstance(bar, ProgressBar)
        self.assertEqual(bar.text, "看全集")
        self.assertEqual(bar.position, "底部")
        self.assertEqual(bar.font_size_px, 48)


if __name__ == "__main__":
    unittest.main()
