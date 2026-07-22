"""第四轮迭代测试：多任务并发下载 / fish-speech 一键安装阶段 / 试听端点 / 克隆存档 / UI 契约。"""

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from dub_align_studio import components, settings as studio_settings, web_server


def _make_wav(path: Path, frames: int = 800) -> bytes:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x01\x02" * frames)
    return path.read_bytes()


def _post_json(base: str, route: str, payload: dict):
    request = urllib.request.Request(
        base + route, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(request).read())


class FishStageTests(unittest.TestCase):
    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.temp = Path(tempfile.mkdtemp(prefix="fish_stage_"))
        studio_settings.SETTINGS_FILE = self.temp / "settings.json"
        studio_settings.set_data_root(self.temp / "数据")

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_stage_none_then_source(self):
        stage, detail = components.fish_stage()
        self.assertEqual(stage, "none")
        self.assertIn("安装", detail)
        (components.fish_source_dir()).mkdir(parents=True)
        (components.fish_source_dir() / "pyproject.toml").write_text("[project]", encoding="utf-8")
        stage, detail = components.fish_stage()
        # 依赖未装 → deps 阶段（fish_speech 包在测试环境不可导入）
        self.assertEqual(stage, "deps")
        self.assertIn("续装", detail)

    def test_statuses_expose_fish_stage(self):
        statuses = {s["key"]: s for s in components.component_statuses()}
        self.assertEqual(statuses["fish_speech"]["kind"], "install")
        self.assertIn("fish_stage", statuses["fish_speech"])

    def test_start_server_refuses_when_not_installed(self):
        with self.assertRaises(RuntimeError):
            components.start_fish_server(lambda _m: None, wait_seconds=1)

    def test_stop_server_idempotent(self):
        logs: list[str] = []
        components.stop_fish_server(logs.append)
        self.assertTrue(any("未由本软件托管" in line for line in logs))


class ConcurrentTaskTests(unittest.TestCase):
    """组件下载各占独立槽：并发不阻塞，主流程（envcheck 等）照常可跑。"""

    port = 8776

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="conc_"))
        studio_settings.SETTINGS_FILE = cls.temp / "settings.json"
        studio_settings.set_data_root(cls.temp / "数据")
        cls._home_backup = web_server.STUDIO_HOME
        web_server.STUDIO_HOME = cls.temp
        cls._install_backup = components.install_component
        cls.gates = {"a": threading.Event(), "b": threading.Event()}

        def fake_install(key, log):
            log(f"安装中：{key}")
            gate = cls.gates.get(key)
            if gate is None:
                raise KeyError(f"未知组件：{key}")
            if not gate.wait(timeout=30):
                raise RuntimeError("测试闸门超时")
            log("完成")

        components.install_component = fake_install
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for gate in cls.gates.values():
            gate.set()
        time.sleep(0.3)
        cls.server.shutdown()
        cls.server.server_close()
        components.install_component = cls._install_backup
        web_server.STUDIO_HOME = cls._home_backup
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.temp, ignore_errors=True)

    def _jobs(self) -> dict:
        return {j["id"]: j for j in json.loads(
            urllib.request.urlopen(self.base + "/api/jobs").read())["jobs"]}

    def test_parallel_components_and_main_not_blocked(self):
        self.assertTrue(_post_json(self.base, "/api/run",
                                   {"action": "component", "component_key": "a"})["ok"])
        # 同一组件重复点击 → 409；不同组件并行 → ok
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post_json(self.base, "/api/run", {"action": "component", "component_key": "a"})
        self.assertEqual(ctx.exception.code, 409)
        self.assertTrue(_post_json(self.base, "/api/run",
                                   {"action": "component", "component_key": "b"})["ok"])
        jobs = self._jobs()
        self.assertTrue(jobs["component:a"]["running"])
        self.assertTrue(jobs["component:b"]["running"])
        # 下载进行中，主流程（envcheck）不再被挡
        self.assertTrue(_post_json(self.base, "/api/run", {"action": "envcheck"})["ok"])
        deadline = time.time() + 30
        while time.time() < deadline:
            main = self._jobs()["main"]
            if main["done"]:
                break
            time.sleep(0.15)
        self.assertTrue(main["ok"], main["log"])
        # 放行两个下载并确认收尾
        self.gates["a"].set()
        self.gates["b"].set()
        deadline = time.time() + 30
        while time.time() < deadline:
            jobs = self._jobs()
            if jobs["component:a"]["done"] and jobs["component:b"]["done"]:
                break
            time.sleep(0.15)
        self.assertTrue(jobs["component:a"]["ok"], jobs["component:a"]["log"])
        self.assertTrue(jobs["component:b"]["ok"], jobs["component:b"]["log"])


