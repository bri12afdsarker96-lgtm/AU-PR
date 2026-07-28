"""批量任务可靠性：断点续跑 + 失败重试 + 完成汇总。

量产时中途失败重跑不必从头：把每个任务的完成/失败状态落盘，续跑时跳过已完成。
纯逻辑（状态文件 JSON），可单元测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BatchState:
    state_path: Path
    done: dict[str, str] = field(default_factory=dict)  # task_id -> 结果摘要
    failed: dict[str, str] = field(default_factory=dict)  # task_id -> 错误摘要

    @classmethod
    def load(cls, state_path: str | Path) -> "BatchState":
        path = Path(state_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path, dict(data.get("done", {})), dict(data.get("failed", {})))
        except Exception:
            return cls(path)

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"done": self.done, "failed": self.failed}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_done(self, task_id: str) -> bool:
        return task_id in self.done

    def mark_done(self, task_id: str, summary: str = "") -> None:
        self.done[task_id] = summary
        self.failed.pop(task_id, None)
        self._save()

    def mark_failed(self, task_id: str, error: str = "") -> None:
        self.failed[task_id] = error
        self._save()

    def pending(self, task_ids: list[str]) -> list[str]:
        """返回还需处理的任务（已完成的跳过；失败的会重试）。"""
        return [tid for tid in task_ids if tid not in self.done]

    def summary(self, total: int) -> dict[str, int]:
        return {
            "total": total,
            "done": len(self.done),
            "failed": len(self.failed),
            "pending": max(0, total - len(self.done)),
        }


def run_resumable(
    task_ids: list[str],
    handler,
    state_path: str | Path,
    retry_failed: bool = True,
) -> BatchState:
    """按状态文件断点续跑：跳过已完成，逐个执行 handler(task_id)。

    handler 返回摘要字符串视为成功；抛异常记为失败并继续下一个。
    retry_failed=True 时上次失败的会重试（因为只跳过 done）。
    """
    state = BatchState.load(state_path)
    for task_id in task_ids:
        if state.is_done(task_id):
            continue
        try:
            summary = handler(task_id)
            state.mark_done(task_id, str(summary or ""))
        except Exception as exc:  # noqa: BLE001
            state.mark_failed(task_id, str(exc))
    return state
