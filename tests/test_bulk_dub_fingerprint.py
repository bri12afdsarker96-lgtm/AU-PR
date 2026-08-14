"""指纹幂等测试。覆盖 36-37。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint  # noqa: E402


def _base(**over):
    args = dict(video_path="/x/a.mp4", text="你好", voice_id="v1", speed=1.25)
    args.update(over)
    return compute_fingerprint(**args)


def test_36_same_inputs_same_fingerprint():
    assert _base() == _base()


def test_37_text_change_new_fingerprint():
    assert _base(text="A") != _base(text="B")


def test_37_speed_change_new_fingerprint():
    assert _base(speed=1.25) != _base(speed=1.30)


def test_37_voice_change_new_fingerprint():
    assert _base(voice_id="v1") != _base(voice_id="v2")


def test_37_keep_original_change_new_fingerprint():
    assert _base(keep_original_audio=False) != _base(keep_original_audio=True)


def test_37_zoom_change_new_fingerprint():
    a = compute_fingerprint(video_path="/x/a.mp4", text="t", voice_id="v", speed=1.0, zoom_percent=130)
    b = compute_fingerprint(video_path="/x/a.mp4", text="t", voice_id="v", speed=1.0, zoom_percent=140)
    assert a != b


def test_37_video_mtime_change_new_fingerprint(tmp_path):
    import os
    f = tmp_path / "v.mp4"
    f.write_bytes(b"a")
    a = compute_fingerprint(video_path=str(f), text="t", voice_id="v", speed=1.0)
    os.utime(f, (0, 0))
    b = compute_fingerprint(video_path=str(f), text="t", voice_id="v", speed=1.0)
    assert a != b


def test_37_video_content_change_new_fingerprint(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"aaa")
    a = compute_fingerprint(video_path=str(f), text="t", voice_id="v", speed=1.0)
    f.write_bytes(b"bbbbb")
    b = compute_fingerprint(video_path=str(f), text="t", voice_id="v", speed=1.0)
    assert a != b
