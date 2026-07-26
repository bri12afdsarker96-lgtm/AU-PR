"""视频进度条烧录滤镜测试（纯逻辑，2026-07-26 改为 overlay 丝滑填充）。"""

import tempfile
import unittest
from pathlib import Path

from dub_align_studio.progressbar import (
    FillOverlay,
    ProgressBar,
    progressbar_from_payload,
    progressbar_layers,
)


class ProgressBarLayerTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="pb_"))
        self.font = self.work / "font.ttf"
        self.font.write_bytes(b"FONT")

    def test_three_layers_track_fill_text(self):
        bar = ProgressBar(text="全文在哪里看 | 完整版", position="顶部", margin_px=24)
        below, fill, above = progressbar_layers(bar, self.font, self.work, 1080, 1920, 20.0)
        self.assertTrue(below and below[0].startswith("drawbox="))   # ① 底色条带
        self.assertIsInstance(fill, FillOverlay)                     # ② 已播进度图层
        self.assertTrue(above and above[-1].startswith("drawtext=")) # ③ 文字在最上层

    def test_fill_is_smooth_overlay_not_stepped_drawbox(self):
        # 丝滑：进度是一个 overlay（x 随 t 连续平移），不再是多段固定宽 drawbox
        below, fill, above = progressbar_layers(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 12.0)
        src = fill.source("[pbfill]")
        ov = fill.overlay_step("[pbbase]", "[pbfill]", "[pbfilled]")
        self.assertIn("color=c=", src)                 # 半透明色块源
        self.assertIn("overlay=", ov)                  # 用 overlay 推进
        self.assertIn("*t/", ov)                       # x 随 t 连续变化 = 丝滑
        self.assertIn("12.000", ov)                    # 以总时长为分母，片尾满
        # below 里不应再有一堆 enable 时间窗的进度块（那是旧的跳格实现）
        self.assertFalse(any("enable='" in f for f in below))

    def test_fill_full_width_and_same_geometry_as_track(self):
        below, fill, above = progressbar_layers(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 10.0)
        self.assertEqual(fill.width, 1080)             # 填充宽=画布宽（片尾满宽）
        track_y = int(below[0].split("y=")[1].split(":")[0])
        track_h = int(below[0].split("h=")[1].split(":")[0])
        self.assertEqual(fill.y, track_y)              # 与底色带同 y
        self.assertEqual(fill.height, track_h)         # 与底色带同高（整条覆盖，非底边细线）

    def test_no_text_skips_drawtext(self):
        below, fill, above = progressbar_layers(ProgressBar(text="   "), self.font, self.work, 1080, 1920, 10.0)
        self.assertEqual(above, [])                    # 空白文字→无 drawtext
        self.assertIsInstance(fill, FillOverlay)       # 仍有条带+进度

    def test_font_missing_skips_text_but_keeps_bars(self):
        below, fill, above = progressbar_layers(ProgressBar(text="有字但没字体"), None, self.work, 1080, 1920, 10.0)
        self.assertEqual(above, [])                    # 字体缺失→降级只画条
        self.assertTrue(below and isinstance(fill, FillOverlay))

    def test_bottom_position_y_below_top(self):
        top = progressbar_layers(ProgressBar(text="a", position="顶部", margin_px=24),
                                 self.font, self.work, 1080, 1920, 10.0)[1]
        bottom = progressbar_layers(ProgressBar(text="a", position="底部", margin_px=24),
                                    self.font, self.work, 1080, 1920, 10.0)[1]
        self.assertEqual(top.y, 0)                     # 顶部=贴满上沿（y=0）
        self.assertGreater(bottom.y, top.y)            # 底部明显更靠下

    def test_zero_duration_does_not_crash(self):
        below, fill, above = progressbar_layers(ProgressBar(text="x"), self.font, self.work, 1080, 1920, 0.0)
        ov = fill.overlay_step("[a]", "[b]", "[c]")
        self.assertNotIn("nan", (below[0] + ov).lower())   # 兜底不除零/不产 NaN
        self.assertGreater(fill.duration, 0)

    def test_opacity_clamped(self):
        below, fill, above = progressbar_layers(
            ProgressBar(text="", track_opacity=5, fill_opacity=-1),
            self.font, self.work, 1080, 1920, 10.0)
        self.assertIn("@1.00", below[0])               # track 5 → 1.0
        self.assertEqual(fill.opacity, 0.0)            # fill -1 → 0.0
        self.assertIn("@0.00", fill.source("[x]"))     # 反映到色块源


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
