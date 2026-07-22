"""Dub Align Studio 文本框测试：滤镜构建纯逻辑 + ffmpeg 门控渲染。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio.overlays import (
    OverlayText,
    POSITION_PRESETS,
    normalize_color,
    overlay_filters,
    overlays_from_dicts,
    overlays_to_dicts,
)


class OverlayFilterTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="overlay_"))
        self.font = self.workdir / "font.ttf"
        self.font.write_bytes(b"\x00")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _one(self, overlay: OverlayText, total: float = 20.0) -> str:
        filters = overlay_filters([overlay], self.font, self.workdir, 1080, total)
        self.assertEqual(len(filters), 1)
        return filters[0]

    def test_custom_size_and_color(self):
        item = self._one(OverlayText(text="思情肚子", font_size_px=96, color="#FFE14D"))
        self.assertIn("fontsize=96", item)
        self.assertIn("fontcolor=0xFFE14D", item)

    def test_position_presets_map_to_ratio(self):
        top = self._one(OverlayText(text="书名", position="顶部"))
        bottom = self._one(OverlayText(text="引导", position="底部"))
        self.assertIn(f"y=(h-text_h)*{POSITION_PRESETS['顶部']:.3f}", top)
        self.assertIn(f"y=(h-text_h)*{POSITION_PRESETS['底部']:.3f}", bottom)

    def test_custom_y_ratio_overrides_preset(self):
        item = self._one(OverlayText(text="自定义", position="顶部", y_ratio=0.42))
        self.assertIn("y=(h-text_h)*0.420", item)

    def test_full_duration_has_no_enable_window(self):
        item = self._one(OverlayText(text="全程", start=0.0, end=0.0), total=18.5)
        self.assertNotIn("enable=", item)

    def test_window_generates_enable(self):
        item = self._one(OverlayText(text="限时", start=2.0, end=8.0), total=18.5)
        self.assertIn("enable='between(t,2.000,8.000)'", item)

    def test_boxed_adds_semi_transparent_background(self):
        item = self._one(OverlayText(text="下载知乎搜书名", boxed=True, box_opacity=0.5))
        self.assertIn("box=1", item)
        self.assertIn("boxcolor=black@0.50", item)

    def test_zero_border_omits_border(self):
        item = self._one(OverlayText(text="无描边", border_width=0))
        self.assertNotIn("borderw", item)

    def test_empty_text_skipped(self):
        filters = overlay_filters([OverlayText(text="  ")], self.font, self.workdir, 1080, 10.0)
        self.assertEqual(filters, [])

    def test_normalize_color(self):
        self.assertEqual(normalize_color("#FFFFFF"), "0xFFFFFF")
        self.assertEqual(normalize_color("white"), "white")
        self.assertEqual(normalize_color(""), "white")

    def test_dict_roundtrip(self):
        overlays = [OverlayText(text="书名", font_size_px=88, color="#FF5A4D", boxed=True)]
        back = overlays_from_dicts(overlays_to_dicts(overlays))
        self.assertEqual(back, overlays)
        self.assertEqual(overlays_from_dicts([{"text": "  "}, {"junk": 1}]), [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class OverlayRenderTests(unittest.TestCase):
    def test_render_with_overlays_keeps_frame_lock(self):
        from dub_align_studio.engines import MockEngine
        from dub_align_studio.render_b import RenderConfig, render_b, _run
        from dub_align_studio.subtitles import find_cjk_font
        from dub_align_studio.timing import MockAligner

        workdir = Path(tempfile.mkdtemp(prefix="overlay_render_"))
        try:
            lines = ["第一句台词。", "第二句台词。"]
            durations = [5.5, 6.0]
            master = MockEngine(durations=durations).synthesize_full("\n".join(lines), None, workdir / "m.wav")
            timings = MockAligner(durations=durations).measure(master.path, lines)
            config = RenderConfig()
            videos = []
            for i in range(2):
                clip = workdir / f"c{i}.mp4"
                _run([config.ffmpeg, "-y", "-f", "lavfi", "-i",
                      "testsrc=size=640x360:rate=30:duration=7", "-pix_fmt", "yuv420p", str(clip)], "样例")
                videos.append(clip)

            overlays = [
                OverlayText(text="思情肚子", position="顶部", font_size_px=96, color="#FFE14D"),
                OverlayText(text="下载知乎搜书名《思情肚子》看后续", position="底部",
                            font_size_px=60, color="#FFFFFF", boxed=True),
            ]
            result = render_b(master.path, timings, videos, workdir / "out.mp4", config, overlays=overlays)
            self.assertTrue(result.ok, str(result))
            if find_cjk_font():
                self.assertIn("2 个文本框", result.subtitle_note)
            else:
                self.assertIn("文本框未叠加", result.subtitle_note)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
