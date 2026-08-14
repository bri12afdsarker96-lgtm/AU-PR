"""SQLite 持久任务队列测试。覆盖 33-35, 47, 48。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_TTS_RUNNING,
    STATUS_VIDEO_RUNNING, STATUS_INTERRUPTED, TaskStore,
)


def _new_store(tmp_path):
    return TaskStore(tmp_path / "q.sqlite3")


def _add(store, batch_id, row, video="/v.mp4", text="hi", fp=None):
    return store.add_task(
        batch_id=batch_id, excel_row=row, input_video=video, text=text,
        fingerprint=fp or f"fp-{row}", voice_id="v", voice_name="V",
        speed=1.0, keep_original_audio=False, params_snapshot={},
    )


def test_create_batch_and_add_task(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("试", "/out", {"a": 1})
    tid = _add(s, b, 2)
    row = s.get(tid)
    assert row.status == STATUS_PENDING and row.excel_row == 2


def test_claim_next_is_atomic(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    tid = _add(s, b, 2)
    r1 = s.claim_next((STATUS_PENDING,), STATUS_TTS_RUNNING)
    r2 = s.claim_next((STATUS_PENDING,), STATUS_TTS_RUNNING)
    assert r1 is not None and r2 is None
    assert r1.task_id == tid and r1.status == STATUS_TTS_RUNNING


def test_34_reap_interrupted(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    _add(s, b, 2); _add(s, b, 3)
    for row_num, status in ((2, STATUS_TTS_RUNNING), (3, STATUS_VIDEO_RUNNING)):
        r = s.list_tasks(excel_row=row_num, limit=1)[0]
        s.update(r.task_id, status=status)
    n = s.reap_interrupted()
    assert n == 2
    for row_num in (2, 3):
        r = s.list_tasks(excel_row=row_num, limit=1)[0]
        assert r.status == STATUS_INTERRUPTED


def test_33_restart_persistence(tmp_path):
    db = tmp_path / "q.sqlite3"
    s1 = TaskStore(db)
    b = s1.create_batch("t", "/o", {})
    tid = _add(s1, b, 2)
    s1.update(tid, status=STATUS_COMPLETED, output_path="/o/1.mp4")
    s2 = TaskStore(db)
    row = s2.get(tid)
    assert row.status == STATUS_COMPLETED and row.output_path == "/o/1.mp4"


def test_35_get_by_fingerprint_completed(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    tid = _add(s, b, 2, fp="same-fp")
    s.update(tid, status=STATUS_COMPLETED, output_path="/o/1.mp4")
    hit = s.get_by_fingerprint("same-fp", status=STATUS_COMPLETED)
    assert hit is not None and hit.task_id == tid
    assert s.get_by_fingerprint("no-such") is None


def test_47_pagination(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    for i in range(2, 30):
        _add(s, b, i)
    page1 = s.list_tasks(batch_id=b, limit=10, offset=0)
    page2 = s.list_tasks(batch_id=b, limit=10, offset=10)
    page3 = s.list_tasks(batch_id=b, limit=10, offset=20)
    assert len(page1) == 10 and len(page2) == 10 and len(page3) == 8


def test_48_changed_since(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    tid_a = _add(s, b, 2)
    time.sleep(0.01)
    checkpoint = time.time()
    time.sleep(0.01)
    tid_b = _add(s, b, 3)
    s.update(tid_a, status=STATUS_TTS_RUNNING)
    changes = s.changed_since(checkpoint, batch_id=b)
    ids = {c.task_id for c in changes}
    assert tid_a in ids and tid_b in ids


def test_count_by_status(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    for i in range(2, 5):
        _add(s, b, i)
    tid = s.list_tasks(batch_id=b, limit=1)[0].task_id
    s.update(tid, status=STATUS_FAILED, error_type="x", error_detail="y")
    counts = s.count_by_status(b)
    assert counts[STATUS_PENDING] == 2 and counts[STATUS_FAILED] == 1


def test_query_filter(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    _add(s, b, 2, video="/v/apple.mp4")
    _add(s, b, 3, video="/v/banana.mp4")
    hits = s.list_tasks(batch_id=b, query="apple")
    assert len(hits) == 1 and hits[0].excel_row == 2


def test_iter_all_streams(tmp_path):
    s = _new_store(tmp_path)
    b = s.create_batch("t", "/o", {})
    for i in range(2, 100):
        _add(s, b, i)
    all_rows = list(s.iter_all(batch_id=b))
    assert len(all_rows) == 98
