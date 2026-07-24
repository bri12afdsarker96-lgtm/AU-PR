"""系统 Python 桥接测试：非 frozen 时零副作用；探测逻辑不抛异常；pip 路由回退正确。"""

import sys
import unittest

from dub_align_studio import components, syspy


class SysPyBridgeTests(unittest.TestCase):
    def test_bridge_noop_when_not_frozen(self):
        # 源码运行（非 frozen）时 bridge 必须是无操作：不动 sys.path、返回空串
        before = list(sys.path)
        self.assertEqual(syspy.bridge_site_packages(), "")
        self.assertEqual(sys.path, before)

    def test_system_python_probe_never_raises(self):
        exe = syspy.system_python()   # 容器/任意环境下都不得抛异常
        if exe is not None:
            self.assertTrue(exe.is_file())
            # 找到的必须与当前进程同 major.minor（桥接的前提）
            probed = syspy._probe(exe)
            self.assertIsNotNone(probed)
            self.assertEqual(probed[0], sys.version_info[:2])

    def test_pip_routes_to_self_when_not_frozen(self):
        # 源码运行时 pip 走自身解释器（frozen 时才路由到系统 Python）
        self.assertEqual(components._py(), sys.executable)
        self.assertEqual(components._pip_base_cmd()[0], sys.executable)


if __name__ == "__main__":
    unittest.main()
