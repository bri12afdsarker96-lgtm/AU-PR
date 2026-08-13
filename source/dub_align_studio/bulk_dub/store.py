"""持久任务队列（SQLite / WAL）——批量带货配音的存储层。

数据库路径：
    默认在 studio_settings.data_root() / "批量带货" / "queue.sqlite3"。
    也可以由调用者显式传入 path，供测试用 tmp 目录。
    不放输出目录、不放网络共享盘（首版**单机**队列）。

状态机（fsm）：
    pending → validating → tts_running → tts_done → video_running → completed
                                    ↘ retry_wait ↗
    任何阶段可 → failed / cancelled；崩溃遗留 → interrupted / 或直接恢复到 pending/tts_done。

线程安全：
    - SQLite 3.x 在 Python 里默认 check_same_thread=True，本模块统一在方法内 open
      并即刻 close（一次 UPDATE/INSERT 一个短连接），避免线程绑定问题。
    - WAL 模式开启后并发读写更平滑；写入串行化仍由 SQLite 自身保证。
    - `reserve_output_path` 用唯一索引原子预留输出路径，防止并发 worker 撞名。

Schema v2（本次 R1 迁移）：
    - 增加 next_attempt_at REAL DEFAULT 0（retry_wait 持久化到期时间）；
    - 增加 warnings TEXT DEFAULT '[]'（JSON 数组：无原声/时长偏差等黄警）；
    - 增加 hw_fallback_used INTEGER DEFAULT 0（视频阶段有过 HW→CPU 回退）；
    - 增加 encoder_used TEXT DEFAULT ''（实际编码器名）；
    - 增加 reserved_output_path TEXT DEFAULT ''（预留但未完成时也要占位）；
    - 为 output_path 建部分 UNIQUE 索引（NULL/空串不参与）。
"""

from __future__ import annotations

import json
import re
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

# batch_id / task_id 允许字符白名单——只允许 uuid 十六进制片段。
# 用来防"cleanup / 目录组装被路径注入"（P0-6 硬护栏之一）。
_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")


def is_safe_id(value: str) -> bool:
    """batch_id / task_id 是否为受控标识（uuid 片段）。任何非匹配 → 拒绝清理。"""
    return bool(value and _ID_RE.fullmatch(value or ""))


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    batch_id: str
    excel_row: int
    input_video: str
    text: str
    fingerprint: str
    output_path: str
    reserved_output_path: str
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
    warnings: list
    encoder_used: str
    hw_fallback_used: int
    video_duration: float
    tts_duration: float
    concat_duration: float
    final_duration: float
    created_at: float
    started_at: float
    finished_at: float
    updated_at: float
    next_attempt_at: float


