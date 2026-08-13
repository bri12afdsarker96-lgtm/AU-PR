"""Dub Align Studio 剪辑层测试：字幕自定义大小 / SRT / 剪映草稿导出（纯逻辑 + ffmpeg 门控烧录）。"""

import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from dub_align_studio import subtitles
from dub_align_studio.capcut_draft import draft_font_size, export_capcut_package
from dub_align_studio.subtitles import SubtitleEntry, SubtitleStyle
from dub_align_studio.timing import LineTiming


class SubtitleWindowTests(unittest.TestCase):
    def test_entries_follow_frame_windows_exactly(self):
        entries = subtitles.entries_from_frame_windows(["一", "二", "三"], [180, 225, 150], 30)
        self.assertAlmostEqual(entries[0].start, 0.0)
        self.assertAlmostEqual(entries[0].end, 6.0)
        self.assertAlmostEqual(entries[1].end, 13.5)
        self.assertAlmostEqual(entries[2].end, 18.5)

    def test_mismatch_raises(self):
        with self.assertRaises(ValueError):
            subtitles.entries_from_frame_windows(["一"], [30, 30], 30)


class DrawtextStyleTests(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="subtitle_style_"))
        self.font = self.workdir / "fake_font.ttf"
        self.font.write_bytes(b"\x00")

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_custom_font_size_lands_in_filter(self):
        entries = [SubtitleEntry(1, 0.0, 6.0, "第一句台词")]
        for size in (48, 64, 96):
            filters = subtitles.drawtext_filters(entries, SubtitleStyle(font_size_px=size), self.font, self.workdir)
            self.assertEqual(len(filters), 1)
            self.assertIn(f"fontsize={size}", filters[0])
            self.assertIn("enable='between(t,0.000,6.000)'", filters[0])

    def test_empty_text_lines_skipped(self):
        entries = [SubtitleEntry(1, 0.0, 5.0, ""), SubtitleEntry(2, 5.0, 10.0, "有词")]
        filters = subtitles.drawtext_filters(entries, SubtitleStyle(), self.font, self.workdir)
        self.assertEqual(len(filters), 1)
        self.assertIn("subtitle_002", filters[0])

    def test_subtitle_never_wraps_single_line(self):
        # 单条字幕永不折行：即便超过样式每行上限，textfile 也必须是一行（长度只靠标点分句控制）
        long_phrase = "假设有个叫沈砚的年轻人他十八岁那年第一次进城"  # 22 字，远超 max_chars_per_line
        style = SubtitleStyle(font_size_px=64, max_chars_per_line=10, max_lines=2)
        subtitles.drawtext_filters([SubtitleEntry(1, 0.0, 6.0, long_phrase)], style, self.font, self.workdir)
        content = (self.workdir / "subtitle_001.txt").read_text(encoding="utf-8")
        self.assertNotIn("\n", content)          # 无换行 = 单行
        self.assertEqual(content, long_phrase)   # 原句不截断、不折

    def test_wrap_respects_style_limits(self):
        style = SubtitleStyle(max_chars_per_line=4, max_lines=2)
        wrapped = subtitles.wrap_subtitle_text("一二三四五六七八九十", style)
        lines = wrapped.split("\n")
        self.assertLessEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 4 for line in lines))
        self.assertIn("…", wrapped)

    def test_wrap_adapts_to_canvas_width(self):
        # 64px 字号 + 1080 宽画布 → 每行最多 15 字（1080*0.92//64），不得溢出画布。
        style = SubtitleStyle(font_size_px=64, max_chars_per_line=18)
        self.assertEqual(subtitles.effective_chars_per_line(style, 1080), 15)
        wrapped = subtitles.wrap_subtitle_text("第二句情绪推进画面素材偏短需要放慢补足了", style, 1080)
        for line in wrapped.split("\n"):
            self.assertLessEqual(len(line) * 64, 1080 * 0.92 + 64)
        # 不给画布宽时保持样式上限（向后兼容）
        self.assertEqual(subtitles.effective_chars_per_line(style, None), 18)
        # 大字号进一步压缩每行字数
        big = SubtitleStyle(font_size_px=96, max_chars_per_line=18)
        self.assertEqual(subtitles.effective_chars_per_line(big, 1080), 10)

    def test_find_font_none_when_no_candidates_exist(self):
        self.assertIsNone(subtitles.find_cjk_font([Path("/不存在/字体.ttc")]))
        self.assertEqual(subtitles.find_cjk_font([self.font]), self.font)


