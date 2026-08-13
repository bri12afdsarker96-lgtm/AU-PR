"""Service + API + CSV 导出的确定性测试。覆盖 43、49、50、35。"""

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
        output_dir=str(tmp_path / "out"),
    )


def test_43_repeat_output_gets_suffix(tmp_path):
    from dub_align_studio.bulk_dub.ffmpeg_pipeline import _next_unique_path
    (tmp_path / "x.mp4").write_bytes(b"a")
    p = _next_unique_path(tmp_path / "x.mp4")
    assert p.name == "x_2.mp4"


def test_49_csv_3000_records(tmp_path):
    """稳定导出万级——用 3000 条流式，验证不吃过多内存/不漏行。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    for i in range(2, 3002):
        tid = store.add_task(
            batch_id=b, excel_row=i, input_video=f"/vid/{i}.mp4",
            text=f"文案 {i}", fingerprint=f"fp-{i}",
            voice_id="v", voice_name="V", speed=1.25,
            keep_original_audio=False, params_snapshot={},
        )
        if i % 3 == 0:
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

    handled, status, body, _ = bulk_api.dispatch_get("/api/bulk_dub/summary", {}, service=svc)
    assert handled and status == 200
    summary = json.loads(body)
    assert "counts" in summary and "scheduler" in summary and "disk" in summary
    for key in ("total", "waiting", "tts_running", "video_running", "succeeded",
                "failed", "retry_wait"):
        assert key in summary["totals"], f"缺 totals 字段：{key}"
    for key in ("recent_rate_1min", "recent_rate_5min", "recent_rate_60min",
                "avg_tts_seconds", "avg_video_seconds", "projected_24h",
                "http_429_count", "http_5xx_count", "retry_count"):
        assert key in summary["scheduler"]["metrics"], f"缺 metrics 字段：{key}"
    svc.stop()


def test_50_start_and_pause_via_api(tmp_path):
    svc = _mk_service(tmp_path)
    xlsx = build_minimal_xlsx([("/nope.mp4", "文案")])
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start",
        {"output_dir": str(tmp_path / "out"), "check_exists": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    assert handled and status == 200
    j = json.loads(body)
    assert j.get("batch_id")
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
    ):
        assert token in html, f"页面缺失控件：{token}"


def test_50_csv_columns_match_requirement():
    for required in ("Excel 行号", "输入视频", "文案摘要", "输出视频", "状态",
                      "失败原因", "视频原始时长(s)", "TTS 时长(s)", "最终时长(s)",
                      "音色名称", "voice ID", "语速", "是否保留原声", "尝试次数",
                      "开始时间", "完成时间"):
        assert required in COLUMNS, f"CSV 缺列：{required}"


def test_35_reuse_completed_by_fingerprint(tmp_path):
    """35 + 36: 已完成任务不会重复提交（同指纹直接建成 completed 复用产物）。"""
    svc = _mk_service(tmp_path)
    b0 = svc.store.create_batch("prev", "/o", {})
    ok_mp4 = tmp_path / "prev.mp4"
    ok_mp4.write_bytes(b"\x00")
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    fp = compute_fingerprint(video_path=str(ok_mp4), text="重用", voice_id="zh-CN-XiaoshuangNeural",
                              speed=1.25)
    tid = svc.store.add_task(
        batch_id=b0, excel_row=2, input_video=str(ok_mp4), text="重用",
        fingerprint=fp, voice_id="zh-CN-XiaoshuangNeural", voice_name="晓双（女·青春）",
        speed=1.25, keep_original_audio=False, params_snapshot={},
    )
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(tmp_path / "prev.mp4"),
                      final_duration=5, tts_duration=3, video_duration=2)
    xlsx = build_minimal_xlsx([(str(ok_mp4), "重用")])
    r = svc.start_batch(source_bytes=xlsx, label="dedupe",
                          output_dir=str(tmp_path / "out"),
                          voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                          check_exists=True)
    assert r["reused"] == 1
    assert r["added"] == 0
    svc.stop()


def test_23_24_25_api_carries_voice_and_speed(tmp_path):
    """23/24/25: 切换音色/语速后请求参数与 UI 同步；试听使用当前音色和语速。"""
    svc = _mk_service(tmp_path)
    xlsx = build_minimal_xlsx([("/x.mp4", "文案")])
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start",
        {"output_dir": str(tmp_path / "out"),
         "voice_id": "zh-CN-YunxiNeural", "speed": "0.9",
         "check_exists": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    j = json.loads(body)
    tasks = svc.store.list_tasks(batch_id=j["batch_id"], limit=10)
    assert tasks and tasks[0].voice_id == "zh-CN-YunxiNeural"
    assert tasks[0].speed == 0.9
    # 25: try_voice 用当前音色和语速
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/try_voice",
        {"voice_id": "zh-CN-YunxiNeural", "speed": "0.9"},
        b"", service=svc,
    )
    assert handled and status == 200
    assert "audio_path" in json.loads(body)
    svc.stop()
