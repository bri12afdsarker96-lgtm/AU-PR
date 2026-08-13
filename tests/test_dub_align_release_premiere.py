"""发行音色 + 批量导入 + Premiere XML 导出 测试（2026-07-25 需求，全离线确定性）。"""

import shutil
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

from dub_align_studio import voice_library
from dub_align_studio.premiere_xml import build_fcp7_xml, export_premiere_project


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1); h.setsampwidth(2); h.setframerate(8000)
        h.writeframes(b"\x01\x02" * 400)


class ReleasedVoiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vrel_"))
        src = self.root / "参考.wav"; _wav(src)
        self.entry = voice_library.register_voice(self.root, "定稿女声", src)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_default_draft_then_release_roundtrip(self):
        self.assertFalse(self.entry.released)                      # 新建=草稿
        voice_library.set_released(self.root, self.entry.voice_id, True)
        v = voice_library.get_voice(self.root, self.entry.voice_id)
        self.assertTrue(v.released)                                # 发行落盘
        voice_library.set_released(self.root, self.entry.voice_id, False)
        self.assertFalse(voice_library.get_voice(self.root, self.entry.voice_id).released)

    def test_release_unknown_voice_raises(self):
        with self.assertRaises(KeyError):
            voice_library.set_released(self.root, "不存在", True)


