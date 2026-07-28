import shutil
import tempfile
import unittest
from pathlib import Path

from tools import package_lite_cloud
from tools import package_lite_cloud_installer


class LitePackagingGuardTests(unittest.TestCase):
    def test_lite_package_requires_ffmpeg_pair(self):
        target = Path(tempfile.mkdtemp())
        original_find_binary = package_lite_cloud.find_binary
        try:
            package_lite_cloud.find_binary = lambda _name: None
            with self.assertRaises(RuntimeError) as ctx:
                package_lite_cloud.copy_ffmpeg(target)
        finally:
            package_lite_cloud.find_binary = original_find_binary
            shutil.rmtree(target, ignore_errors=True)

        message = str(ctx.exception)
        self.assertIn("ffmpeg.exe", message)
        self.assertIn("ffprobe.exe", message)

    def test_installer_source_requires_root_runtime_files(self):
        src = Path(tempfile.mkdtemp())
        try:
            (src / "启动.bat").write_text("@echo off\n", encoding="utf-8")
            (src / "ffmpeg.exe").write_bytes(b"ffmpeg")

            self.assertFalse(package_lite_cloud_installer.validate_lite_source(src))

            (src / "ffprobe.exe").write_bytes(b"ffprobe")
            self.assertTrue(package_lite_cloud_installer.validate_lite_source(src))
        finally:
            shutil.rmtree(src, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
