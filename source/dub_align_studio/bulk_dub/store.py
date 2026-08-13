"""持久任务队列（SQLite / WAL）——批量带货配音的存储层。

数据库路径：
    默认在 studio_settings.data_root() / "批量带货" / "queue.sqlite3"。
    也可以由调用者显式传入 path，供测试用 tmp 目录。
    不放输出目录、不放网络共享盘（首版**单机**队列）。

状态机（fsm）：
    pending → validating → tts_running → tts_done → video_running → completed
                                    ↘ retry_wait ↗
    任何阶段可 → failed / cancelled；崩溃遗留 → interrupted。

线程安全：
    - SQLite 3.x 在 Python 里默认 check_same_thread=True，本模块统一在方法内 open
      并即刻 close（一次 UPDATE/INSERT 一个短连接），避免线程绑定问题；调度器读侧
      也是短连接。测试里跑 4 个 worker × 万级任务不会因连接抢占死锁。
    - WAL 模式开启后并发读写更平滑；写入串行化仍由 SQLite 自身保证。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

STATUS_PENDING = "pending"
STATUS_VALIDATING = "validating"
STATUS_TTS_RUNNING = "tts_running"
STATUS_TTS_DONE = "tts_done"
STATUS_VIDEO_RUNNING = "video_running"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

ALL_STATUSES = frozenset({
    STATUS_PENDING, STATUS_VALIDATING, STATUS_TTS_RUNNING, STATUS_TTS_DONE,
    STATUS_VIDEO_RUNNING, STATUS_RETRY_WAIT, STATUS_COMPLETED,
    STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED,
})

_RUNNING_STATUSES = (STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING, STATUS_VALIDATING)


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    batch_id: str
    excel_row: int
    input_video: str
    text: str
    fingerprint: str
    output_path: str
    staging_dir: str
    stage: str
    status: str
    attempts: int
    progress: int
    voice_id: str
    voice_name: str
    speed: float
    keep_original_audio: int
    params_snapshot: dict
    error_type: str
    error_detail: str
    video_duration: float
    tts_duration: float
    concat_duration: float
    final_duration: float
    created_at: float
    started_at: float
    finished_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT    PRIMARY KEY,
    batch_id          TEXT    NOT NULL,
    excel_row         INTEGER NOT NULL,
    input_video       TEXT    NOT NULL,
    text              TEXT    NOT NULL,
    fingerprint       TEXT    NOT NULL,
    output_path       TEXT    NOT NULL DEFAULT '',
    staging_dir       TEXT    NOT NULL DEFAULT '',
    stage             TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    progress          INTEGER NOT NULL DEFAULT 0,
    voice_id          TEXT    NOT NULL DEFAULT '',
    voice_name        TEXT    NOT NULL DEFAULT '',
    speed             REAL    NOT NULL DEFAULT 1.0,
    keep_original_audio INTEGER NOT NULL DEFAULT 0,
    params_snapshot   TEXT    NOT NULL DEFAULT '{}',
    error_type        TEXT    NOT NULL DEFAULT '',
    error_detail      TEXT    NOT NULL DEFAULT '',
    video_duration    REAL    NOT NULL DEFAULT 0,
    tts_duration      REAL    NOT NULL DEFAULT 0,
    concat_duration   REAL    NOT NULL DEFAULT 0,
    final_duration    REAL    NOT NULL DEFAULT 0,
    created_at        REAL    NOT NULL DEFAULT 0,
    started_at        REAL    NOT NULL DEFAULT 0,
    finished_at       REAL    NOT NULL DEFAULT 0,
    updated_at        REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_batch     ON tasks(batch_id, excel_row);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_fpr       ON tasks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_tasks_updated   ON tasks(updated_at);
CREATE TABLE IF NOT EXISTS batches (
    batch_id     TEXT    PRIMARY KEY,
    label        TEXT    NOT NULL DEFAULT '',
    output_dir   TEXT    NOT NULL DEFAULT '',
    params_json  TEXT    NOT NULL DEFAULT '{}',
    created_at   REAL    NOT NULL DEFAULT 0,
    frozen_at    REAL    NOT NULL DEFAULT 0
);
"""


def default_db_path() -> Path:
    from .. import settings as studio_settings

    root = studio_settings.data_root() / "批量带货"
    root.mkdir(parents=True, exist_ok=True)
    return root / "queue.sqlite3"


class TaskStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
            self._initialized = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    def create_batch(self, label: str, output_dir: str, params: dict) -> str:
        batch_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO batches(batch_id,label,output_dir,params_json,created_at,frozen_at)"
                " VALUES(?,?,?,?,?,?)",
                (batch_id, label, output_dir, json.dumps(params, ensure_ascii=False), now, now),
            )
        return batch_id

    def get_batch(self, batch_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "batch_id": row["batch_id"],
            "label": row["label"],
            "output_dir": row["output_dir"],
            "params": json.loads(row["params_json"] or "{}"),
            "created_at": row["created_at"],
            "frozen_at": row["frozen_at"],
        }

    def list_batches(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT batch_id,label,output_dir,created_at FROM batches"
                " ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_task(self, *, batch_id: str, excel_row: int, input_video: str,
                  text: str, fingerprint: str, voice_id: str, voice_name: str,
                  speed: float, keep_original_audio: bool,
                  params_snapshot: dict) -> str:
        task_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks("
                " task_id,batch_id,excel_row,input_video,text,fingerprint,"
                " status,voice_id,voice_name,speed,keep_original_audio,params_snapshot,"
                " created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, batch_id, int(excel_row), input_video, text, fingerprint,
                 STATUS_PENDING, voice_id, voice_name, float(speed),
                 1 if keep_original_audio else 0,
                 json.dumps(params_snapshot, ensure_ascii=False),
                 now, now),
            )
        return task_id

    def get(self, task_id: str) -> TaskRow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row is not None else None

    def get_by_fingerprint(self, fingerprint: str, status: str | None = None) -> TaskRow | None:
        with self._connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE fingerprint=? AND status=?"
                    " ORDER BY finished_at DESC LIMIT 1", (fingerprint, status)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE fingerprint=?"
                    " ORDER BY updated_at DESC LIMIT 1", (fingerprint,)
                ).fetchone()
        return _row_to_task(row) if row is not None else None

    def update(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "output_path", "staging_dir", "stage", "status", "attempts", "progress",
            "error_type", "error_detail", "video_duration", "tts_duration",
            "concat_duration", "final_duration", "started_at", "finished_at",
        }
        params_snapshot = fields.pop("params_snapshot", None)
        columns: list[str] = []
        values: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"update(): 不允许写入的列名 {k}")
            columns.append(f"{k}=?")
            values.append(v)
        if params_snapshot is not None:
            columns.append("params_snapshot=?")
            values.append(json.dumps(params_snapshot, ensure_ascii=False))
        columns.append("updated_at=?")
        values.append(time.time())
        values.append(task_id)
        sql = f"UPDATE tasks SET {', '.join(columns)} WHERE task_id=?"
        with self._connect() as conn:
            conn.execute(sql, values)

    def bump_attempts(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET attempts=attempts+1, updated_at=? WHERE task_id=?",
                (time.time(), task_id),
            )

    def claim_next(self, allowed_statuses: Iterable[str], target_status: str,
                   *, batch_id: str | None = None) -> TaskRow | None:
        statuses = tuple(allowed_statuses)
        if not statuses:
            return None
        for _ in range(5):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    q = (
                        "SELECT * FROM tasks WHERE status IN (" + ",".join("?" * len(statuses)) + ")"
                    )
                    args: list[Any] = list(statuses)
                    if batch_id:
                        q += " AND batch_id=?"
                        args.append(batch_id)
                    q += " ORDER BY created_at ASC, excel_row ASC LIMIT 1"
                    row = conn.execute(q, args).fetchone()
                    if row is None:
                        conn.execute("COMMIT")
                        return None
                    cur = conn.execute(
                        "UPDATE tasks SET status=?, updated_at=?"
                        " WHERE task_id=? AND status IN (" + ",".join("?" * len(statuses)) + ")",
                        (target_status, time.time(), row["task_id"], *statuses),
                    )
                    if cur.rowcount == 1:
                        row2 = conn.execute(
                            "SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)
                        ).fetchone()
                        conn.execute("COMMIT")
                        return _row_to_task(row2)
                    conn.execute("ROLLBACK")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        return None

    def count_by_status(self, batch_id: str | None = None) -> dict[str, int]:
        with self._connect() as conn:
            if batch_id:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE batch_id=? GROUP BY status",
                    (batch_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                ).fetchall()
        result = {s: 0 for s in ALL_STATUSES}
        for r in rows:
            result[r["status"]] = int(r["n"])
        return result

    def list_tasks(self, *, batch_id: str | None = None,
                    status: str | None = None, excel_row: int | None = None,
                    query: str | None = None, limit: int = 100,
                    offset: int = 0, order: str = "row") -> list[TaskRow]:
        conditions: list[str] = []
        args: list[Any] = []
        if batch_id:
            conditions.append("batch_id=?"); args.append(batch_id)
        if status:
            conditions.append("status=?"); args.append(status)
        if excel_row is not None:
            conditions.append("excel_row=?"); args.append(int(excel_row))
        if query:
            like = f"%{query}%"
            conditions.append("(input_video LIKE ? OR text LIKE ? OR output_path LIKE ?)")
            args.extend([like, like, like])
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        order_by = {
            "row": "excel_row ASC, created_at ASC",
            "updated": "updated_at DESC",
            "created": "created_at ASC",
        }.get(order, "excel_row ASC")
        sql = f"SELECT * FROM tasks {where} ORDER BY {order_by} LIMIT ? OFFSET ?"
        args.extend([int(limit), int(offset)])
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def iter_all(self, batch_id: str | None = None) -> Iterator[TaskRow]:
        offset = 0
        while True:
            batch = self.list_tasks(batch_id=batch_id, limit=500, offset=offset)
            if not batch:
                return
            for row in batch:
                yield row
            offset += len(batch)

    def changed_since(self, since: float, batch_id: str | None = None,
                      limit: int = 500) -> list[TaskRow]:
        conditions = ["updated_at>?"]
        args: list[Any] = [float(since)]
        if batch_id:
            conditions.append("batch_id=?"); args.append(batch_id)
        sql = (
            "SELECT * FROM tasks WHERE " + " AND ".join(conditions) +
            " ORDER BY updated_at ASC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def reap_interrupted(self, batch_id: str | None = None) -> int:
        placeholders = ",".join("?" * len(_RUNNING_STATUSES))
        args: list[Any] = list(_RUNNING_STATUSES)
        sql = (
            f"UPDATE tasks SET status=?, error_type='interrupted',"
            f" error_detail='软件重启或异常退出中断', updated_at=?"
            f" WHERE status IN ({placeholders})"
        )
        args = [STATUS_INTERRUPTED, time.time()] + args
        if batch_id:
            sql += " AND batch_id=?"; args.append(batch_id)
        with self._connect() as conn:
            cur = conn.execute(sql, args)
            return int(cur.rowcount or 0)

    def reset_status(self, task_id: str, new_status: str = STATUS_PENDING) -> None:
        if new_status not in ALL_STATUSES:
            raise ValueError(f"未知状态：{new_status}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, error_type='', error_detail='',"
                " progress=0, updated_at=? WHERE task_id=?",
                (new_status, time.time(), task_id),
            )

    def bulk_reset(self, batch_id: str, from_status: str,
                   to_status: str = STATUS_PENDING) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status=?, error_type='', error_detail='',"
                " progress=0, updated_at=? WHERE batch_id=? AND status=?",
                (to_status, time.time(), batch_id, from_status),
            )
            return int(cur.rowcount or 0)


def _row_to_task(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        task_id=row["task_id"], batch_id=row["batch_id"],
        excel_row=int(row["excel_row"]),
        input_video=row["input_video"], text=row["text"],
        fingerprint=row["fingerprint"],
        output_path=row["output_path"] or "",
        staging_dir=row["staging_dir"] or "",
        stage=row["stage"] or "",
        status=row["status"],
        attempts=int(row["attempts"] or 0),
        progress=int(row["progress"] or 0),
        voice_id=row["voice_id"] or "",
        voice_name=row["voice_name"] or "",
        speed=float(row["speed"] or 1.0),
        keep_original_audio=int(row["keep_original_audio"] or 0),
        params_snapshot=json.loads(row["params_snapshot"] or "{}"),
        error_type=row["error_type"] or "",
        error_detail=row["error_detail"] or "",
        video_duration=float(row["video_duration"] or 0),
        tts_duration=float(row["tts_duration"] or 0),
        concat_duration=float(row["concat_duration"] or 0),
        final_duration=float(row["final_duration"] or 0),
        created_at=float(row["created_at"] or 0),
        started_at=float(row["started_at"] or 0),
        finished_at=float(row["finished_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
    )