class BatchImportTests(unittest.TestCase):
    def test_folder_import_names_and_transcripts(self):
        root = Path(tempfile.mkdtemp(prefix="vb_"))
        src = Path(tempfile.mkdtemp(prefix="vbsrc_"))
        try:
            _wav(src / "书单女声.wav"); _wav(src / "沉稳男声.wav")
            (src / "书单女声.txt").write_text("这是转写", encoding="utf-8")
            (src / "无关.doc").write_text("x", encoding="utf-8")   # 非音频忽略
            names = voice_library.batch_import_folder(root, src)
            self.assertEqual(sorted(names), ["书单女声", "沉稳男声"])
            voices = {v.name: v for v in voice_library.list_voices(root)}
            self.assertEqual(voices["书单女声"].transcript, "这是转写")  # 同名txt=转写
            self.assertEqual(voices["沉稳男声"].transcript, "")
            self.assertTrue(all(not v.released for v in voices.values()))  # 批量默认草稿
        finally:
            shutil.rmtree(root, ignore_errors=True); shutil.rmtree(src, ignore_errors=True)

    def test_empty_folder_raises(self):
        empty = Path(tempfile.mkdtemp(prefix="vbe_"))
        try:
            with self.assertRaises(ValueError):
                voice_library.batch_import_folder(Path(tempfile.mkdtemp()), empty)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class PremiereXmlTests(unittest.TestCase):
    """v0.7.71 P1-1：Premiere A1 走"成片同款混音单轨"（mixdown.wav）。ffmpeg 提取
    音频用 mock；build_fcp7_xml 层测试仍能纯字符串验证结构。"""

    def setUp(self):
        from unittest import mock
        from dub_align_studio import export_mixdown

        self.work = Path(tempfile.mkdtemp(prefix="pp_"))
        self.segs = []
        for i in (1, 2, 3):
            f = self.work / f"{i:03d}.mp4"; f.write_bytes(b"x"); self.segs.append(f)
        self.master = self.work / "master.wav"; _wav(self.master)
        # 假成片文件（extract_mixdown_wav 被 mock，不真跑 ffmpeg）
        self.film = self.work / "成片.mp4"; self.film.write_bytes(b"fake film mp4")

        def fake_extract(film_mp4, out_wav, **_kw):
            # 保留真实契约：film 不存在则抛 MixdownError（不 mock 掉这条错误路径）
            if film_mp4 is None or not Path(film_mp4).is_file():
                raise export_mixdown.MixdownError(f"成片文件不存在：{film_mp4}")
            out_wav.parent.mkdir(parents=True, exist_ok=True)
            _wav(out_wav)
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
        shutil.rmtree(self.work, ignore_errors=True)

    def test_xml_parses_with_correct_structure(self):
        xml = build_fcp7_xml("测试成片", self.segs, self.master, [150, 210, 180], 30, 1080, 1920)
        root = ET.fromstring(xml)                                   # 可被标准解析器解析
        self.assertEqual(root.tag, "xmeml")
        seq = root.find("sequence")
        self.assertEqual(seq.findtext("duration"), "540")           # Σ帧 = 序列总长
        vclips = seq.findall("./media/video/track/clipitem")
        self.assertEqual(len(vclips), 3)
        self.assertEqual(vclips[1].findtext("start"), "150")        # 顺排无缝
        self.assertEqual(vclips[1].findtext("end"), "360")
        aclips = seq.findall("./media/audio/track/clipitem")
        self.assertEqual(len(aclips), 1)                            # A1 整轨配音
        self.assertEqual(aclips[0].findtext("end"), "540")
        for url in root.iter("pathurl"):                            # file URL 均已转义
            self.assertTrue(url.text.startswith("file://localhost"))

    def test_export_writes_xml_and_note(self):
        path = export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920,
                                        film_mp4=self.film)
        self.assertTrue(path.exists())
        self.assertTrue((self.work / "Premiere导入说明.txt").exists())
        ET.parse(path)                                              # 落盘文件同样可解析
        # 说明文件必须明确"成片同款混音单轨"契约文案
        note = (self.work / "Premiere导入说明.txt").read_text(encoding="utf-8")
        self.assertIn("成片同款混音单轨", note)

    def test_export_is_self_contained_and_survives_cleanup(self):
        # v0.7.71 契约：素材复制进 Premiere工程_素材/，A1=mixdown.wav（成片同款）、V1=去音副本；
        # 清理源分镜段/配音后工程引用的副本仍在
        path = export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920,
                                        film_mp4=self.film)
        mat = self.work / "Premiere工程_素材"
        self.assertTrue((mat / "001.mp4").exists() and (mat / "mixdown.wav").exists())
        self.assertFalse((mat / "master.wav").exists())     # 不再复制裸 master
        from urllib.parse import unquote

        root = ET.parse(path).getroot()
        urls = [unquote(u.text) for u in root.iter("pathurl")]      # pathurl 对中文做了 %XX 编码
        self.assertTrue(urls and all("Premiere工程_素材" in u for u in urls))  # 只引用素材副本
        # 模拟清理：删掉源分镜段与配音，工程引用的副本仍在
        for s in self.segs:
            s.unlink()
        self.master.unlink()
        self.assertTrue((mat / "001.mp4").exists() and (mat / "mixdown.wav").exists())

    def test_export_requires_film_mp4(self):
        """v0.7.71 契约：没有成片必须明确失败，不静默用裸 master 冒充成功。"""
        from dub_align_studio.export_mixdown import MixdownError
        with self.assertRaises(MixdownError):
            export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920,
                                     film_mp4=None)
        with self.assertRaises(MixdownError):
            export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920,
                                     film_mp4=self.work / "不存在.mp4")

    def test_build_fcp7_xml_audio_sample_rate_configurable(self):
        """采样率参数化后必须真的落到 XML 里，且与实际 WAV 保持一致——
        防旧版硬编码 48000 导致 XML 声明与音频文件不匹配的时长/拒收问题。"""
        for sr in (44100, 48000):
            xml = build_fcp7_xml("t", self.segs, self.master, [10, 10, 10], 30, 1080, 1920,
                                  audio_sample_rate=sr, audio_channels=1)
            self.assertIn(f"<samplerate>{sr}</samplerate>", xml)
            self.assertIn("<channelcount>1</channelcount>", xml)
            self.assertIn("<numOutputChannels>1</numOutputChannels>", xml)

    def test_import_robustness_fields_present(self):
        # Premiere 严格导入所需字段：缺了会判「格式不正确」而拒收（用户反馈）
        xml = build_fcp7_xml("测试成片", self.segs, self.master, [150, 210, 180], 30, 1080, 1920)
        root = ET.fromstring(xml)
        seq = root.find("sequence")
        self.assertIsNotNone(seq.find("timecode"))                  # 序列时间码
        self.assertIsNotNone(seq.find("./media/video/format/samplecharacteristics/pixelaspectratio"))
        v0 = seq.find("./media/video/track/clipitem")
        self.assertEqual(v0.findtext("masterclipid"), "masterclip-v1")   # 主素材 id
        self.assertEqual(v0.findtext("pproTicksIn"), "0")           # ppro ticks
        self.assertTrue(int(v0.findtext("pproTicksOut")) > 0)
        self.assertIsNotNone(v0.find("./file/media/video/samplecharacteristics/width"))
        a0 = seq.find("./media/audio/track/clipitem")
        self.assertEqual(a0.findtext("./sourcetrack/mediatype"), "audio")

    def test_mismatched_counts_raise(self):
        with self.assertRaises(ValueError):
            build_fcp7_xml("x", self.segs, self.master, [100, 100], 30, 1080, 1920)

    def test_transactional_preserves_old_project_on_segment_failure(self):
        """v0.7.71 P1-1 事务化：先跑一次成功导出，再故意让第 2 段无音副本失败——
        旧的 Premiere工程.xml + Premiere工程_素材/ 必须**字节级**完整保留，
        不允许被半成品覆盖；staging 目录也彻底清干净。"""
        from unittest import mock as _m
        from dub_align_studio import export_mixdown
        from dub_align_studio.export_mixdown import MixdownError

        # 1) 先成功导一版
        path = export_premiere_project(self.work, self.segs, self.master,
                                        [150, 210, 180], 30, 1080, 1920, film_mp4=self.film)
        mat = self.work / "Premiere工程_素材"
        old_xml_bytes = path.read_bytes()
        old_note_bytes = (self.work / "Premiere导入说明.txt").read_bytes()
        old_material_files = {p.name: p.read_bytes() for p in mat.iterdir()}

        # 2) 故意让第 2 段 make_silent_video 失败
        call_count = {"n": 0}

        def _boom_on_second(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise MixdownError("第 2 段模拟失败")
            import shutil as _sh
            dst.parent.mkdir(parents=True, exist_ok=True)
            _sh.copy2(src, dst)
            return dst

        with _m.patch.object(export_mixdown, "make_silent_video", side_effect=_boom_on_second):
            with self.assertRaises(MixdownError):
                export_premiere_project(self.work, self.segs, self.master,
                                         [150, 210, 180], 30, 1080, 1920, film_mp4=self.film)

        # 3) 旧工程字节完整保留
        self.assertEqual(path.read_bytes(), old_xml_bytes,
                          "事务化失败：旧 Premiere工程.xml 被覆盖或损坏")
        self.assertEqual((self.work / "Premiere导入说明.txt").read_bytes(), old_note_bytes)
        new_material_files = {p.name: p.read_bytes() for p in mat.iterdir()}
        self.assertEqual(new_material_files, old_material_files,
                          "事务化失败：素材目录内容被修改")
        # 无 .staging 残留
        residues = [p.name for p in self.work.iterdir() if ".staging" in p.name or ".old_" in p.name]
        self.assertEqual(residues, [], f"残留：{residues}")


if __name__ == "__main__":
    unittest.main()
