"""Dub Align Studio 浏览器版界面测试：本地 API 端点 + 任务运行（含 ffmpeg 门控全流程）。"""

import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from dub_align_studio import web_server


def _storyboard_xlsx(path: Path, lines: list[str]) -> None:
    values = [['镜头序号', '口播文稿', '时长', '图片生成提示词', '视频生成提示词']]
    values.extend([[str(i), line, '5秒', line, line] for i, line in enumerate(lines, 1)])
    rows = []
    for number, values_row in enumerate(values, 1):
        cells = ''.join(f'<c r="{col}{number}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                        for col, value in zip('ABCDE', values_row))
        rows.append(f'<row r="{number}">{cells}</row>')
    xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(rows) + '</sheetData></worksheet>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', xml)


def _wav_bytes(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


class WebServerTests(unittest.TestCase):
    port = 8770

    @classmethod
    def setUpClass(cls):
        # 隔离音色库到临时目录，避免污染 ~/.dub_align_studio
        cls._home_backup = web_server.STUDIO_HOME
        cls._temp_home = Path(tempfile.mkdtemp(prefix="web_home_"))
        web_server.STUDIO_HOME = cls._temp_home
        import dub_align_studio.web_server as ws

        ws.STUDIO_HOME = cls._temp_home
        from dub_align_studio import edit_queue as _eq
        cls._eq_backup = _eq._STORE_OVERRIDE
        _eq._STORE_OVERRIDE = cls._temp_home / "edit_queue.json"
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_server.STUDIO_HOME = cls._home_backup
        from dub_align_studio import edit_queue as _eq
        _eq._STORE_OVERRIDE = cls._eq_backup
        shutil.rmtree(cls._temp_home, ignore_errors=True)

    def _get(self, path: str) -> dict:
        return json.loads(urllib.request.urlopen(self.base + path).read())

    def _wait_job(self, timeout: float = 120.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self._get("/api/job")
            if job["done"]:
                return job
            time.sleep(0.2)
        raise AssertionError("任务超时未完成")

    def test_index_and_state(self):
        html = urllib.request.urlopen(self.base + "/").read().decode("utf-8")
        self.assertIn("配音对齐工作室", html)
        state = self._get("/api/state")
        self.assertIn("mock", state["engines"])
        self.assertIn("均分兜底", state["aligners"])
        self.assertIn("顶部", state["positions"])
        self.assertEqual(len(state["aspects"]), 5)

    def test_probe_endpoint_structured(self):
        probe = self._get("/api/probe")
        names = [c["name"] for c in probe["components"]]
        keys = [c["key"] for c in probe["components"]]
        # ffmpeg + mock + dots.tts + dots.tts 云端 + fish + edge_tts + whisper（正式版；
        # edge_tts 于 v0.7.71 加入，随之补一位）
        self.assertEqual(len(names), 7)
        self.assertIn("dots_remote", keys)  # 云配音远程引擎已注册进探针
        self.assertIn("edge_tts", keys)     # Edge TTS 免费云端预设引擎
        self.assertTrue(all(c["detail"] for c in probe["components"]))

    def test_voice_upload_and_delete_with_chinese_id(self):
        query = urllib.parse.urlencode({"name": "网页音色", "filename": "参考.wav", "transcript": "你好"})
        request = urllib.request.Request(
            self.base + "/api/voices?" + query, data=_wav_bytes(), method="POST")
        voice = json.loads(urllib.request.urlopen(request).read())
        self.assertTrue(voice["ok"])
        self.assertEqual(len(self._get("/api/state")["voices"]), 1)
        urllib.request.urlopen(urllib.request.Request(
            self.base + "/api/voices/" + urllib.parse.quote(voice["voice_id"]), method="DELETE"))
        self.assertEqual(self._get("/api/state")["voices"], [])

    def test_probe_action_and_busy_conflict(self):
        request = urllib.request.Request(
            self.base + "/api/run", data=json.dumps({"action": "probe"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        self.assertTrue(json.loads(urllib.request.urlopen(request).read())["ok"])
        job = self._wait_job()
        self.assertTrue(job["ok"])
        self.assertTrue(any("mock" in line for line in job["log"]))

    def test_unknown_action_reports_failure(self):
        request = urllib.request.Request(
            self.base + "/api/run", data=json.dumps({"action": "nope"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request)
        job = self._wait_job()
        self.assertFalse(job["ok"])
        self.assertTrue(any("失败" in line for line in job["log"]))

    def test_storyboard_parse_endpoint(self):
        workdir = Path(tempfile.mkdtemp(prefix='web_story_parse_'))
        try:
            sheet = workdir / '分镜.xlsx'
            _storyboard_xlsx(sheet, ['地球太空', '黑洞吞噬恒星'])
            request = urllib.request.Request(
                self.base + '/api/storyboard/parse?path=' + urllib.parse.quote(str(sheet)),
                data=b'', method='POST')
            parsed = json.loads(urllib.request.urlopen(request).read())
            self.assertEqual(parsed['count'], 2)
            self.assertEqual(parsed['shots'][0]['row'], 2)
            self.assertEqual(parsed['text'], '地球太空\n黑洞吞噬恒星')
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def test_storyboard_plan_and_choice_api(self):
        workdir = Path(tempfile.mkdtemp(prefix='web_story_plan_'))
        try:
            sheet = workdir / '分镜.xlsx'
            _storyboard_xlsx(sheet, ['地球太空'])
            library = workdir / '素材'
            library.mkdir()
            for name in ('BJ1_地球太空.mp4', 'BJ2_地球太空.mp4'):
                (library / name).write_bytes(b'x')
            output = workdir / '输出'
            plan = self._post('/api/storyboard/plan', {
                'storyboard_path': str(sheet), 'shots_dir': str(library),
                'output_dir': str(output)})
            self.assertEqual(plan['count'], 1)
            self.assertEqual(len(plan['shots'][0]['candidates']), 2)
            other = plan['shots'][0]['candidates'][1]['source']
            changed = self._post('/api/storyboard/choice', {
                'output_dir': str(output), 'row': 2, 'source': other,
                'source_in_seconds': 1.25})
            self.assertEqual(changed['shots'][0]['source'], other)
            self.assertEqual(changed['shots'][0]['source_in_seconds'], 1.25)
            self.assertTrue(changed['shots'][0]['locked'])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 ffmpeg/ffprobe')
    def test_storyboard_run_all_produces_film_and_review_report(self):
        workdir = Path(tempfile.mkdtemp(prefix='web_story_film_'))
        try:
            sheet = workdir / '分镜.xlsx'
            lines = ['地球太空', '黑洞吞噬恒星']
            _storyboard_xlsx(sheet, lines)
            library = workdir / '素材库'
            library.mkdir()
            from dub_align_studio.render_b import RenderConfig, _run
            config = RenderConfig(width=320, height=180)
            for name in ('BJ1_地球太空.mp4', 'BJ2_黑洞吞噬恒星.mp4'):
                _run([config.ffmpeg, '-y', '-f', 'lavfi', '-i',
                      'testsrc=size=320x180:rate=30:duration=6',
                      '-pix_fmt', 'yuv420p', str(library / name)], '样例')
            output = workdir / '成片输出'
            prepared = self._post('/api/storyboard/plan', {
                'storyboard_path': str(sheet), 'shots_dir': str(library),
                'output_dir': str(output)})
            self._post('/api/storyboard/choice', {
                'output_dir': str(output), 'row': 2,
                'source': prepared['shots'][0]['source'], 'source_in_seconds': 1.0})
            payload = {'action': 'run_all', 'text': '\n'.join(lines), 'engine': 'mock',
                       'aligner': '均分兜底', 'shots_dir': str(library), 'output_dir': str(output),
                       'storyboard_path': str(sheet), 'material_mode': 'storyboard',
                       'aspect': '16:9 横屏', 'burn_subtitles': False}
            with patch('dub_align_studio.studio_pipeline.make_render_config', return_value=config):
                self.assertTrue(self._post('/api/run', payload)['ok'])
                job = self._wait_job(timeout=180)
            self.assertTrue(job['ok'], '\n'.join(job['log']))
            self.assertTrue((output / '成片.mp4').is_file())
            self.assertTrue((output / '剪辑方案.json').is_file())
            snapshot = json.loads((output / '剪辑方案_执行快照.json').read_text(encoding='utf-8'))
            self.assertEqual(snapshot['shots'][0]['source_in_seconds'], 1.0)
            self.assertTrue((output / '选片复核清单.csv').is_file())
            # The existing separate "render" button must also honor the
            # storyboard selection and source in-point after the first film.
            with patch('dub_align_studio.studio_pipeline.make_render_config', return_value=config):
                self.assertTrue(self._post('/api/run', {**payload, 'action': 'render'})['ok'])
                rerender = self._wait_job(timeout=180)
            self.assertTrue(rerender['ok'], '\n'.join(rerender['log']))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(request).read())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
    def test_generation_queue_runs_and_enters_edit_queue(self):
        workdir = Path(tempfile.mkdtemp(prefix="web_queue_"))
        try:
            shots = workdir / "分镜"
            shots.mkdir()
            from dub_align_studio.render_b import RenderConfig, _run
            config = RenderConfig()
            for i in (1, 2):
                _run([config.ffmpeg, "-y", "-f", "lavfi", "-i",
                      "testsrc=size=640x360:rate=30:duration=6",
                      "-pix_fmt", "yuv420p", str(shots / f"{i}.mp4")], "样例")
            out = workdir / "输出Q"
            payload = {"text": "第一句台词。\n第二句台词。", "engine": "mock", "aligner": "均分兜底",
                       "shots_dir": str(shots), "output_dir": str(out),
                       "burn_subtitles": True, "subtitle_size": 56, "title": "队列集01"}
            # ⑨ 加入生成队列
            resp = self._post("/api/queue", {"action": "add", **payload})
            self.assertTrue(resp["ok"])
            task_id = resp["id"]
            # 等队列任务跑完
            deadline = time.time() + 180
            task = None
            while time.time() < deadline:
                tasks = self._get("/api/queue")["tasks"]
                task = next((t for t in tasks if t["id"] == task_id), None)
                if task and task["status"] in ("done", "failed"):
                    break
                time.sleep(0.3)
            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "done", "\n".join(task["log"]) if task else "")
            self.assertTrue((out / "成片.mp4").exists())
            # ⑧ 成片自动进待编辑队列
            items = self._get("/api/edit_queue")["items"]
            self.assertTrue(any(it["title"] == "队列集01" for it in items))
            # 清理已完成队列项
            self.assertTrue(self._post("/api/queue", {"action": "clear"})["ok"])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg/ffprobe")
    def test_run_all_via_api(self):
        workdir = Path(tempfile.mkdtemp(prefix="web_run_"))
        try:
            shots = workdir / "分镜"
            shots.mkdir()
            from dub_align_studio.render_b import RenderConfig, _run

            config = RenderConfig()
            for i in (1, 2):
                _run([config.ffmpeg, "-y", "-f", "lavfi", "-i",
                      "testsrc=size=640x360:rate=30:duration=6",
                      "-pix_fmt", "yuv420p", str(shots / f"{i}.mp4")], "样例")
            payload = {
                "action": "run_all", "text": "第一句台词。\n第二句台词。",
                "engine": "mock", "aligner": "均分兜底",
                "shots_dir": str(shots), "output_dir": str(workdir / "输出"),
                "burn_subtitles": True, "subtitle_size": 56,
                "overlays": [{"text": "网页文本框", "position": "顶部", "font_size_px": 80}],
            }
            request = urllib.request.Request(
                self.base + "/api/run", data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(request)
            job = self._wait_job(timeout=180)
            self.assertTrue(job["ok"], "\n".join(job["log"]))
            self.assertTrue((workdir / "输出" / "成片.mp4").exists())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
