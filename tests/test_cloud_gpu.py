"""云 GPU 会话模块的纯逻辑单测：
- Credential：DPAPI 可用/不可用两条路径
- CloudConfig：save/load 往返 + 明文密码在保存时自动加密
- IdleWatchdog：空闲超阈值触发关机、mark_active 复位、pause 期间不触发
- web_server /api/cloud/* 端点
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from unittest import mock

from dub_align_studio import cloud_gpu, settings as studio_settings, web_server


class _MockClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class CredentialTests(unittest.TestCase):
    def setUp(self):
        cloud_gpu._DPAPI_AVAILABLE = None  # 强制重探

    def test_plain_fallback_when_no_dpapi(self):
        with mock.patch.object(cloud_gpu, "_dpapi_available", return_value=False):
            cipher = cloud_gpu.encrypt_str("secret42")
            self.assertTrue(cipher.startswith("plain:"))
            self.assertNotIn("secret42", cipher)  # 至少 base64 不肉眼见
            self.assertEqual(cloud_gpu.decrypt_str(cipher), "secret42")
            self.assertEqual(cloud_gpu.credential_mode(), "plain")

    def test_dpapi_roundtrip_when_available(self):
        """在没有真 win32crypt 的沙箱里模拟 DPAPI，验证 encrypt/decrypt 走对分支。"""
        fake = mock.MagicMock()
        # 把 "明文||盐" 当作"加密"结果，让 decrypt 能还原
        fake.CryptProtectData = lambda data, desc, *a, **k: b"E|" + data
        fake.CryptUnprotectData = lambda blob, *a, **k: (None, blob[2:])
        with mock.patch.object(cloud_gpu, "_dpapi_available", return_value=True), \
             mock.patch.dict("sys.modules", {"win32crypt": fake}):
            cipher = cloud_gpu.encrypt_str("云-密码-中文")
            self.assertTrue(cipher.startswith("dpapi:"))
            self.assertEqual(cloud_gpu.decrypt_str(cipher), "云-密码-中文")

    def test_dpapi_cipher_returns_empty_on_other_machine(self):
        """换到没 DPAPI 的机器上，dpapi:xxx 只能返回空串——设计如此，用户须重设。"""
        with mock.patch.object(cloud_gpu, "_dpapi_available", return_value=False):
            self.assertEqual(cloud_gpu.decrypt_str("dpapi:AAAA"), "")

    def test_empty_input_stays_empty(self):
        self.assertEqual(cloud_gpu.encrypt_str(""), "")
        self.assertEqual(cloud_gpu.decrypt_str(""), "")


class CloudConfigRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.tmp = Path(tempfile.mkdtemp(prefix="cloud_cfg_"))
        studio_settings.SETTINGS_FILE = self.tmp / "settings.json"

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_password_is_encrypted_on_save(self):
        """前端传明文 password；后端保存前必须转成 dpapi:/plain: 密文——settings.json 不留明文。"""
        with mock.patch.object(cloud_gpu, "_dpapi_available", return_value=False):
            cloud_gpu.save_cloud_config({"host": "1.2.3.4", "user": "root", "password": "hunter2"})
            raw = json.loads(studio_settings.SETTINGS_FILE.read_text(encoding="utf-8"))
            stored = raw["cloud_gpu"]["password_cipher"]
            self.assertTrue(stored.startswith("plain:"))
            self.assertNotIn("hunter2", stored)     # base64 后不肉眼见
            self.assertNotIn("hunter2", json.dumps(raw))
        # 二次 load → 密码可正常还原
        with mock.patch.object(cloud_gpu, "_dpapi_available", return_value=False):
            cfg = cloud_gpu.load_cloud_config()
            self.assertEqual(cloud_gpu.decrypt_str(cfg.password_cipher), "hunter2")

    def test_empty_password_clears(self):
        cloud_gpu.save_cloud_config({"password": "abc"})
        cloud_gpu.save_cloud_config({"password": ""})
        self.assertEqual(cloud_gpu.load_cloud_config().password_cipher, "")

    def test_bad_types_are_normalized(self):
        cloud_gpu.save_cloud_config({"port": "22a", "idle_minutes": "abc"})
        cfg = cloud_gpu.load_cloud_config()
        self.assertEqual(cfg.port, 22)                # 非法端口 → 22
        self.assertGreaterEqual(cfg.idle_minutes, 0.5)  # 非法时长 → 默认

    def test_idle_paused_until_persists(self):
        cloud_gpu.save_cloud_config({"idle_paused_until": 1234567.0})
        self.assertAlmostEqual(cloud_gpu.load_cloud_config().idle_paused_until, 1234567.0)


class IdleWatchdogTests(unittest.TestCase):
    def _make(self, config: cloud_gpu.CloudConfig, clock: _MockClock):
        fired = {"n": 0}

        def on_timeout():
            fired["n"] += 1

        wd = cloud_gpu.IdleWatchdog(lambda: config, on_timeout,
                                     tick_seconds=0.05, time_source=clock)
        return wd, fired

    def test_triggers_after_idle_minutes(self):
        clock = _MockClock()
        cfg = cloud_gpu.CloudConfig(enabled=True, auto_sleep=True, idle_minutes=5.0)
        wd, fired = self._make(cfg, clock)
        wd.mark_active()
        # 未到 5 分钟不触发
        clock.advance(4 * 60)
        self.assertFalse(wd.check_once())
        self.assertEqual(fired["n"], 0)
        # 到达 5 分钟 → 触发一次
        clock.advance(60 + 1)
        self.assertTrue(wd.check_once())
        self.assertEqual(fired["n"], 1)
        # 触发后再 check 不重复触发（等 mark_active 再重置）
        clock.advance(60)
        self.assertFalse(wd.check_once())
        self.assertEqual(fired["n"], 1)
        # 复位后重新计时
        wd.mark_active()
        clock.advance(6 * 60)
        self.assertTrue(wd.check_once())
        self.assertEqual(fired["n"], 2)

    def test_disabled_never_triggers(self):
        clock = _MockClock()
        cfg = cloud_gpu.CloudConfig(enabled=False, auto_sleep=True, idle_minutes=1.0)
        wd, fired = self._make(cfg, clock)
        wd.mark_active()
        clock.advance(3600)
        self.assertFalse(wd.check_once())
        self.assertEqual(fired["n"], 0)

    def test_auto_sleep_off_never_triggers(self):
        clock = _MockClock()
        cfg = cloud_gpu.CloudConfig(enabled=True, auto_sleep=False, idle_minutes=1.0)
        wd, fired = self._make(cfg, clock)
        clock.advance(3600)
        self.assertFalse(wd.check_once())
        self.assertEqual(fired["n"], 0)

    def test_pause_skips_until_deadline(self):
        clock = _MockClock()
        cfg = cloud_gpu.CloudConfig(enabled=True, auto_sleep=True, idle_minutes=1.0,
                                     idle_paused_until=clock() + 10 * 60)
        wd, fired = self._make(cfg, clock)
        wd.mark_active()
        clock.advance(5 * 60)  # 到 5 分钟应触发，但暂停中
        self.assertFalse(wd.check_once())
        self.assertEqual(fired["n"], 0)
        # 走过暂停截止时刻后正常触发
        clock.advance(6 * 60)
        self.assertTrue(wd.check_once())
        self.assertEqual(fired["n"], 1)


class CloudApiEndpointsTests(unittest.TestCase):
    port = 8791

    @classmethod
    def setUpClass(cls):
        cls._settings_backup = studio_settings.SETTINGS_FILE
        cls.tmp = Path(tempfile.mkdtemp(prefix="cloud_api_"))
        studio_settings.SETTINGS_FILE = cls.tmp / "settings.json"
        studio_settings.set_data_root(cls.tmp / "数据")
        cls._home_backup = web_server.STUDIO_HOME
        web_server.STUDIO_HOME = cls.tmp
        cloud_gpu.reset_manager_for_tests()
        cls.server = web_server.serve(port=cls.port, open_browser=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cloud_gpu.reset_manager_for_tests()
        web_server.STUDIO_HOME = cls._home_backup
        studio_settings.SETTINGS_FILE = cls._settings_backup
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 清 cloud_gpu 配置，避免同 class 内测试之间相互污染
        raw = studio_settings.load_settings()
        raw.pop("cloud_gpu", None)
        studio_settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        studio_settings.SETTINGS_FILE.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.base + path,
                                      data=json.dumps(body).encode(),
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    def test_status_before_save(self):
        with urllib.request.urlopen(self.base + "/api/cloud/status") as r:
            got = json.loads(r.read())
        self.assertFalse(got["enabled"])
        self.assertEqual(got["idle_minutes"], 5.0)
        self.assertIn(got["credential_mode"], ("dpapi", "plain"))

    def test_save_then_status_reflects(self):
        got = self._post("/api/cloud/save", {
            "enabled": True, "host": "gpu.compshare.com", "port": 22,
            "user": "root", "password": "S3cret!", "wake_cmd": "bash /root/start.sh",
            "health_url": "", "idle_minutes": 7,
        })
        self.assertTrue(got["enabled"])
        self.assertEqual(got["host"], "gpu.compshare.com")
        self.assertTrue(got["has_password"])
        self.assertEqual(got["idle_minutes"], 7.0)
        # settings.json 里不能有明文密码
        raw = studio_settings.SETTINGS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("S3cret!", raw)

    def test_wake_without_cmd_reports_friendly(self):
        self._post("/api/cloud/save", {"enabled": True, "host": "x", "wake_cmd": ""})
        j = self._post("/api/cloud/wake", {})
        self.assertFalse(j["ok"])
        self.assertIn("未配置", j["message"])

    def test_pause_auto_and_resume(self):
        self._post("/api/cloud/save", {"enabled": True, "host": "x", "wake_cmd": "true",
                                        "sleep_cmd": "true", "idle_minutes": 5})
        s = self._post("/api/cloud/pause_auto", {"minutes": 30})
        self.assertTrue(s["auto_paused"])
        s = self._post("/api/cloud/pause_auto", {"minutes": 0})
        self.assertFalse(s["auto_paused"])


class UiWiringTests(unittest.TestCase):
    def test_index_has_cloud_gpu_panel_and_hooks(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="cgHost"', 'id="cgUser"', 'id="cgPass"', 'id="cgWake"',
                        'id="cgSleep"', 'id="cgEnabled"', 'id="cgAuto"', 'id="cgIdleMin"',
                        "saveCloudGpu", "cloudWake", "cloudSleep", "cloudPauseAuto",
                        "loadCloudGpu", "/api/cloud/status", "/api/cloud/save",
                        "/api/cloud/wake", "/api/cloud/sleep", "/api/cloud/pause_auto"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
