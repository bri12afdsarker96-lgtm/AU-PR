# -*- coding: utf-8 -*-
"""build_dist.py 单元测试：ffmpeg 内嵌 + 敏感清扫 + 启动脚本。

覆盖：
    1. _find_ffmpeg_binary：项目自带优先于 PATH
    2. _find_ffmpeg_binary：都找不到 → None（不 raise）
    3. bundle_ffmpeg：找到就拷贝，报告数量
    4. bundle_ffmpeg：完全找不到 → 返回 0
    5. scrub_sensitive：*.py / license.json / __pycache__ 会被清掉
    6. scrub_sensitive：合法资源（*.exe / web/*.html / fonts/*.ttf）保留
    7. write_launcher_bat：内容含两个 gate 环境变量
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_dist  # noqa: E402


# --------------------------------------------------------------------------
# _find_ffmpeg_binary
# --------------------------------------------------------------------------


def test_1_find_prefers_project_tools_dir(tmp_path, monkeypatch):
    """项目内 ./tools/ffmpeg/ 优先于 PATH。"""
    monkeypatch.setattr(build_dist, "HERE", tmp_path)
    tools = tmp_path / "tools" / "ffmpeg"
    tools.mkdir(parents=True)
    stem = "ffmpeg"
    is_win = build_dist.platform.system() == "Windows"
    fname = f"{stem}.exe" if is_win else stem
    fake = tools / fname
    fake.write_bytes(b"fake-ffmpeg")

    # 就算 PATH 里也有 ffmpeg，也应该走项目自带
    result = build_dist._find_ffmpeg_binary("ffmpeg")
    assert result is not None
    assert result == fake


def test_1b_find_falls_back_to_tools_bin_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr(build_dist, "HERE", tmp_path)
    tools_bin = tmp_path / "tools" / "ffmpeg" / "bin"
    tools_bin.mkdir(parents=True)
    is_win = build_dist.platform.system() == "Windows"
    fname = "ffprobe.exe" if is_win else "ffprobe"
    fake = tools_bin / fname
    fake.write_bytes(b"fake")
    result = build_dist._find_ffmpeg_binary("ffprobe")
    assert result == fake


def test_2_find_none_when_missing_everywhere(tmp_path, monkeypatch):
    """项目内没有、PATH 里也没有 → 返回 None，不 raise。"""
    monkeypatch.setattr(build_dist, "HERE", tmp_path)
    # 清空 PATH，确保 shutil.which 找不到
    monkeypatch.setenv("PATH", "")
    result = build_dist._find_ffmpeg_binary("no-such-tool-xyz-123")
    assert result is None


# --------------------------------------------------------------------------
# bundle_ffmpeg
# --------------------------------------------------------------------------


def test_3_bundle_copies_both_when_found(tmp_path, monkeypatch):
    """两个都找到 → 返回 2，产物目录里有两份。"""
    monkeypatch.setattr(build_dist, "HERE", tmp_path)
    tools = tmp_path / "tools" / "ffmpeg"
    tools.mkdir(parents=True)
    is_win = build_dist.platform.system() == "Windows"
    for stem in ("ffmpeg", "ffprobe"):
        fname = f"{stem}.exe" if is_win else stem
        (tools / fname).write_bytes(b"fake-" + stem.encode())

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    n = build_dist.bundle_ffmpeg(dist_dir)
    assert n == 2
    # 两个产物都在
    for stem in ("ffmpeg", "ffprobe"):
        fname = f"{stem}.exe" if is_win else stem
        assert (dist_dir / fname).exists()


def test_4_bundle_returns_zero_when_none(tmp_path, monkeypatch):
    """都找不到 → 返回 0，不 raise。"""
    monkeypatch.setattr(build_dist, "HERE", tmp_path)
    monkeypatch.setenv("PATH", "")
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    n = build_dist.bundle_ffmpeg(dist_dir)
    assert n == 0


# --------------------------------------------------------------------------
# scrub_sensitive
# --------------------------------------------------------------------------


def test_5_scrub_removes_sensitive(tmp_path):
    root = tmp_path
    # 敏感文件
    (root / "settings.json").write_text("{}")
    (root / "license.json").write_text("{}")
    (root / "gpu_state.json").write_text("{}")
    (root / "queue.sqlite3").write_bytes(b"")
    (root / "README.md").write_text("# doc")
    (root / "PROTECTION.md").write_text("hack info")
    (root / "some.py").write_text("code")
    (root / "some.pyc").write_bytes(b"\x00")
    (root / "conftest.py").write_text("")
    (root / "requirements.txt").write_text("")
    pcache = root / "sub" / "__pycache__"
    pcache.mkdir(parents=True)
    (pcache / "x.pyc").write_bytes(b"\x00")

    n = build_dist.scrub_sensitive(root)
    assert n >= 8

    for name in (
        "settings.json", "license.json", "gpu_state.json",
        "queue.sqlite3", "README.md", "PROTECTION.md",
        "some.py", "some.pyc", "conftest.py", "requirements.txt",
    ):
        assert not (root / name).exists(), f"{name} 应被清掉"
    assert not pcache.exists()


def test_6_scrub_keeps_legit_resources(tmp_path):
    root = tmp_path
    # 合法资源
    (root / "app.exe").write_bytes(b"\x00")
    (root / "ffmpeg.exe").write_bytes(b"\x00")
    web = root / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>")
    (web / "app.js").write_text("var x=1;")
    fonts = root / "fonts"
    fonts.mkdir()
    (fonts / "src.ttf").write_bytes(b"\x00")
    (root / "integrity.hash").write_text("abc")

    build_dist.scrub_sensitive(root)

    assert (root / "app.exe").exists()
    assert (root / "ffmpeg.exe").exists()
    assert (web / "index.html").exists()
    assert (web / "app.js").exists()
    assert (fonts / "src.ttf").exists()
    assert (root / "integrity.hash").exists()


# --------------------------------------------------------------------------
# write_launcher_bat
# --------------------------------------------------------------------------


def test_7_launcher_bat_sets_gate_envs(tmp_path, monkeypatch):
    monkeypatch.setattr(build_dist.platform, "system", lambda: "Windows")
    build_dist.write_launcher_bat(tmp_path, "app.exe")
    bat = tmp_path / "启动软件.bat"
    assert bat.exists()
    raw = bat.read_bytes()
    text = raw.decode("gbk")
    assert "DUB_ALIGN_LICENSE_REQUIRED=1" in text
    assert "DUB_ALIGN_RASP_STRICT=1" in text
    assert "app.exe" in text
    # CRLF 行尾（Windows bat 兼容）
    assert b"\r\n" in raw


def test_7_launcher_bat_noop_on_non_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(build_dist.platform, "system", lambda: "Linux")
    build_dist.write_launcher_bat(tmp_path, "app")
    assert not (tmp_path / "启动软件.bat").exists()
