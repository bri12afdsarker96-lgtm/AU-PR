"""批量带货配音 · Excel 解析测试。覆盖点 1-7。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import excel_reader  # noqa: E402


def test_1_first_row_is_header_skipped():
    xlsx = excel_reader.build_minimal_xlsx(
        [("C:/带货/1.mp4", "第一条文案"), ("D:/带货/2.mp4", "第二条")],
        include_header=True,
    )
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert result.total == 2
    assert all(r.row_number >= 2 for r in result.rows), "首行应跳过"


def test_2_columns_a_and_b_parsed():
    xlsx = excel_reader.build_minimal_xlsx(
        [("/videos/a.mp4", "文案A"), ("/videos/b.mov", "文案B")],
    )
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    videos = [r.video_path for r in result.rows]
    texts = [r.text for r in result.rows]
    assert "/videos/a.mp4" in videos
    assert "文案A" in texts and "文案B" in texts


def test_3_empty_rows_skipped():
    xlsx = excel_reader.build_minimal_xlsx(
        [("A/1.mp4", "1"), ("", ""), ("", ""), ("A/2.mp4", "2")],
    )
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert result.skipped_empty == 2
    assert len(result.rows) == 2


def test_4_chinese_and_space_paths():
    xlsx = excel_reader.build_minimal_xlsx(
        [("C:/我的 视频/带货 一.mp4", "中文文案 有空格"),
         (r"\\NAS\share\video 1.mkv", "UNC 路径")],
    )
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert result.valid == 2
    videos = [r.video_path for r in result.rows]
    assert "C:/我的 视频/带货 一.mp4" in videos
    assert r"\\NAS\share\video 1.mkv" in videos


def test_5_missing_video_marks_row_failed(tmp_path):
    exists = tmp_path / "real.mp4"
    exists.write_bytes(b"\x00" * 8)
    xlsx = excel_reader.build_minimal_xlsx(
        [(str(exists), "有视频"), (str(tmp_path / "no.mp4"), "无视频")],
    )
    result = excel_reader.parse_excel(xlsx, check_exists=True)
    assert result.valid == 1 and result.invalid == 1
    bad = [r for r in result.rows if not r.valid][0]
    assert "不存在" in bad.reason


def test_6_empty_text_marks_row_failed():
    xlsx = excel_reader.build_minimal_xlsx(
        [("/x.mp4", ""), ("/y.mp4", "有")],
    )
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert result.valid == 1 and result.invalid == 1
    bad = [r for r in result.rows if not r.valid][0]
    assert "B 列" in bad.reason


def test_7_single_row_failure_does_not_block_others(tmp_path):
    good = tmp_path / "g.mp4"
    good.write_bytes(b"\x00")
    xlsx = excel_reader.build_minimal_xlsx([
        ("", "空 A"),
        (str(good), ""),
        ("/x.txt", "格式不对"),
        ("/missing.mp4", "找不到"),
        (str(good), "正常一条"),
    ])
    result = excel_reader.parse_excel(xlsx, check_exists=True)
    assert result.valid == 1 and result.invalid == 4
    reasons = {r.reason for r in result.rows if not r.valid}
    assert len(reasons) == 4


def test_extra_bad_extension_rejected():
    xlsx = excel_reader.build_minimal_xlsx([("/a.txt", "abc")])
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert result.invalid == 1
    assert "格式" in result.rows[0].reason


def test_extra_preview_shortens_long_text():
    long = "口播" * 200
    xlsx = excel_reader.build_minimal_xlsx([("/a.mp4", long)])
    result = excel_reader.parse_excel(xlsx, check_exists=False)
    assert "…" in result.rows[0].text_preview
    assert len(result.rows[0].text_preview) < len(long)
