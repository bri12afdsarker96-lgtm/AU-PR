import tempfile
import unittest
from pathlib import Path

from integrated_workbench import dub_bridge as db


class DubScriptTests(unittest.TestCase):
    def test_writes_one_row_per_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = db.write_dub_script("第一句\n\n第二句\n第三句", Path(d))
            rows = path.read_text(encoding="utf-8-sig").strip().splitlines()
            # 表头 + 3 行
            self.assertEqual(len(rows), 4)
            self.assertIn("index,text,audio_file,duration_seconds", rows[0])
            self.assertIn("第一句", rows[1])


class AlignTests(unittest.TestCase):
    def test_aligns_by_order(self):
        text = "没想到大反转\n好温暖的陪伴\n真相是什么"
        plan, notes = db.build_aligned_plan(
            text,
            audio_durations=[2.0, 3.0, 2.5],
            video_durations=[5.0, 2.0, 4.0],
            genre="影视解说",
        )
        self.assertEqual(len(plan), 3)
        self.assertEqual(notes, [])
        # 句1 反转 → 冲出屏幕 + 综艺开头-咚
        self.assertEqual(plan[0].emotion, "反转高能")
        self.assertEqual(plan[0].sound_effect, "综艺开头-咚")

    def test_sync_trims_when_video_longer(self):
        plan, _ = db.build_aligned_plan(
            "普通一句", audio_durations=[2.0], video_durations=[5.0],
            genre="影视解说", audio_match_mode="裁剪多余画面",
        )
        self.assertEqual(plan[0].sync.trim_to, 2.0)  # 画面裁到配音时长

    def test_sync_speed_mode(self):
        plan, _ = db.build_aligned_plan(
            "普通一句", audio_durations=[2.0], video_durations=[4.0],
            genre="影视解说", audio_match_mode="变速匹配",
        )
        self.assertAlmostEqual(plan[0].sync.video_speed, 2.0)

    def test_mismatched_counts_note_and_min_align(self):
        plan, notes = db.build_aligned_plan(
            "句一\n句二\n句三", audio_durations=[1.0, 1.0], video_durations=[1.0],
            genre="美食",
        )
        self.assertEqual(len(plan), 1)  # 按最短对齐
        self.assertTrue(notes)

    def test_total_duration_is_audio_sum(self):
        plan, _ = db.build_aligned_plan(
            "a\nb", audio_durations=[2.0, 3.0], video_durations=[9.0, 9.0], genre="美食",
        )
        self.assertEqual(db.plan_total_duration(plan), 5.0)


if __name__ == "__main__":
    unittest.main()


class ManifestTemplateTests(unittest.TestCase):
    def test_template_has_headers_and_examples(self):
        with tempfile.TemporaryDirectory() as d:
            path = db.write_dub_manifest_template(Path(d))
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("index,text,audio_file,duration_seconds", text)
            self.assertTrue((Path(d) / "配音清单填写说明.txt").exists())

    def test_template_from_text_fills_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = db.write_dub_manifest_template(Path(d), from_text="第一句\n第二句")
            rows = path.read_text(encoding="utf-8-sig").strip().splitlines()
            self.assertEqual(len(rows), 3)  # header + 2
            self.assertIn("第一句", rows[1])

    def test_validate_good_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "配音清单.csv"
            p.write_text("index,text,audio_file,duration_seconds\n1,你好,001.wav,\n", encoding="utf-8-sig")
            self.assertEqual(db.validate_dub_manifest(p), [])

    def test_validate_missing_audio_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.csv"
            p.write_text("index,text\n1,你好\n", encoding="utf-8-sig")
            issues = db.validate_dub_manifest(p)
            self.assertTrue(any("音频" in i for i in issues))

    def test_validate_empty_audio_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "配音清单.csv"
            p.write_text("index,text,audio_file\n1,你好,\n2,再见,002.wav\n", encoding="utf-8-sig")
            issues = db.validate_dub_manifest(p)
            self.assertTrue(any("未填音频" in i for i in issues))
