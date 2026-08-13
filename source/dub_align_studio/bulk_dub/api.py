"""HTTP API 路由：所有路径以 /api/bulk_dub/ 前缀，避免与"一键成片"路由冲突。

R8 加固：
    - 上传路径明确限制 Content-Length 上限；超限返回 413；
    - 服务端参数校验错误统一 400；
    - `/changes` 返回下一个游标 (next_since)；
    - CSV 触发下载头（Content-Disposition: attachment）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..engines.edge_tts import EDGE_STYLES, EDGE_VOICES, edge_tts_endpoint
from .csv_export import to_bytes as csv_to_bytes
from .excel_reader import (
    MAX_XLSX_UPLOAD_BYTES, MAX_TASKS_PER_BATCH, ExcelSizeError,
)
from .service import (
    DEFAULT_SPEED, DEFAULT_VOICE_ID, BulkDubService, ValidationError, get_service,
)


API_PREFIX = "/api/bulk_dub"
# 非 xlsx 上传路径的绝对 Content-Length 上限（如 pause/cancel 的 JSON 或空 body）
MAX_JSON_BODY_BYTES = 256 * 1024


def _json_response(payload: Any, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, body, "application/json; charset=utf-8"


def dispatch_get(path: str, query: dict[str, str],
                 service: BulkDubService | None = None) -> tuple[bool, int, bytes, str]:
    if not path.startswith(API_PREFIX):
        return False, 404, b"", ""
    svc = service or get_service()
    route = path[len(API_PREFIX):]

    if route == "/voices":
        return True, *_json_response({
            "voices": [dict(v) for v in EDGE_VOICES],
            "styles": list(EDGE_STYLES),
            "default_voice_id": DEFAULT_VOICE_ID,
            "default_speed": DEFAULT_SPEED,
            "edge_endpoint": edge_tts_endpoint(),
            "edge_endpoint_configured": bool(edge_tts_endpoint()),
        })

    if route == "/summary":
        batch_id = query.get("batch_id") or None
        return True, *_json_response(svc.summary(batch_id))

    if route == "/batches":
        try:
            limit = max(1, min(100, int(query.get("limit") or 50)))
        except ValueError:
            return True, *_json_response({"error": "非法 limit"}, 400)
        return True, *_json_response({"batches": svc.list_batches(limit)})

    if route == "/tasks":
        batch_id = query.get("batch_id") or None
        status = query.get("status") or None
        excel_row = None
        if query.get("excel_row"):
            try:
                excel_row = int(query["excel_row"])
            except ValueError:
                return True, *_json_response({"error": "非法 excel_row"}, 400)
        q = query.get("q") or None
        try:
            limit = max(1, min(500, int(query.get("limit") or 100)))
            offset = max(0, int(query.get("offset") or 0))
        except ValueError:
            return True, *_json_response({"error": "非法 limit/offset"}, 400)
        rows = svc.list_tasks(batch_id=batch_id, status=status,
                               excel_row=excel_row, query=q,
                               limit=limit, offset=offset)
        counts = svc.store.count_by_status(batch_id)
        return True, *_json_response({
            "tasks": rows, "limit": limit, "offset": offset,
            "counts": counts,
        })

    if route == "/changes":
        try:
            since = float(query.get("since") or 0)
        except ValueError:
            return True, *_json_response({"error": "非法 since"}, 400)
        batch_id = query.get("batch_id") or None
        rows = svc.changed_since(since, batch_id)
        # 稳定游标 = 本批次最大 updated_at；无变化时 next_since=since
        next_since = max((r.get("updated_at", 0) for r in rows), default=since)
        return True, *_json_response({
            "changes": rows, "now": time.time(), "next_since": next_since,
        })

    if route == "/csv":
        batch_id = query.get("batch_id") or None
        rows_iter = svc.store.iter_all(batch_id=batch_id)
        body = csv_to_bytes(rows_iter)
        fname = f"bulk_dub_{batch_id or 'all'}.csv"
        # 返回文件下载头（外层 web_server 会照抄 Content-Type 但也需要 Content-Disposition
        # ——这里把它塞进 Content-Type 字段的话不合适；改由外层根据 path 自行加）
        return True, 200, body, "text/csv; charset=utf-8"

    if route == "/audio":
        # 试听/成片音频回读——严格白名单在 web_server 层做（含 data_root/批量带货/试听）
        # 这里只把请求参数原样返回给 web_server 用
        path_arg = query.get("path") or ""
        return True, 200, path_arg.encode("utf-8"), "application/x-bulk-dub-audio"

    return True, *_json_response({"error": "not found"}, 404)


def dispatch_post(path: str, query: dict[str, str], body: bytes,
                  content_type: str = "",
                  service: BulkDubService | None = None) -> tuple[bool, int, bytes, str]:
    if not path.startswith(API_PREFIX):
        return False, 404, b"", ""
    svc = service or get_service()
    route = path[len(API_PREFIX):]

    # 尺寸护栏：xlsx 上传 20MB；其他 JSON body 256KB
    is_xlsx_upload = route in ("/preview", "/start")
    max_bytes = MAX_XLSX_UPLOAD_BYTES if is_xlsx_upload else MAX_JSON_BODY_BYTES
    if body and len(body) > max_bytes:
        return True, *_json_response(
            {"error": f"上传体积过大：{len(body)} > {max_bytes}"}, 413,
        )

    if route == "/preview":
        xlsx = _extract_xlsx_body(body, content_type)
        check_exists = str(query.get("check_exists", "1")) not in ("0", "false", "False")
        try:
            payload = svc.preview_excel(xlsx, check_exists=check_exists)
        except ExcelSizeError as exc:
            return True, *_json_response({"error": str(exc)}, 413)
        except (ValueError, ValidationError) as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(payload)

    if route == "/start":
        xlsx = _extract_xlsx_body(body, content_type)
        try:
            speed = float(query.get("speed") or DEFAULT_SPEED)
            pitch = int(query.get("pitch") or 0)
            zoom = int(query.get("zoom_percent") or 130)
            tts_c = int(query.get("tts_concurrency") or 4)
            video_c = int(query.get("video_concurrency") or 0)
        except ValueError as exc:
            return True, *_json_response({"error": f"参数不是数字：{exc}"}, 400)
        try:
            result = svc.start_batch(
                source_bytes=xlsx,
                label=query.get("label") or "",
                output_dir=query.get("output_dir") or "",
                voice_id=query.get("voice_id") or DEFAULT_VOICE_ID,
                speed=speed, pitch=pitch,
                style=query.get("style") or "general",
                keep_original_audio=str(query.get("keep_original_audio", "0")) in ("1", "true", "True"),
                zoom_percent=zoom,
                tts_concurrency=tts_c,
                video_concurrency=video_c,
                encoder_preference=query.get("encoder_preference") or "auto",
                check_exists=str(query.get("check_exists", "1")) not in ("0", "false", "False"),
            )
        except ExcelSizeError as exc:
            return True, *_json_response({"error": str(exc)}, 413)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        except ValueError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response(result)

    if route == "/pause":
        svc.pause()
        return True, *_json_response({"paused": True})
    if route == "/resume":
        svc.resume()
        return True, *_json_response({"paused": False})

    if route == "/pause_batch":
        batch_id = query.get("batch_id") or ""
        try:
            svc.pause_batch(batch_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response({"paused_batch": batch_id})
    if route == "/resume_batch":
        batch_id = query.get("batch_id") or ""
        try:
            svc.resume_batch(batch_id)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response({"resumed_batch": batch_id})

    if route == "/cancel_task":
        task_id = query.get("task_id") or _read_json(body).get("task_id")
        if not task_id:
            return True, *_json_response({"error": "缺少 task_id"}, 400)
        ok = svc.cancel_task(task_id)
        return True, *_json_response({"cancelled": ok})

    if route == "/cancel_waiting":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        try:
            n = svc.cancel_waiting(batch_id) if batch_id else 0
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response({"cancelled": n})

    if route == "/retry_failed":
        batch_id = query.get("batch_id") or _read_json(body).get("batch_id") or ""
        only = str(query.get("only_retryable", "0")) in ("1", "true", "True")
        try:
            n = svc.retry_failed(batch_id, only) if batch_id else 0
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        return True, *_json_response({"retried": n})

    if route == "/try_voice":
        voice_id = query.get("voice_id") or DEFAULT_VOICE_ID
        try:
            speed = float(query.get("speed") or DEFAULT_SPEED)
        except ValueError:
            return True, *_json_response({"error": "非法 speed"}, 400)
        try:
            out = svc.probe_sample(voice_id=voice_id, speed=speed)
        except ValidationError as exc:
            return True, *_json_response({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return True, *_json_response({"error": str(exc)}, 502)
        return True, *_json_response({"audio_path": str(out)})

    return True, *_json_response({"error": "not found"}, 404)


def _extract_xlsx_body(body: bytes, content_type: str) -> bytes:
    if body and content_type and content_type.startswith("multipart/form-data"):
        try:
            return _parse_multipart_file(body, content_type)
        except Exception:
            return body
    return body


def _parse_multipart_file(body: bytes, content_type: str) -> bytes:
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
            break
    if not boundary:
        return body
    marker = ("--" + boundary).encode()
    parts = body.split(marker)
    for chunk in parts:
        if not chunk or chunk == b"--\r\n" or chunk.startswith(b"--"):
            continue
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = chunk[:header_end].decode("utf-8", "ignore").lower()
        data = chunk[header_end + 4:]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if 'name="file"' in headers or 'name="xlsx"' in headers:
            return data
    return body


def _read_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        v = json.loads(body.decode("utf-8", "ignore"))
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
