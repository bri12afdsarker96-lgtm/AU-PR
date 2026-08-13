"""Service + API + CSV 导出的确定性测试。"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import api as bulk_api  # noqa: E402
from dub_align_studio.bulk_dub.csv_export import COLUMNS, write_csv  # noqa: E402
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx  # noqa: E402
from dub_align_studio.bulk_dub.service import BulkDubService  # noqa: E402
from dub_align_studio.bulk_dub.store import STATUS_COMPLETED, TaskStore  # noqa: E402


def _mk_service(tmp_path):
    return BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.05),
    )


def test_49_csv_3000_records(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    # 用 bulk_insert 快速灌
    rows = []
    for i in range(2, 3002):
        rows.append(dict(
            excel_row=i, input_video=f"/vid/{i}.mp4",
            text=f"文案 {i}", fingerprint=f"fp-{i}",
            voice_id="v", voice_name="V", speed=1.25,
            keep_original_audio=False, params_snapshot={},
        ))
    ids = store.bulk_insert(b, rows)
    for i, tid in enumerate(ids):
        if (i + 2) % 3 == 0:
            store.update(tid, status=STATUS_COMPLETED, output_path=f"/o/{i}.mp4",
                          final_duration=10, tts_duration=8, video_duration=5)
    buf = io.StringIO()
    n = write_csv(store.iter_all(b), buf)
    assert n == 3000
    content = buf.getvalue()
    first_line = content.split("\n", 1)[0]
    for col in ("Excel 行号", "输入视频", "音色名称", "voice ID", "语速"):
        assert col in first_line


def test_50_api_returns_expected_state_fields(tmp_path):
    svc = _mk_service(tmp_path)
    handled, status, body, _ = bulk_api.dispatch_get("/api/bulk_dub/voices", {}, service=svc)
    assert handled and status == 200
    payload = json.loads(body)
    assert payload["default_voice_id"] == "zh-CN-XiaoshuangNeural"
    assert payload["default_speed"] == 1.25
    assert len(payload["voices"]) > 0
    assert "edge_endpoint_configured" in payload

    handled, status, body, _ = bulk_api.dispatch_get("/api/bulk_dub/summary", {}, service=svc)
    assert handled and status == 200
    summary = json.loads(body)
    assert "counts" in summary and "scheduler" in summary and "disk" in summary
    for key in ("total", "waiting", "tts_running", "video_running", "succeeded",
                "failed", "retry_wait"):
        assert key in summary["totals"]
    for key in ("recent_rate_1min", "recent_rate_5min", "recent_rate_60min",
                "avg_tts_seconds", "avg_video_seconds",
                "http_429_count", "http_5xx_count", "retry_count",
                "hw_fallback_count", "projected_24h_estimate"):
        assert key in summary["scheduler"]["metrics"]
    assert "note_24h" in summary["scheduler"]
    svc.stop()


def test_50_start_via_api_needs_valid_output_dir(tmp_path):
    svc = _mk_service(tmp_path)
    xlsx = build_minimal_xlsx([("/nope.mp4", "文案")])
    # 空 output_dir → 400
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start", {"output_dir": "", "check_exists": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    assert handled and status == 400

    (tmp_path / "out").mkdir()
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start",
        {"output_dir": str(tmp_path / "out"), "check_exists": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    assert handled and status == 200
    assert json.loads(body).get("batch_id")
    svc.stop()


def test_50_pause_via_api(tmp_path):
    svc = _mk_service(tmp_path)
    handled, status, body, _ = bulk_api.dispatch_post("/api/bulk_dub/pause", {}, b"", service=svc)
    assert handled and status == 200
    assert json.loads(body)["paused"] is True
    svc.stop()


def test_50_page_html_exists():
    page = Path(__file__).resolve().parents[1] / "source" / "dub_align_studio" / "web" / "bulk_dub.html"
    assert page.is_file(), "bulk_dub.html 缺失"
    html = page.read_text(encoding="utf-8")
    for token in (
        "excelFile", "btnPreview", "btnStart", "btnPause", "btnResume",
        "btnCancelWait", "btnRetryFail", "btnCSV", "voiceId", "speedRange",
        "keepOrig", "outputDir", "ttsConc", "videoConc", "encoderPref",
        "endpointBadge", "batchSelector", "tryAudioBox",
    ):
        assert token in html, f"页面缺失控件：{token}"


def test_50_index_html_has_bulk_dub_entry():
    """R9：主页面必须有可发现的批量带货入口。"""
    page = Path(__file__).resolve().parents[1] / "source" / "dub_align_studio" / "web" / "index.html"
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    assert "/bulk_dub" in html, "index.html 缺入口链接"
    assert "Excel 批量带货" in html, "index.html 缺文案入口"


def test_50_csv_columns_match_requirement():
    for required in ("Excel 行号", "输入视频", "文案摘要", "输出视频", "状态",
                      "失败原因", "视频原始时长(s)", "TTS 时长(s)", "最终时长(s)",
                      "音色名称", "voice ID", "语速", "是否保留原声", "尝试次数",
                      "开始时间", "完成时间"):
        assert required in COLUMNS


def test_35_reuse_completed_by_fingerprint(tmp_path):
    svc = _mk_service(tmp_path)
    b0 = svc.store.create_batch("prev", "/o", {})
    ok_mp4 = tmp_path / "prev.mp4"
    ok_mp4.write_bytes(b"\x00")
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    fp = compute_fingerprint(video_path=str(ok_mp4), text="重用",
                              voice_id="zh-CN-XiaoshuangNeural", speed=1.25)
    tid = svc.store.add_task(
        batch_id=b0, excel_row=2, input_video=str(ok_mp4), text="重用",
        fingerprint=fp, voice_id="zh-CN-XiaoshuangNeural", voice_name="晓双（女·青春）",
        speed=1.25, keep_original_audio=False, params_snapshot={},
    )
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(tmp_path / "prev.mp4"),
                      final_duration=5, tts_duration=3, video_duration=2)
    xlsx = build_minimal_xlsx([(str(ok_mp4), "重用")])
    (tmp_path / "out").mkdir()
    r = svc.start_batch(source_bytes=xlsx, label="dedupe",
                          output_dir=str(tmp_path / "out"),
                          voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                          check_exists=True)
    assert r["reused"] == 1
    assert r["added"] == 0
    svc.stop()


def test_23_24_25_api_carries_voice_and_speed(tmp_path):
    svc = _mk_service(tmp_path)
    xlsx = build_minimal_xlsx([("/x.mp4", "文案")])
    (tmp_path / "out").mkdir()
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start",
        {"output_dir": str(tmp_path / "out"),
         "voice_id": "zh-CN-YunxiNeural", "speed": "0.9",
         "check_exists": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    j = json.loads(body)
    assert status == 200
    tasks = svc.store.list_tasks(batch_id=j["batch_id"], limit=10)
    assert tasks and tasks[0].voice_id == "zh-CN-YunxiNeural"
    assert tasks[0].speed == 0.9
    # 25: try_voice 用当前音色和语速；返回本地文件路径
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/try_voice",
        {"voice_id": "zh-CN-YunxiNeural", "speed": "0.9"},
        b"", service=svc,
    )
    assert handled and status == 200
    assert "audio_path" in json.loads(body)
    svc.stop()