_SCHEMA_V1 = """
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
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# v2 迁移语句（幂等，可反复跑）
_MIGRATION_V2 = [
    "ALTER TABLE tasks ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE tasks ADD COLUMN encoder_used TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN hw_fallback_used INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN reserved_output_path TEXT NOT NULL DEFAULT ''",
]


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
                conn.executescript(_SCHEMA_V1)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._apply_migrations(conn)
                # 只对 reserved_output_path 做部分 UNIQUE 索引——它是"渲染前抢占"的原子锁。
                # output_path 不能 UNIQUE：幂等复用场景允许多条 completed 行指向同一成片文件。
                # 建索引前先删旧同名索引（幂等升级：v2→v2.1 移除 output_path 唯一约束）
                conn.execute("DROP INDEX IF EXISTS idx_tasks_output_unique")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_reserved_unique"
                    " ON tasks(reserved_output_path) WHERE reserved_output_path != ''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_next_attempt"
                    " ON tasks(status, next_attempt_at)"
                )
            self._initialized = True

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """v2 加列——用 PRAGMA table_info 检测已存在则跳过。幂等。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for stmt in _MIGRATION_V2:
            # 提取列名做幂等判断
            m = re.search(r"ADD COLUMN (\w+)", stmt)
            if m and m.group(1) in cols:
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', '2')"
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- 批次

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

    def list_batches(self, limit: int = 50) -> list[dict]:
        """返回批次列表——含每批次的状态统计（供页面重开时选择历史批次）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT batch_id,label,output_dir,created_at FROM batches"
                " ORDER BY created_at DESC LIMIT ?", (int(limit),),
            ).fetchall()
            batches = [dict(r) for r in rows]
            # 附上每批次的 counts
            for b in batches:
                counts_rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE batch_id=? GROUP BY status",
                    (b["batch_id"],)
                ).fetchall()
                b["counts"] = {r["status"]: int(r["n"]) for r in counts_rows}
                b["total"] = sum(b["counts"].values())
        return batches

    # ---------------------------------------------------------------- 任务

    def add_task(self, *, batch_id: str, excel_row: int, input_video: str,
                  text: str, fingerprint: str, voice_id: str, voice_name: str,
                  speed: float, keep_original_audio: bool,
                  params_snapshot: dict, conn: sqlite3.Connection | None = None) -> str:
        """插入单条任务。可传入外部 conn 让 N 条走同一事务。"""
        task_id = uuid.uuid4().hex[:16]
        now = time.time()
        sql = (
            "INSERT INTO tasks("
            " task_id,batch_id,excel_row,input_video,text,fingerprint,"
            " status,voice_id,voice_name,speed,keep_original_audio,params_snapshot,"
            " created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        args = (
            task_id, batch_id, int(excel_row), input_video, text, fingerprint,
            STATUS_PENDING, voice_id, voice_name, float(speed),
            1 if keep_original_audio else 0,
            json.dumps(params_snapshot, ensure_ascii=False),
            now, now,
        )
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self._connect() as c:
                c.execute(sql, args)
        return task_id

    def bulk_insert(self, batch_id: str, tasks: list[dict]) -> list[str]:
        """R8 单事务批量插入 N 条任务。tasks 是 dict 列表，字段同 add_task 参数。

        用于 10000 行 xlsx 导入时避免 10000 次短连接开销。
        """
        task_ids: list[str] = []
        now = time.time()
        rows: list[tuple] = []
        for t in tasks:
            tid = uuid.uuid4().hex[:16]
            task_ids.append(tid)
            rows.append((
                tid, batch_id, int(t["excel_row"]),
                str(t["input_video"]), str(t["text"]), str(t["fingerprint"]),
                str(t.get("status") or STATUS_PENDING),
                str(t["voice_id"]), str(t["voice_name"]), float(t["speed"]),
                1 if t.get("keep_original_audio") else 0,
                json.dumps(t.get("params_snapshot") or {}, ensure_ascii=False),
                str(t.get("error_type") or ""),
                str(t.get("error_detail") or ""),
                now, now,
            ))
        if not rows:
            return []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT INTO tasks(task_id,batch_id,excel_row,input_video,text,"
                    "fingerprint,status,voice_id,voice_name,speed,keep_original_audio,"
                    "params_snapshot,error_type,error_detail,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return task_ids

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
            "output_path", "reserved_output_path", "staging_dir", "stage",
            "status", "attempts", "progress",
            "error_type", "error_detail", "warnings", "encoder_used",
            "hw_fallback_used",
            "video_duration", "tts_duration", "concat_duration", "final_duration",
            "started_at", "finished_at", "next_attempt_at",
        }
        params_snapshot = fields.pop("params_snapshot", None)
        columns: list[str] = []
        values: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"update(): 不允许写入的列名 {k}")
            if k == "warnings" and not isinstance(v, str):
                v = json.dumps(list(v or []), ensure_ascii=False)
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

    def add_warning(self, task_id: str, message: str) -> None:
        """把一条 warning 追加到 JSON 数组列（幂等 / 去重）。"""
        if not message:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT warnings FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            existing = json.loads((row["warnings"] if row else "[]") or "[]")
            if message not in existing:
                existing.append(message)
                conn.execute(
                    "UPDATE tasks SET warnings=?, updated_at=? WHERE task_id=?",
                    (json.dumps(existing, ensure_ascii=False), time.time(), task_id),
                )

    # ---------------------------------------------------------------- 领取

    def claim_next(self, allowed_statuses: Iterable[str], target_status: str,
                   *, batch_id: str | None = None,
                   respect_next_attempt: bool = True,
                   exclude_batch_ids: Iterable[str] | None = None) -> TaskRow | None:
        """原子领取一条任务；retry_wait 只领已到期（next_attempt_at <= now）的。

        R11-2：`exclude_batch_ids` 用来把"批次级暂停"直接下推到 SQL WHERE 里——
        避免"领了再放回"的死循环（否则同一 pending 暂停任务被反复 SELECT，
        排队更后面批次的任务永远饿死）。
        """
        statuses = tuple(allowed_statuses)
        if not statuses:
            return None
        excludes = tuple(exclude_batch_ids or ())
        now = time.time()
        for _ in range(5):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    q = (
                        "SELECT * FROM tasks WHERE status IN ("
                        + ",".join("?" * len(statuses)) + ")"
                    )
                    args: list[Any] = list(statuses)
                    if batch_id:
                        q += " AND batch_id=?"
                        args.append(batch_id)
                    if excludes:
                        q += " AND batch_id NOT IN (" + ",".join("?" * len(excludes)) + ")"
                        args.extend(excludes)
                    if respect_next_attempt:
                        q += " AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
                        args.append(now)
                    q += " ORDER BY created_at ASC, excel_row ASC LIMIT 1"
                    row = conn.execute(q, args).fetchone()
                    if row is None:
                        conn.execute("COMMIT")
                        return None
                    cur = conn.execute(
                        "UPDATE tasks SET status=?, updated_at=?, next_attempt_at=0"
                        " WHERE task_id=? AND status IN ("
                        + ",".join("?" * len(statuses)) + ")",
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

    def find_completed_by_fingerprints(self, fingerprints: Iterable[str]) -> dict[str, "TaskRow"]:
        """R11-7 一次批量查所有可复用记录，避免 N+1 查询。

        返回 {fingerprint: TaskRow(最新一条 completed)}。空输入返回 {}。
        SQLite 表达式列表默认最多约 999 个占位符——超过就分片。
        """
        fps = [f for f in fingerprints if f]
        if not fps:
            return {}
        out: dict[str, TaskRow] = {}
        CHUNK = 800
        with self._connect() as conn:
            for i in range(0, len(fps), CHUNK):
                chunk = fps[i:i + CHUNK]
                q = (
                    "SELECT * FROM tasks WHERE status=? AND fingerprint IN ("
                    + ",".join("?" * len(chunk)) + ")"
                    " ORDER BY fingerprint, finished_at DESC"
                )
                rows = conn.execute(q, (STATUS_COMPLETED, *chunk)).fetchall()
                for r in rows:
                    fp = r["fingerprint"]
                    if fp not in out:   # 已按 finished_at DESC 排序，第一条即最新
                        out[fp] = _row_to_task(r)
        return out

    # ---------------------------------------------------------------- 输出路径原子预留

    def reserve_output_path(self, task_id: str, base_path: Path) -> Path:
        """R4 原子预留输出路径：从 base_path 起找可用文件名（同名追加 _2/_3），
        写入 reserved_output_path 列（UNIQUE 约束保证跨线程唯一）。返回预留路径。

        - 只登记在数据库中；不真正 touch 文件。
        - 冲突：如果磁盘上已有同名文件（另一软件写的），也会跳过找下一个编号。
        """
        base = Path(base_path)
        stem = base.stem
        suffix = base.suffix
        parent = base.parent
        # 尝试 base, base_2, base_3, ...
        with self._connect() as conn:
            for i in range(1, 10000):
                candidate = base if i == 1 else parent / f"{stem}_{i}{suffix}"
                cand_str = str(candidate)
                # 磁盘上已存在？直接跳过
                if candidate.exists():
                    continue
                # 数据库里是否已被别人占用（output_path 或 reserved_output_path）？
                conn.execute("BEGIN IMMEDIATE")
                try:
                    hit = conn.execute(
                        "SELECT 1 FROM tasks"
                        " WHERE output_path=? OR reserved_output_path=?",
                        (cand_str, cand_str),
                    ).fetchone()
                    if hit is not None:
                        conn.execute("COMMIT")
                        continue
                    conn.execute(
                        "UPDATE tasks SET reserved_output_path=?, updated_at=?"
                        " WHERE task_id=?",
                        (cand_str, time.time(), task_id),
                    )
                    conn.execute("COMMIT")
                    return candidate
                except sqlite3.IntegrityError:
                    conn.execute("ROLLBACK")
                    continue
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        raise RuntimeError(f"输出重名预留失败（尝试 10000 次仍无空位）：{base}")

    def commit_output_path(self, task_id: str, final_path: str) -> None:
        """把 reserved_output_path 提升为正式 output_path（成品已落地时调用）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET output_path=?, updated_at=? WHERE task_id=?",
                (final_path, time.time(), task_id),
            )

    def release_reservation(self, task_id: str) -> None:
        """任务失败/取消时清掉预留位，让重试或后续任务可以重新预留同名。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET reserved_output_path='', updated_at=? WHERE task_id=?",
                (time.time(), task_id),
            )

    # ---------------------------------------------------------------- 分页/汇总

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
        """兼容旧接口——只按 updated_at > since 简单游标。**不适合万级同秒场景**。
        新代码请用 changed_since_cursor()。"""
        conditions = ["updated_at>?"]
        args: list[Any] = [float(since)]
        if batch_id:
            conditions.append("batch_id=?"); args.append(batch_id)
        sql = (
            "SELECT * FROM tasks WHERE " + " AND ".join(conditions) +
            " ORDER BY updated_at ASC, task_id ASC LIMIT ?"
        )
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def changed_since_cursor(self, cursor: str | None,
                              batch_id: str | None = None,
                              limit: int = 500) -> tuple[list["TaskRow"], str, bool]:
        """R11-5 真正稳定的复合游标：`(updated_at, task_id)`。

        - cursor 为空/None → 从最早开始
        - cursor 形如 "<updated_at>|<task_id>" —— 严格大于该 (u, id) 对
        - 返回 (rows, next_cursor, has_more)；rows 不重不漏，即使 10000 条同 updated_at
        """
        cursor_u: float = 0.0
        cursor_id: str = ""
        if cursor:
            try:
                left, right = cursor.split("|", 1)
                cursor_u = float(left)
                cursor_id = right
            except ValueError:
                cursor_u = 0.0
                cursor_id = ""
        # 复合游标 SQL：updated_at > u OR (updated_at = u AND task_id > id)
        conditions = ["(updated_at > ? OR (updated_at = ? AND task_id > ?))"]
        args: list[Any] = [cursor_u, cursor_u, cursor_id]
        if batch_id:
            conditions.append("batch_id=?"); args.append(batch_id)
        # 多取一条判断 has_more
        take = int(limit) + 1
        sql = (
            "SELECT * FROM tasks WHERE " + " AND ".join(conditions) +
            " ORDER BY updated_at ASC, task_id ASC LIMIT ?"
        )
        args.append(take)
        with self._connect() as conn:
            rows_raw = conn.execute(sql, args).fetchall()
        has_more = len(rows_raw) > int(limit)
        rows_raw = rows_raw[:int(limit)]
        rows = [_row_to_task(r) for r in rows_raw]
        if rows:
            last = rows[-1]
            next_cursor = f"{last.updated_at:.9f}|{last.task_id}"
        else:
            next_cursor = cursor or ""
        return rows, next_cursor, has_more

    # ---------------------------------------------------------------- 恢复

    def reap_and_recover_running(self, batch_id: str | None = None,
                                  tts_wav_ok: "callable" = None,
                                  reserved_output_verifier: "callable" = None,
                                  staging_cleanup: "callable" = None) -> dict[str, int]:
        """R3+R11-3 启动恢复：分类修复上次异常退出的运行中任务。

        - validating / tts_running → pending（安全清理 staging、清预留位）
        - video_running：**分三种**处理
          1. 预留的正式文件已存在且 reserved_output_verifier(row, path) 返回 (True, meta)
             → 补记 completed（这就是"文件已提交、DB 未提交"的断电恢复）
          2. 预留失败但 tts.wav 完整 → 回 tts_done 重跑视频
          3. 都不行 → 回 pending 从头
        - retry_wait 保留（next_attempt_at 已持久化）
        - **兼容旧版**：`interrupted` 状态也一并重新分类（旧 reap_interrupted 留下的孤儿）
        - `staging_cleanup(staging_dir)` 允许调用方注入白名单清理

        返回：{ tts_recovered_pending, video_recovered_completed,
                video_recovered_tts_done, video_recovered_pending,
                interrupted_recovered }
        """
        stats = {
            "tts_recovered_pending": 0,
            "video_recovered_completed": 0,
            "video_recovered_tts_done": 0,
            "video_recovered_pending": 0,
            "interrupted_recovered": 0,
        }
        now = time.time()

        def _clean(row: TaskRow) -> None:
            if staging_cleanup and row.staging_dir:
                try:
                    staging_cleanup(row.staging_dir)
                except Exception:  # noqa: BLE001
                    pass

        with self._connect() as conn:
            # validating / tts_running → pending
            q = "SELECT * FROM tasks WHERE status IN (?, ?)"
            args: list[Any] = [STATUS_VALIDATING, STATUS_TTS_RUNNING]
            if batch_id:
                q += " AND batch_id=?"; args.append(batch_id)
            rows = conn.execute(q, args).fetchall()
            for r in rows:
                row_obj = _row_to_task(r)
                _clean(row_obj)
                conn.execute(
                    "UPDATE tasks SET status=?, stage='等待恢复',"
                    " reserved_output_path='', staging_dir='', progress=0,"
                    " error_type='', error_detail='',"
                    " updated_at=? WHERE task_id=?",
                    (STATUS_PENDING, now, r["task_id"]),
                )
                stats["tts_recovered_pending"] += 1

            # video_running：分三种
            q = "SELECT * FROM tasks WHERE status=?"
            args = [STATUS_VIDEO_RUNNING]
            if batch_id:
                q += " AND batch_id=?"; args.append(batch_id)
            rows = conn.execute(q, args).fetchall()
            for r in rows:
                row_obj = _row_to_task(r)
                reserved = row_obj.reserved_output_path
                # 情形 1：正式文件已落，DB 未提交
                if reserved and Path(reserved).is_file() and reserved_output_verifier:
                    try:
                        ok, meta = reserved_output_verifier(row_obj, Path(reserved))
                    except Exception:  # noqa: BLE001
                        ok, meta = False, {}
                    if ok:
                        # 单事务式补记 completed
                        conn.execute(
                            "UPDATE tasks SET status=?, output_path=?,"
                            " reserved_output_path='',"
                            " final_duration=?, tts_duration=?, concat_duration=?,"
                            " video_duration=?, encoder_used=?, hw_fallback_used=?,"
                            " progress=100, stage='完成（恢复）',"
                            " error_type='', error_detail='',"
                            " finished_at=?, updated_at=?"
                            " WHERE task_id=?",
                            (STATUS_COMPLETED, reserved,
                             float(meta.get("final_duration") or row_obj.final_duration or 0),
                             float(meta.get("tts_duration") or row_obj.tts_duration or 0),
                             float(meta.get("concat_duration") or row_obj.concat_duration or 0),
                             float(meta.get("video_duration") or row_obj.video_duration or 0),
                             str(meta.get("encoder_used") or row_obj.encoder_used or ""),
                             int(meta.get("hw_fallback_used") or row_obj.hw_fallback_used or 0),
                             now, now, r["task_id"]),
                        )
                        _clean(row_obj)
                        stats["video_recovered_completed"] += 1
                        continue
                # 情形 2：正式文件缺失或校验不过 —— 但 tts.wav 完整 → tts_done
                if tts_wav_ok and tts_wav_ok(row_obj):
                    conn.execute(
                        "UPDATE tasks SET status=?, stage='等待视频池（恢复）',"
                        " reserved_output_path='', progress=0,"
                        " error_type='', error_detail='',"
                        " updated_at=? WHERE task_id=?",
                        (STATUS_TTS_DONE, now, r["task_id"]),
                    )
                    stats["video_recovered_tts_done"] += 1
                    continue
                # 情形 3：都不行 → pending
                _clean(row_obj)
                conn.execute(
                    "UPDATE tasks SET status=?, stage='等待恢复',"
                    " reserved_output_path='', staging_dir='', progress=0,"
                    " error_type='', error_detail='',"
                    " updated_at=? WHERE task_id=?",
                    (STATUS_PENDING, now, r["task_id"]),
                )
                stats["video_recovered_pending"] += 1

            # 兼容旧版 interrupted 孤儿：一律回 pending（安全，走正常重跑）
            q = "SELECT * FROM tasks WHERE status=?"
            args = [STATUS_INTERRUPTED]
            if batch_id:
                q += " AND batch_id=?"; args.append(batch_id)
            rows = conn.execute(q, args).fetchall()
            for r in rows:
                row_obj = _row_to_task(r)
                _clean(row_obj)
                conn.execute(
                    "UPDATE tasks SET status=?, stage='等待恢复',"
                    " reserved_output_path='', staging_dir='', progress=0,"
                    " error_type='', error_detail='',"
                    " updated_at=? WHERE task_id=?",
                    (STATUS_PENDING, now, r["task_id"]),
                )
                stats["interrupted_recovered"] += 1
        return stats

    def complete_task_transactional(self, task_id: str, *,
                                     output_path: str,
                                     final_duration: float,
                                     tts_duration: float,
                                     concat_duration: float,
                                     video_duration: float,
                                     encoder_used: str,
                                     hw_fallback_used: bool,
                                     warnings_to_add: list[str] | None = None) -> None:
        """R11-3 视频完成走单个事务：写 output_path + 清 reservation + 记 warnings +
        编码器/时长/completed 状态。避免"文件已落 / DB 未落"跨步失败面。"""
        now = time.time()
        warnings_to_add = warnings_to_add or []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 读现有 warnings 做去重合并
                cur_row = conn.execute(
                    "SELECT warnings FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                existing = json.loads((cur_row["warnings"] if cur_row else "[]") or "[]")
                if not isinstance(existing, list):
                    existing = []
                for w in warnings_to_add:
                    if w and w not in existing:
                        existing.append(w)
                conn.execute(
                    "UPDATE tasks SET status=?, output_path=?, reserved_output_path='',"
                    " final_duration=?, tts_duration=?, concat_duration=?,"
                    " video_duration=?, encoder_used=?, hw_fallback_used=?,"
                    " progress=100, stage='完成', error_type='', error_detail='',"
                    " warnings=?, finished_at=?, updated_at=? WHERE task_id=?",
                    (STATUS_COMPLETED, output_path,
                     float(final_duration), float(tts_duration),
                     float(concat_duration), float(video_duration),
                     encoder_used, 1 if hw_fallback_used else 0,
                     json.dumps(existing, ensure_ascii=False), now, now, task_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def reap_interrupted(self, batch_id: str | None = None) -> int:
        """旧接口保留（兼容测试）：把 running 全标 interrupted。

        新代码应使用 reap_and_recover_running() 让任务自动重新排队。
        """
        placeholders = ",".join("?" * 3)
        args: list[Any] = [STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING, STATUS_VALIDATING]
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
                " progress=0, next_attempt_at=0, reserved_output_path='',"
                " updated_at=? WHERE task_id=?",
                (new_status, time.time(), task_id),
            )

    def bulk_reset(self, batch_id: str, from_status: str,
                   to_status: str = STATUS_PENDING) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status=?, error_type='', error_detail='',"
                " progress=0, next_attempt_at=0, updated_at=?"
                " WHERE batch_id=? AND status=?",
                (to_status, time.time(), batch_id, from_status),
            )
            return int(cur.rowcount or 0)


def _row_to_task(row: sqlite3.Row) -> TaskRow:
    def _optional(name: str, default):
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    warnings_raw = _optional("warnings", "[]") or "[]"
    try:
        warnings_list = json.loads(warnings_raw)
        if not isinstance(warnings_list, list):
            warnings_list = []
    except json.JSONDecodeError:
        warnings_list = []
    return TaskRow(
        task_id=row["task_id"], batch_id=row["batch_id"],
        excel_row=int(row["excel_row"]),
        input_video=row["input_video"], text=row["text"],
        fingerprint=row["fingerprint"],
        output_path=row["output_path"] or "",
        reserved_output_path=_optional("reserved_output_path", "") or "",
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
        warnings=warnings_list,
        encoder_used=_optional("encoder_used", "") or "",
        hw_fallback_used=int(_optional("hw_fallback_used", 0) or 0),
        video_duration=float(row["video_duration"] or 0),
        tts_duration=float(row["tts_duration"] or 0),
        concat_duration=float(row["concat_duration"] or 0),
        final_duration=float(row["final_duration"] or 0),
        created_at=float(row["created_at"] or 0),
        started_at=float(row["started_at"] or 0),
        finished_at=float(row["finished_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        next_attempt_at=float(_optional("next_attempt_at", 0) or 0),
    )