class SrtTests(unittest.TestCase):
    def test_timestamp_format(self):
        self.assertEqual(subtitles.srt_timestamp(0.0), "00:00:00,000")
        self.assertEqual(subtitles.srt_timestamp(6.0), "00:00:06,000")
        self.assertEqual(subtitles.srt_timestamp(3661.25), "01:01:01,250")

    def test_write_srt_skips_empty_and_numbers_sequentially(self):
        workdir = Path(tempfile.mkdtemp(prefix="srt_"))
        try:
            entries = [
                SubtitleEntry(1, 0.0, 6.0, "第一句"),
                SubtitleEntry(2, 6.0, 13.5, ""),
                SubtitleEntry(3, 13.5, 18.5, "第三句"),
            ]
            path = subtitles.write_srt(workdir / "out.srt", entries)
            content = path.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:06,000", content)
            self.assertIn("00:00:13,500 --> 00:00:18,500", content)
            self.assertNotIn("00:00:06,000 --> 00:00:13,500", content)  # 空行跳过
            self.assertIn("\n2\n00:00:13", content)  # 序号连续
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


class CapcutPackageTests(unittest.TestCase):
    """v0.7.71 P1-1：剪映草稿的 A1 走"成片同款混音单轨"。ffmpeg 提取音频用 mock，
    测试仍完整验证契约：materials 里是 mixdown.wav、视频段是"去音轨副本"、
    没成片时抛错。"""

    def setUp(self):
        from unittest import mock
        from dub_align_studio import export_mixdown

        self.workdir = Path(tempfile.mkdtemp(prefix="capcut_pkg_"))
        self.segments = []
        for i in range(1, 3):
            seg = self.workdir / f"{i:03d}.mp4"
            seg.write_bytes(b"fake video " + bytes([i]))
            self.segments.append(seg)
        self.master = self.workdir / "master.wav"
        with wave.open(str(self.master), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000)
        # 假的"成片"文件（内容任意；extract_mixdown_wav 被 mock 不会真跑 ffmpeg）
        self.film = self.workdir / "成片.mp4"
        self.film.write_bytes(b"fake film mp4")
        self.timings = [LineTiming(1, "第一句", 6.0), LineTiming(2, "第二句", 5.5)]

        # Mock 两个 ffmpeg 依赖函数：extract_mixdown_wav 写一个占位 WAV 头；
        # make_silent_video 拷贝源视频（等价于"去音"，但测试断言只看文件存在）。
        def fake_extract(film_mp4, out_wav, **_kw):
            # 保留真实契约：film 不存在则抛 MixdownError（不 mock 掉这条错误路径）
            if film_mp4 is None or not Path(film_mp4).is_file():
                raise export_mixdown.MixdownError(f"成片文件不存在：{film_mp4}")
            out_wav.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out_wav), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(b"\x00\x00" * (48000 * 2))  # 1 秒 stereo
            return out_wav

        def fake_silent_video(src, dst):
            import shutil as _sh
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(src, dst)
            return dst

        self._patches = [
            mock.patch.object(export_mixdown, "extract_mixdown_wav", side_effect=fake_extract),
            mock.patch.object(export_mixdown, "make_silent_video", side_effect=fake_silent_video),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in getattr(self, "_patches", []):
            p.stop()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_package_contains_all_deliverables(self):
        package = export_capcut_package(
            self.timings, self.segments, self.master, self.workdir / "out",
            style=SubtitleStyle(font_size_px=96),
            film_mp4=self.film,
        )
        self.assertTrue(package.timeline_csv.exists())
        self.assertTrue(package.script_py.exists())
        self.assertTrue(package.srt_path and package.srt_path.exists())
        self.assertTrue((package.material_dir / "001.mp4").exists())
        # v0.7.71 契约：A1 是 mixdown.wav（成片同款混音）；不再是裸 master.wav
        self.assertTrue((package.material_dir / "mixdown.wav").exists())
        self.assertFalse((package.material_dir / "master.wav").exists())
        csv_content = package.timeline_csv.read_text(encoding="utf-8-sig")
        self.assertIn("6.000", csv_content)
        self.assertIn("11.500", csv_content)  # end 累加
        script = package.script_py.read_text(encoding="utf-8")
        self.assertIn(f"FONT_SIZE = {draft_font_size(SubtitleStyle(font_size_px=96))}", script)
        self.assertIn("TrackType.audio", script)  # 整轨配音进草稿
        manifest = json.loads((package.package_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["font_size_px"], 96)
        self.assertAlmostEqual(manifest["total_seconds"], 11.5)
        # 使用说明明确契约措辞
        note = (package.package_dir / "使用说明.txt").read_text(encoding="utf-8")
        self.assertIn("成片同款混音单轨", note)

    def test_no_pycapcut_degrades_to_package_only(self):
        package = export_capcut_package(self.timings, self.segments, self.master,
                                          self.workdir / "out2", film_mp4=self.film)
        self.assertIsNone(package.real_draft_dir)  # 本容器无 pyCapCut
        self.assertIn("交接包", package.message)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            export_capcut_package(self.timings, self.segments[:1], self.master,
                                    self.workdir / "x", film_mp4=self.film)
        # v0.7.71 契约：没有成片必须明确失败，不再退化到裸 master 冒充成功
        from dub_align_studio.export_mixdown import MixdownError
        with self.assertRaises(MixdownError):
            export_capcut_package(self.timings, self.segments, self.master,
                                    self.workdir / "x", film_mp4=None)
        with self.assertRaises(MixdownError):
            export_capcut_package(self.timings, self.segments, self.master,
                                    self.workdir / "x2",
                                    film_mp4=self.workdir / "不存在.mp4")

    def test_draft_font_size_scaling(self):
        self.assertAlmostEqual(draft_font_size(SubtitleStyle(font_size_px=108)), 15.0)
        self.assertGreater(draft_font_size(SubtitleStyle(font_size_px=96)), draft_font_size(SubtitleStyle(font_size_px=48)))

    def test_transactional_no_half_package_on_segment_failure(self):
        """v0.7.71 P1-1 事务化：第 N 段无音副本失败时，不留任何『剪映草稿包_*/』半成品目录，
        staging 目录也彻底清干净——用户看到的应该是"整体失败"而非"只走了一半的草稿包"。"""
        from unittest import mock as _m
        from dub_align_studio import export_mixdown
        # 覆写 make_silent_video：第 2 段失败
        call_count = {"n": 0}

        def _boom_on_second(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise export_mixdown.MixdownError("第 2 段模拟失败")
            import shutil as _sh
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(src, dst)
            return dst

        with _m.patch.object(export_mixdown, "make_silent_video", side_effect=_boom_on_second):
            with self.assertRaises(export_mixdown.MixdownError):
                export_capcut_package(self.timings, self.segments, self.master,
                                       self.workdir / "out_fail",
                                       film_mp4=self.film)
        # out_fail 目录里不应有任何『剪映草稿包_*』**或** 半成品 staging 目录
        out_fail = self.workdir / "out_fail"
        if out_fail.is_dir():
            leftovers = [p.name for p in out_fail.iterdir()]
            self.assertFalse(
                any(name.startswith("剪映草稿包_") for name in leftovers),
                f"事务化失败：留下了半成品目录 {leftovers}",
            )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
class BurnSubtitleRenderTests(unittest.TestCase):
    """端到端：带字幕样式渲染 → 收口保持 + SRT 落盘 +（有字体时）烧录成功。"""

    def test_render_with_subtitles_keeps_frame_lock(self):
        from dub_align_studio.engines import MockEngine
        from dub_align_studio.render_b import RenderConfig, render_b, _run
        from dub_align_studio.timing import MockAligner

        workdir = Path(tempfile.mkdtemp(prefix="burn_sub_"))
        try:
            lines = ["第一句台词。", "第二句台词。"]
            durations = [5.5, 6.0]
            master = MockEngine(durations=durations).synthesize_full("\n".join(lines), None, workdir / "master.wav")
            timings = MockAligner(durations=durations).measure(master.path, lines)
            config = RenderConfig()
            videos = []
            for i in range(2):
                clip = workdir / f"clip_{i}.mp4"
                _run([config.ffmpeg, "-y", "-f", "lavfi", "-i",
                      "testsrc=size=640x360:rate=30:duration=7", "-pix_fmt", "yuv420p", str(clip)], "样例画面")
                videos.append(clip)

            result = render_b(master.path, timings, videos, workdir / "out.mp4", config,
                              subtitle_style=SubtitleStyle(font_size_px=72))
            self.assertTrue(result.ok, f"收口断言未通过：{result}")
            self.assertTrue(result.srt_path and result.srt_path.exists())
            if subtitles.find_cjk_font():
                self.assertTrue(result.subtitles_burned, result.subtitle_note)
            else:
                self.assertIn("字体缺失", result.subtitle_note)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class ManualBreakTests(unittest.TestCase):
    """手动换行=分行位置（2026-07-24）：烧录折行必须尊重用户回车。"""

    def setUp(self):
        from dub_align_studio.subtitles import wrap_subtitle_text
        global wrap_subtitle_text_fn
        wrap_subtitle_text_fn = wrap_subtitle_text

    def test_manual_breaks_preserved(self):
        style = SubtitleStyle(max_chars_per_line=10, max_lines=3)
        self.assertEqual(wrap_subtitle_text_fn("命不好的人\n真能翻盘吗", style),
                         "命不好的人\n真能翻盘吗")

    def test_manual_line_overflow_rewraps_that_line(self):
        style = SubtitleStyle(max_chars_per_line=5, max_lines=3)
        out = wrap_subtitle_text_fn("短行\n这一行超过五个字了", style)
        self.assertEqual(out.split("\n")[0], "短行")
        self.assertTrue(all(len(l) <= 5 for l in out.split("\n")))

    def test_manual_lines_capped_with_ellipsis(self):
        style = SubtitleStyle(max_chars_per_line=10, max_lines=2)
        out = wrap_subtitle_text_fn("一\n二\n三", style)
        self.assertEqual(len(out.split("\n")), 2)
        self.assertTrue(out.endswith("…"))


class PhraseTimelineTests(unittest.TestCase):
    """标点逐句字幕（2026-07-25 定案）：切句/按字数排时/无缝衔接/行尾对齐。"""

    def setUp(self):
        from dub_align_studio.subtitles import SubtitleEntry, entries_to_phrases, split_line_phrases
        self.E, self.expand, self.split = SubtitleEntry, entries_to_phrases, split_line_phrases

    def test_user_example_split(self):
        line = "命不好的人，真能翻盘吗？听完这个逆风局，你就明白了。假设有个叫沈砚的年轻人。"
        self.assertEqual(self.split(line),
                         ["命不好的人", "真能翻盘吗", "听完这个逆风局", "你就明白了", "假设有个叫沈砚的年轻人"])

    def test_no_punct_line_is_single_phrase(self):
        self.assertEqual(self.split("没有标点的一行"), ["没有标点的一行"])

    def test_windows_seamless_and_end_aligned(self):
        es = self.expand([self.E(index=1, start=2.0, end=8.0, text="第一句，第二句。第三句！")])
        self.assertEqual(es[0].start, 2.0)
        self.assertEqual(es[-1].end, 8.0)                     # 末句对齐行尾（不破坏行边界）
        for a, b in zip(es, es[1:]):
            self.assertEqual(a.end, b.start)                  # 无缝衔接
        self.assertTrue(all(e.end > e.start for e in es))

    def test_proportional_by_chars(self):
        es = self.expand([self.E(index=1, start=0.0, end=9.0, text="三个字，六个字六个字。")])
        self.assertLess(es[0].end - es[0].start, es[1].end - es[1].start)  # 短句时长更短

    def test_multi_lines_counter_and_boundaries(self):
        es = self.expand([self.E(index=1, start=0.0, end=4.0, text="甲句，乙句"),
                          self.E(index=2, start=4.0, end=9.0, text="丙句。丁句")])
        self.assertEqual([e.index for e in es], [1, 2, 3, 4])
        self.assertEqual(es[1].end, 4.0)                      # 行边界不被跨越
        self.assertEqual(es[2].start, 4.0)

    def test_min_duration_floor_holds_for_all_phrases(self):
        # 回归（2026-07-25 自检）：两长句+四短句，span 恰够每句 0.4s 底线。
        # 旧版只从最长一句扣抬底溢出，扣不完导致后续短句被夹到 0.1s（甚至末句零时长）。
        from dub_align_studio.subtitles import PHRASE_MIN_SECONDS
        line = "，".join(["甲" * 10, "乙" * 10, "丙", "丁", "戊", "己"])  # 字数 [10,10,1,1,1,1]
        es = self.expand([self.E(index=1, start=0.0, end=2.4, text=line)])
        self.assertEqual(len(es), 6)
        for e in es:
            self.assertGreaterEqual(round(e.end - e.start, 6), PHRASE_MIN_SECONDS - 1e-6)  # 每句都 ≥ 底线
        self.assertEqual(es[-1].end, 2.4)                     # 末句仍对齐行尾
        for a, b in zip(es, es[1:]):
            self.assertAlmostEqual(a.end, b.start)            # 仍无缝衔接
