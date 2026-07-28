import unittest

from integrated_workbench import edit_compose as ec


class AspectTests(unittest.TestCase):
    def test_vertical_canvas(self):
        self.assertEqual(ec.aspect_canvas("9:16 竖屏", base=1080), (1080, 1920))

    def test_horizontal_canvas(self):
        self.assertEqual(ec.aspect_canvas("16:9 横屏", base=1080), (1920, 1080))

    def test_square_canvas(self):
        self.assertEqual(ec.aspect_canvas("1:1 方形", base=1080), (1080, 1080))

    def test_original_returns_base_square(self):
        self.assertEqual(ec.aspect_canvas("原始", base=720), (720, 720))

    def test_canvas_is_even(self):
        w, h = ec.aspect_canvas("2.35:1 电影", base=1080)
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)

    def test_fit_contain_pads(self):
        f = ec.aspect_fit_filter(1080, 1920, "contain")
        self.assertIn("pad=1080:1920", f)
        self.assertIn("decrease", f)

    def test_fit_cover_crops(self):
        f = ec.aspect_fit_filter(1080, 1920, "cover")
        self.assertIn("crop=1080:1920", f)
        self.assertIn("increase", f)


class AudioSyncTests(unittest.TestCase):
    def test_trim_when_video_longer(self):
        plan = ec.match_video_to_audio(video_duration=8.0, audio_duration=5.0, mode="裁剪多余画面")
        self.assertEqual(plan.trim_to, 5.0)
        self.assertEqual(plan.video_speed, 1.0)
        self.assertEqual(plan.target_duration, 5.0)

    def test_trim_when_video_shorter_falls_back_to_slow(self):
        plan = ec.match_video_to_audio(video_duration=3.0, audio_duration=5.0, mode="裁剪多余画面")
        self.assertIsNone(plan.trim_to)
        self.assertLess(plan.video_speed, 1.0)  # 放慢补足
        self.assertAlmostEqual(plan.target_duration, 5.0)

    def test_speed_mode_matches_audio(self):
        plan = ec.match_video_to_audio(video_duration=10.0, audio_duration=5.0, mode="变速匹配")
        self.assertAlmostEqual(plan.video_speed, 2.0)  # 视频加速 2x → 5s
        self.assertIn("setpts=PTS/", plan.video_filter())

    def test_none_mode_unchanged(self):
        plan = ec.match_video_to_audio(6.0, 5.0, "不处理")
        self.assertEqual(plan.video_speed, 1.0)
        self.assertEqual(plan.video_filter(), "")

    def test_modes_constant(self):
        self.assertIn("裁剪多余画面", ec.AUDIO_MATCH_MODES)
        self.assertIn("变速匹配", ec.AUDIO_MATCH_MODES)


class AtempoTests(unittest.TestCase):
    def test_simple_within_range(self):
        self.assertEqual(ec.atempo_chain(1.5), "atempo=1.5")

    def test_high_speed_chained(self):
        chain = ec.atempo_chain(4.0)
        self.assertEqual(chain.count("atempo"), 2)  # 2.0 * 2.0

    def test_low_speed_chained(self):
        chain = ec.atempo_chain(0.25)
        self.assertGreaterEqual(chain.count("atempo"), 2)


class MultiTrackTests(unittest.TestCase):
    def test_unlimited_layers_per_kind(self):
        proj = ec.EditProject()
        for _ in range(5):
            proj.add_track("video")
        for _ in range(3):
            proj.add_track("audio")
        self.assertEqual(proj.track_count("video"), 5)
        self.assertEqual(proj.track_count("audio"), 3)

    def test_add_track_auto_names(self):
        proj = ec.EditProject()
        t1 = proj.add_track("subtitle")
        t2 = proj.add_track("subtitle")
        self.assertNotEqual(t1.name, t2.name)

    def test_serializable(self):
        proj = ec.EditProject()
        track = proj.add_track("video")
        track.clips.append(ec.Clip(source="a.mp4", duration=3.0, filter_name="电影感"))
        data = proj.to_dict()
        self.assertEqual(data["tracks"][0]["clips"][0]["filter_name"], "电影感")


if __name__ == "__main__":
    unittest.main()
