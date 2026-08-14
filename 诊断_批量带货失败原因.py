# -*- coding: utf-8 -*-
"""批量带货配音——失败原因诊断脚本

跑法：
  * Windows：双击「诊断_批量带货失败原因.bat」
  * 或命令行：python 诊断_批量带货失败原因.py

功能：
  1. 定位 queue.sqlite3（走软件自己的 settings.data_root()）
  2. 列出**每条失败任务的具体原因**（error_type / error_detail / stage）
  3. 对每个 input_video 路径**实地检查**：
        - 文件是否存在
        - 是文件还是目录 / 符号链接
        - 大小、只读、扩展名
  4. 汇总"失败原因分布"和"缺失文件唯一列表"
  5. 输出到 `诊断_批量带货失败原因_日志.txt`（UTF-8），同时打印到控制台
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


# ------------------------------------------------------------------
# 让脚本能找到软件源码里的 settings（用来定位 data_root）
# ------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE / "source"):
    src = candidate / "source" if (candidate / "source").is_dir() else candidate
    if (src / "dub_align_studio").is_dir():
        sys.path.insert(0, str(src))
        break

DATA_ROOT: Path | None = None
try:
    from dub_align_studio import settings as studio_settings  # noqa: E402
    DATA_ROOT = studio_settings.data_root()
except Exception as exc:  # noqa: BLE001
    print(f"[warn] 无法导入 studio_settings：{exc}")
    print("[warn] 回退：假设 data_root = <脚本目录>/水星配音数据")
    DATA_ROOT = HERE / "水星配音数据"


DB_PATH = DATA_ROOT / "批量带货" / "queue.sqlite3"
LOG_PATH = HERE / "诊断_批量带货失败原因_日志.txt"


class Tee:
    """同时写文件和 stdout。"""

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


def _probe_video_path(raw: str) -> dict:
    """检查 Excel A 列填的视频路径在磁盘上的真实状态。"""
    info: dict = {"raw": raw, "exists": False, "detail": ""}
    if not raw:
        info["detail"] = "路径为空字符串"
        return info
    try:
        p = Path(raw)
    except Exception as exc:  # noqa: BLE001
        info["detail"] = f"路径解析异常：{exc}"
        return info
    try:
        info["exists"] = p.exists()
    except OSError as exc:
        info["detail"] = f"stat 异常：{exc}"
        return info
    if not info["exists"]:
        # 尝试猜猜看是不是仅文件名（没写完整路径）
        parent_hint = "无父目录（可能是相对路径 or 仅文件名）"
        if p.is_absolute():
            parent_hint = (
                "父目录存在" if p.parent.exists()
                else f"父目录也不存在：{p.parent}"
            )
        info["detail"] = f"文件不存在（{parent_hint}）"
        return info
    try:
        st = p.stat()
    except OSError as exc:
        info["detail"] = f"stat 后异常：{exc}"
        return info
    info["is_file"] = p.is_file()
    info["is_dir"] = p.is_dir()
    info["is_symlink"] = p.is_symlink()
    info["size_bytes"] = st.st_size
    info["ext"] = p.suffix.lower()
    if p.is_dir():
        info["detail"] = "路径指向的是目录，不是文件"
    elif st.st_size == 0:
        info["detail"] = "文件存在但大小为 0"
    else:
        info["detail"] = "OK"
    return info


def main() -> int:
    tee = Tee(LOG_PATH)
    try:
        tee.line("=" * 70)
        tee.line(f"批量带货 · 失败诊断")
        tee.line(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        tee.line(f"data_root：{DATA_ROOT}")
        tee.line(f"DB 路径：{DB_PATH}")
        tee.line(f"DB 存在：{DB_PATH.exists()}")
        if not DB_PATH.exists():
            tee.line("")
            tee.line("[FATAL] queue.sqlite3 找不到。")
            tee.line("  1) 确认软件是否装在别的目录（换机后 data_root 变了？）")
            tee.line("  2) 打开软件 → 设置 → 查看真实数据总目录")
            tee.line("  3) 手动把该目录填到 ~/.dub_align_studio/settings.json 的 data_root")
            return 1

        tee.line("=" * 70)

        with sqlite3.connect(str(DB_PATH), timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            # --- 1. 全库状态概览 ---
            tee.line("【1】全库状态计数")
            tee.line("-" * 70)
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks "
                "GROUP BY status ORDER BY n DESC"
            ).fetchall()
            if not counts:
                tee.line("  (tasks 表为空 —— 还没跑过任何批次)")
            for r in counts:
                tee.line(f"  {r['status']:22s}  {r['n']}")
            tee.line("")

            # --- 2. 批次列表 ---
            tee.line("【2】最近 5 个批次")
            tee.line("-" * 70)
            batches = conn.execute(
                "SELECT batch_id, label, output_dir, created_at "
                "FROM batches ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            for b in batches:
                ts = datetime.fromtimestamp(b["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
                tee.line(
                    f"  {b['batch_id']} · {b['label']!r} · "
                    f"out={b['output_dir']} · {ts}"
                )
            tee.line("")

            # --- 3. 失败任务明细 ---
            tee.line("【3】失败任务明细（status=failed）")
            tee.line("-" * 70)
            failed = conn.execute(
                "SELECT task_id, batch_id, excel_row, input_video, "
                "error_type, error_detail, stage "
                "FROM tasks WHERE status='failed' "
                "ORDER BY excel_row"
            ).fetchall()

            if not failed:
                tee.line("  没有失败任务 🎉")
            else:
                tee.line(f"  共 {len(failed)} 条失败任务")
                tee.line("")
                errtype_counter: Counter = Counter()
                errdetail_counter: Counter = Counter()
                missing_videos: set = set()

                for row in failed:
                    et = row["error_type"] or "(空 error_type)"
                    ed = row["error_detail"] or "(空 error_detail)"
                    errtype_counter[et] += 1
                    errdetail_counter[ed[:80]] += 1

                    probe = _probe_video_path(row["input_video"] or "")
                    exists_mark = "✓" if probe["exists"] else "✗"
                    tee.line(
                        f"  行 {row['excel_row']:>4} · task={row['task_id'][:8]}… · "
                        f"error_type={et}"
                    )
                    tee.line(
                        f"        input_video[{exists_mark}] = {row['input_video']!r}"
                    )
                    tee.line(f"        磁盘检查：{probe['detail']}")
                    tee.line(f"        stage    = {row['stage']!r}")
                    tee.line(f"        detail   = {ed}")
                    tee.line("")

                    if not probe["exists"]:
                        missing_videos.add(row["input_video"] or "")

                tee.line("-" * 70)
                tee.line("【4】error_type 分布")
                for et, n in errtype_counter.most_common():
                    tee.line(f"  {n:>4}  {et}")
                tee.line("")

                tee.line("【5】error_detail 分布（前 80 字）")
                for ed, n in errdetail_counter.most_common(10):
                    tee.line(f"  {n:>4}  {ed}")
                tee.line("")

                if missing_videos:
                    tee.line("【6】不存在的视频路径（唯一）")
                    tee.line("-" * 70)
                    for mv in sorted(missing_videos):
                        tee.line(f"  {mv}")
                    tee.line("")
                    tee.line("  ⚠ 大部分 excel_invalid 都来自这里：Excel A 列写的路径")
                    tee.line("    在这台机器上找不到。修法二选一：")
                    tee.line("    A) 把视频文件复制到 Excel 里写的绝对路径")
                    tee.line("    B) 修改 Excel A 列，改成本机的真实路径（推荐用绝对路径）")
                    tee.line("       然后重新「预览 + 开始批量处理」")
                    tee.line("")

        tee.line("=" * 70)
        tee.line(f"日志已写入：{LOG_PATH}")
        tee.line("把这个日志文件截图或发给协助方就能一眼看到根因。")
    finally:
        tee.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
