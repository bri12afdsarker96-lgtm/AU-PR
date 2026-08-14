"""Excel 批量带货配音（独立子系统）。

设计要点：
    - **独立包**：不使用一键成片的单例 JOB / GEN_QUEUE，也不共享其锁；避免两个页面
      互相覆盖进度和结果。
    - **持久任务队列**：SQLite WAL；页面关闭 / 应用重启后可继续未完成任务。
    - **两独立池**：TTS 池（默认 4）与 视频池（默认 max(1,min(4,CPU/4))）互不占用。
    - **短者裁剪**：最终成片时长 = min(拼接后视频, TTS 旁白)，禁止对 Edge 结果二次变速。
    - **首版**：单机 SQLite 队列（不宣称多机共享）；架构声称"支持万级"，不吹嘘吞吐。

导入侧仅通过 `bulk_dub.api.register(...)` 挂载到现有 web_server；核心逻辑在
`store / excel_reader / fingerprint / ffmpeg_pipeline / scheduler` 中，可独立测试。
"""

from __future__ import annotations

__all__ = [
    "store",
    "excel_reader",
    "fingerprint",
    "ffmpeg_pipeline",
    "hw_encoder",
    "scheduler",
    "api",
]
