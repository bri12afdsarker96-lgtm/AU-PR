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

    def test_track_steps_and_text(self):
        bar = ProgressBar(text="全文在哪里看？ | 完整版", position="顶部", margin_px=24)
        fs = progressbar_filters(bar, self.font, self.work, 1080, 1920, 20.0)
        self.assertTrue(fs[0].startswith("drawbox="))      # ① 底色条带
        self.assertTrue(fs[-1].startswith("drawtext="))    # ③ 文字在最上层
        steps = [f for f in fs[1:-1] if f.startswith("drawbox=")]
        self.assertGreaterEqual(len(steps), 20)            # ② N 段进度块
        self.assertTrue(all("enable='" in s for s in steps))  # 每段靠时间窗推进

    def test_fill_steps_advance_and_use_enable_timeline(self):
        # 不依赖几何表达式里的 t：用 enable 时间窗；且各段宽度递增（进度前进），末段到片尾常亮
        fs = progressbar_filters(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 12.0)
        steps = [f for f in fs if "enable='" in f]
        widths = [int(f.split("w=")[1].split(":")[0]) for f in steps]
        self.assertEqual(widths, sorted(widths))           # 宽度单调递增 = 从左往右扫
        self.assertLessEqual(widths[-1], 1080)             # 末段满宽 ≤ 画布宽
        self.assertIn("between(t,", steps[0])              # 首段时间窗
        self.assertIn("gte(t,", steps[-1])                # 末段到片尾一直满
        self.assertNotIn("min(1", "".join(fs))             # 不再用 drawbox 几何表达式 t

    def test_fill_is_full_height_over_track(self):
        # 进度块与底色条带同 y、同高（整条覆盖，非底边细线）
        fs = progressbar_filters(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 10.0)
        def geo(f):
            return f.split("y=")[1].split(":")[0], f.split("h=")[1].split(":")[0]
        self.assertEqual(geo(fs[0]), geo(fs[1]))           # 底色 vs 第一段进度块

    def test_no_text_skips_drawtext(self):
        bar = ProgressBar(text="   ")                       # 空白文字
        fs = progressbar_filters(bar, self.font, self.work, 1080, 1920, 10.0)
        self.assertFalse(any(f.startswith("drawtext=") for f in fs))  # 无文字
        self.assertTrue(all(f.startswith("drawbox=") for f in fs))    # 只有条带+进度块

    def test_font_missing_skips_text_but_keeps_bars(self):
        bar = ProgressBar(text="有字但没字体")
        fs = progressbar_filters(bar, None, self.work, 1080, 1920, 10.0)
        self.assertFalse(any(f.startswith("drawtext=") for f in fs))  # 字体缺失→降级只画条

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
        self.assertTrue(len(fs) >= 2 and all("nan" not in f.lower() for f in fs))  # 兜底不除零/不产 NaN

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
