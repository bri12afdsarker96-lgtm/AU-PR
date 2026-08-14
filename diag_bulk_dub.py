# -*- coding: utf-8 -*-
"""Bulk Dub failure diagnostic (ASCII filename + ASCII bat-friendly).

Usage:
  * Windows: double-click `diag_bulk_dub.bat`
  * CLI:     python diag_bulk_dub.py

Locates <data_root>/批量带货/queue.sqlite3 via studio_settings.data_root(),
dumps each failed task's error_type / error_detail / stage, probes each
input_video path on disk, and writes `diag_bulk_dub_log.txt`.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


# ------------------------------------------------------------------
# Force stdout UTF-8 so Chinese error_detail from DB prints cleanly
# even before chcp 65001 fully applies.
# ------------------------------------------------------------------
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


HERE = Path(__file__).resolve().parent

# Try to find dub_align_studio package (installed or in ./source)
for candidate in (HERE, HERE / "source"):
    if (candidate / "dub_align_studio").is_dir():
        sys.path.insert(0, str(candidate))
        break

DATA_ROOT: Path
try:
    from dub_align_studio import settings as studio_settings  # noqa: E402
    DATA_ROOT = studio_settings.data_root()
except Exception as exc:  # noqa: BLE001
    print(f"[warn] cannot import studio_settings: {exc}")
    print("[warn] fallback: DATA_ROOT = <script_dir>/水星配音数据")
    DATA_ROOT = HERE / "水星配音数据"


DB_PATH = DATA_ROOT / "批量带货" / "queue.sqlite3"
LOG_PATH = HERE / "diag_bulk_dub_log.txt"


class Tee:
    def __init__(self, log_path: Path) -> None:
        self.f = log_path.open("w", encoding="utf-8", newline="\n")

    def line(self, s: str = "") -> None:
        print(s)
        self.f.write(s + "\n")
        self.f.flush()

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:  # noqa: BLE001
            pass


def _probe(raw: str) -> dict:
    info: dict = {"raw": raw, "exists": False, "detail": ""}
    if not raw:
        info["detail"] = "empty path"
        return info
    try:
        p = Path(raw)
    except Exception as exc:  # noqa: BLE001
        info["detail"] = f"path parse error: {exc}"
        return info
    try:
        info["exists"] = p.exists()
    except OSError as exc:
        info["detail"] = f"stat error: {exc}"
        return info
    if not info["exists"]:
        hint = "no parent (relative or bare filename)"
        if p.is_absolute():
            hint = (
                "parent exists" if p.parent.exists()
                else f"parent missing: {p.parent}"
            )
        info["detail"] = f"NOT FOUND ({hint})"
        return info
    try:
        st = p.stat()
    except OSError as exc:
        info["detail"] = f"stat post-check error: {exc}"
        return info
    info["is_file"] = p.is_file()
    info["is_dir"] = p.is_dir()
    info["size_bytes"] = st.st_size
    if p.is_dir():
        info["detail"] = "IS A DIRECTORY (not a file)"
    elif st.st_size == 0:
        info["detail"] = "size=0"
    else:
        info["detail"] = f"OK size={st.st_size}"
    return info


def main() -> int:
    tee = Tee(LOG_PATH)
    try:
        tee.line("=" * 70)
        tee.line("Bulk Dub failure diagnostic")
        tee.line(f"time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        tee.line(f"data_root : {DATA_ROOT}")
        tee.line(f"db path   : {DB_PATH}")
        tee.line(f"db exists : {DB_PATH.exists()}")
        if not DB_PATH.exists():
            tee.line("")
            tee.line("[FATAL] queue.sqlite3 not found.")
            tee.line("  1) Wrong data_root? Check the software settings.")
            tee.line("  2) Or edit ~/.dub_align_studio/settings.json 'data_root' key.")
            return 1

        tee.line("=" * 70)

        with sqlite3.connect(str(DB_PATH), timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row

            # 1. status counts
            tee.line("[1] status counts")
            tee.line("-" * 70)
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "GROUP BY status ORDER BY n DESC"
            ).fetchall()
            if not counts:
                tee.line("  (tasks table empty)")
            for r in counts:
                tee.line(f"  {r['status']:22s}  {r['n']}")
            tee.line("")

            # 2. recent batches
            tee.line("[2] recent 5 batches")
            tee.line("-" * 70)
            batches = conn.execute(
                "SELECT batch_id, label, output_dir, created_at "
                "FROM batches ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            for b in batches:
                ts = datetime.fromtimestamp(
                    b["created_at"]
                ).strftime("%Y-%m-%d %H:%M:%S")
                tee.line(
                    f"  {b['batch_id']} | label={b['label']!r} | "
                    f"out={b['output_dir']} | {ts}"
                )
            tee.line("")

            # 3. failed task details
            tee.line("[3] failed tasks (status=failed)")
            tee.line("-" * 70)
            failed = conn.execute(
                "SELECT task_id, batch_id, excel_row, input_video, "
                "error_type, error_detail, stage "
                "FROM tasks WHERE status='failed' "
                "ORDER BY excel_row"
            ).fetchall()

            if not failed:
                tee.line("  no failed tasks")
            else:
                tee.line(f"  {len(failed)} failed tasks")
                tee.line("")
                errtype_ctr: Counter = Counter()
                errdetail_ctr: Counter = Counter()
                missing: set = set()

                for row in failed:
                    et = row["error_type"] or "(empty error_type)"
                    ed = row["error_detail"] or "(empty error_detail)"
                    errtype_ctr[et] += 1
                    errdetail_ctr[ed[:120]] += 1

                    probe = _probe(row["input_video"] or "")
                    mark = "OK" if probe["exists"] else "MISS"
                    tee.line(
                        f"  row {row['excel_row']:>4} | "
                        f"task={row['task_id'][:8]}... | error_type={et}"
                    )
                    tee.line(
                        f"        input_video[{mark}] = {row['input_video']!r}"
                    )
                    tee.line(f"        disk    : {probe['detail']}")
                    tee.line(f"        stage   : {row['stage']!r}")
                    tee.line(f"        detail  : {ed}")
                    tee.line("")

                    if not probe["exists"]:
                        missing.add(row["input_video"] or "")

                tee.line("-" * 70)
                tee.line("[4] error_type distribution")
                for et, n in errtype_ctr.most_common():
                    tee.line(f"  {n:>4}  {et}")
                tee.line("")

                tee.line("[5] error_detail distribution (first 120 chars)")
                for ed, n in errdetail_ctr.most_common(10):
                    tee.line(f"  {n:>4}  {ed}")
                tee.line("")

                if missing:
                    tee.line("[6] missing video paths (unique)")
                    tee.line("-" * 70)
                    for mv in sorted(missing):
                        tee.line(f"  {mv}")
                    tee.line("")

        tee.line("=" * 70)
        tee.line(f"log written: {LOG_PATH}")
        tee.line("Send this log (or a screenshot) for triage.")
    finally:
        tee.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
