"""第十轮迭代测试：字幕颜色/描边规范化 / 进度字段 / 音色试听缓存 / UI 契约（进度条·字幕色·#7·#8）。"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
import wave
from pathlib import Path

from dub_align_studio import settings as studio_settings, subtitles, voice_library, web_server
from dub_align_studio.subtitles import SubtitleEntry, SubtitleStyle, drawtext_filters, normalize_color


def _wav(path: Path) -> None:
    with wave.open(str(path), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(8000)
        h.writeframes(b"\x01\x02" * 800)


class SubtitleColorTests(unittest.TestCase):
    def test_normalize_color(self):
        self.assertEqual(normalize_color("#FF5A4D"), "0xFF5A4D")
        self.assertEqual(normalize_color("white"), "white")
        self.assertEqual(normalize_color(""), "white")

    def test_drawtext_uses_normalized_color_and_border(self):
        work = Path(tempfile.mkdtemp(prefix="subcol_"))
        try:
            font = work / "f.ttf"
            font.write_bytes(b"\x00")
            style = SubtitleStyle(color="#FFE14D", border_width=6, border_color="#111111")
            f = drawtext_filters([SubtitleEntry(1, 0.0, 5.0, "台词")], style, font, work)[0]
            self.assertIn("fontcolor=0xFFE14D", f)       # #RRGGBB → 0x
            self.assertIn("borderw=6", f)                # 描边真的生效
            self.assertIn("bordercolor=0x111111", f)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class JobProgressTests(unittest.TestCase):
    def test_snapshot_has_stage_and_progress(self):
        job = web_server.JobState()
        job.set_progress("③ 渲染成片 · 第 2/3 段", 60)
        snap = job.snapshot()
        self.assertEqual(snap["stage"], "③ 渲染成片 · 第 2/3 段")
        self.assertEqual(snap["progress"], 60)

    def test_progress_clamped(self):
        job = web_server.JobState()
        job.set_progress("x", 250)
        self.assertEqual(job.snapshot()["progress"], 100)
        job.set_progress("x", -5)
        self.assertEqual(job.snapshot()["progress"], 0)


class VoiceTryCacheTests(unittest.TestCase):
    port = 8788

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="vtry_"))
        studio_settings.SETTINGS_FILE = cls.temp / "settings.json"
        studio_settings.set_data_root(cls.temp / "数据")
        cls._home_backup = web_server.STUDIO_HOME
        web_server.STUDIO_HOME = cls.temp
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # 登记一个 mock 音色
        src = cls.temp / "ref.wav"
        _wav(src)
        import urllib.parse
        q = urllib.parse.urlencode({"name": "缓存音色", "filename": "ref.wav", "engine": "mock"})
        urllib.request.urlopen(urllib.request.Request(
            cls.base + "/api/voices?" + q, data=src.read_bytes(), method="POST"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_server.STUDIO_HOME = cls._home_backup
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.temp, ignore_errors=True)

    def _voice_try(self) -> dict:
        vid = json.loads(urllib.request.urlopen(self.base + "/api/state").read())["voices"][0]["voice_id"]
        req = urllib.request.Request(self.base + "/api/run",
                                     data=json.dumps({"action": "voice_try", "voice_id": vid,
                                                      "engine": "mock", "text": "缓存测试句。"}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req)
        deadline = time.time() + 30
        while time.time() < deadline:
            job = json.loads(urllib.request.urlopen(self.base + "/api/job").read())
            if job["done"]:
                return job
            time.sleep(0.2)
        raise AssertionError("voice_try 超时")

    def test_second_try_hits_cache(self):
        j1 = self._voice_try()
        self.assertTrue(j1["ok"], j1["log"])
        self.assertTrue(j1["try_audio"])
        self.assertTrue(any("生成并缓存" in x for x in j1["log"]), j1["log"])
        j2 = self._voice_try()
        self.assertTrue(j2["ok"])
        self.assertTrue(any("命中试听缓存" in x for x in j2["log"]), j2["log"])
        self.assertEqual(j1["try_audio"], j2["try_audio"])  # 复用同一文件，未重复渲染


class UiContractV10Tests(unittest.TestCase):
    def test_index_wires_round9_features(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for m in ('id="progBar"', 'id="progStage"', "updateProgress",       # #1 进度条
                  'id="subColor"', 'id="subSwatches"', 'id="subBorder"',      # #4 字幕颜色/描边
                  "setSfxVol", "mixGains",                                    # #2/#3 音效音量·独立增益
                  "subtitlesToOverlays", "_sub",                             # #7 字幕转文本框
                  "④ 剪映导出", "最后一步"):                                  # #8 剪映位置
            self.assertIn(m, html, m)


if __name__ == "__main__":
    unittest.main()
