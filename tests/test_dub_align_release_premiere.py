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
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="pp_"))
        self.segs = []
        for i in (1, 2, 3):
            f = self.work / f"{i:03d}.mp4"; f.write_bytes(b"x"); self.segs.append(f)
        self.master = self.work / "master.wav"; _wav(self.master)

    def tearDown(self):
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
        path = export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920)
        self.assertTrue(path.exists())
        self.assertTrue((self.work / "Premiere导入说明.txt").exists())
        ET.parse(path)                                              # 落盘文件同样可解析

    def test_export_is_self_contained_and_survives_cleanup(self):
        # 导出后素材复制进 Premiere工程_素材/，且工程只引用副本——删掉源分镜段/配音后仍完整
        path = export_premiere_project(self.work, self.segs, self.master, [150, 210, 180], 30, 1080, 1920)
        mat = self.work / "Premiere工程_素材"
        self.assertTrue((mat / "001.mp4").exists() and (mat / "master.wav").exists())
        from urllib.parse import unquote

        root = ET.parse(path).getroot()
        urls = [unquote(u.text) for u in root.iter("pathurl")]      # pathurl 对中文做了 %XX 编码
        self.assertTrue(urls and all("Premiere工程_素材" in u for u in urls))  # 只引用素材副本
        # 模拟清理：删掉源分镜段与配音，工程引用的副本仍在
        for s in self.segs:
            s.unlink()
        self.master.unlink()
        self.assertTrue((mat / "001.mp4").exists() and (mat / "master.wav").exists())

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


if __name__ == "__main__":
    unittest.main()