class MediaEndpointTests(unittest.TestCase):
    port = 8778

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="media_"))
        studio_settings.SETTINGS_FILE = cls.temp / "settings.json"
        studio_settings.set_data_root(cls.temp / "数据")
        cls._home_backup = web_server.STUDIO_HOME
        web_server.STUDIO_HOME = cls.temp
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_server.STUDIO_HOME = cls._home_backup
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.temp, ignore_errors=True)

    def _audio_url(self, path: Path) -> str:
        return self.base + "/api/audio?path=" + urllib.parse.quote(str(path))

    def test_audio_serves_whitelisted_file_with_range(self):
        sample = studio_settings.clones_dir() / "存档.wav"
        payload = _make_wav(sample)
        with urllib.request.urlopen(self._audio_url(sample)) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "audio/wav")
            self.assertEqual(response.read(), payload)
        request = urllib.request.Request(self._audio_url(sample), headers={"Range": "bytes=0-99"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), payload[:100])

    def test_audio_rejects_outside_and_bad_suffix(self):
        outside = Path(tempfile.mkdtemp(prefix="outside_"))
        try:
            stray = outside / "别的.wav"
            _make_wav(stray)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self._audio_url(stray))
            self.assertEqual(ctx.exception.code, 403)
        finally:
            shutil.rmtree(outside, ignore_errors=True)
        secret = studio_settings.data_root() / "秘密.txt"
        secret.write_text("x", encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._audio_url(secret))
        self.assertEqual(ctx.exception.code, 403)

    def test_run_registers_output_dir_as_media_root(self):
        out = Path(tempfile.mkdtemp(prefix="out_"))
        try:
            film = out / "成片.mp4"
            film.write_bytes(b"\x00" * 128)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(self._audio_url(film))  # 注册前拒绝
            _post_json(self.base, "/api/run",
                       {"action": "probe", "output_dir": str(out)})
            deadline = time.time() + 20
            while time.time() < deadline:
                job = json.loads(urllib.request.urlopen(self.base + "/api/job").read())
                if job["done"]:
                    break
                time.sleep(0.1)
            with urllib.request.urlopen(self._audio_url(film)) as response:
                self.assertEqual(response.status, 200)  # 注册后放行
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_clones_list_and_delete(self):
        sample = studio_settings.clones_dir() / "20260722_mock_默认.wav"
        _make_wav(sample)
        sample.with_suffix(".json").write_text("{}", encoding="utf-8")
        clones = json.loads(urllib.request.urlopen(self.base + "/api/clones").read())["clones"]
        files = [c["file"] for c in clones]
        self.assertIn("20260722_mock_默认.wav", files)
        self.assertTrue(all(c["size_mb"] >= 0 for c in clones))
        request = urllib.request.Request(
            self.base + "/api/clones/" + urllib.parse.quote("20260722_mock_默认.wav"),
            method="DELETE")
        urllib.request.urlopen(request)
        self.assertFalse(sample.exists())
        self.assertFalse(sample.with_suffix(".json").exists())

    def test_voice_audition_endpoint(self):
        source = self.temp / "参考源.wav"
        payload = _make_wav(source)
        query = urllib.parse.urlencode({"name": "试听音色", "filename": "参考源.wav"})
        request = urllib.request.Request(self.base + "/api/voices?" + query,
                                         data=payload, method="POST")
        voice_id = json.loads(urllib.request.urlopen(request).read())["voice_id"]
        url = self.base + "/api/voices/" + urllib.parse.quote(voice_id) + "/audio"
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/voices/" + urllib.parse.quote("不存在") + "/audio")
        self.assertEqual(ctx.exception.code, 404)

    def test_open_folder_rejects_missing_dir(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _post_json(self.base, "/api/open_folder", {"path": str(self.temp / "不存在")})
        self.assertEqual(ctx.exception.code, 400)


class UiContractTests(unittest.TestCase):
    """界面契约：按钮背后必须有真实端点（防「样子货」回归）。"""

    def test_index_wires_new_features(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ("/api/jobs", "id=\"dlog\"", "id=\"masterAudio\"", "id=\"clList\"",
                       "fish_server", "/api/clones", "playUrl", "open_folder", "id=\"mediaRow\""):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
