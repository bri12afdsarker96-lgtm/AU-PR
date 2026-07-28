"""第九轮迭代测试：pip 国内镜像加速 + 安装子进程可「停止」掐断。"""

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from dub_align_studio import components, settings as studio_settings, web_server


class PipMirrorTests(unittest.TestCase):
    def test_base_cmd_uses_china_mirror(self):
        cmd = components._pip_base_cmd()
        self.assertIn("-i", cmd)
        self.assertIn(components.PIP_INDEX, cmd)
        self.assertIn("pypi.tuna.tsinghua.edu.cn", components.PIP_INDEX)
        # 阿里/中科大/官方作为兜底 extra-index
        joined = " ".join(cmd)
        self.assertIn("mirrors.aliyun.com", joined)
        self.assertIn("--timeout", cmd)  # 有超时，避免无限期挂起

    def test_stop_when_nothing_running_is_graceful(self):
        logs: list[str] = []
        components.stop_component("dots_tts", logs.append)  # 不应抛异常
        self.assertTrue(logs and "没有正在运行" in logs[0])


class ProcessCancelTests(unittest.TestCase):
    def test_stream_command_can_be_stopped(self):
        logs: list[str] = []
        err: list[Exception] = []

        def run():
            try:
                # 模拟一个长时间安装：睡 30s，应被 stop_component 掐断
                components._stream_command(
                    [sys.executable, "-c", "import time;time.sleep(30)"],
                    logs.append, error="被测长任务", key="dots_tts")
            except Exception as exc:  # 掐断后子进程非零退出 → RuntimeError
                err.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # 等子进程登记
        deadline = time.time() + 5
        while time.time() < deadline:
            with components._ACTIVE_LOCK:
                if "dots_tts" in components._ACTIVE_PROCS:
                    break
            time.sleep(0.05)
        self.assertIn("dots_tts", components._ACTIVE_PROCS, "子进程应已登记，可被停止")

        components.stop_component("dots_tts", logs.append)
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "停止后线程应结束")
        self.assertTrue(err, "被掐断的安装应抛出错误")
        with components._ACTIVE_LOCK:
            self.assertNotIn("dots_tts", components._ACTIVE_PROCS, "结束后应从登记表移除")


class StopEndpointTests(unittest.TestCase):
    port = 8786

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.temp = Path(tempfile.mkdtemp(prefix="stop_"))
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

    def test_component_stop_endpoint_inline_ok(self):
        req = urllib.request.Request(
            self.base + "/api/run",
            data=json.dumps({"action": "component_stop", "component_key": "pycapcut"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        out = json.loads(urllib.request.urlopen(req).read())
        self.assertTrue(out["ok"])
        self.assertTrue(any("没有正在运行" in line for line in out.get("log", [])))


class UiStopContractTests(unittest.TestCase):
    def test_index_wires_stop_button(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("stopComponent", html)
        self.assertIn("component_stop", html)
        self.assertIn("■ 停止", html)


if __name__ == "__main__":
    unittest.main()
