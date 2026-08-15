from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model_registry import validate_component_file
from .plugins import ToolPlugin, plugin_catalog, plugin_statuses


ProgressCallback = Callable[[int, int | None], None]


def _ssl_context():
    """HTTPS 用 SSL context——冻结 exe 缺 CA 证书会导致 HF/GitHub 下载失败。
    显式用 certifi 的 cacert.pem（certifi 已在打包 spec 里）。"""
    import ssl
    try:
        import certifi  # type: ignore[import-not-found]
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        try:
            return ssl.create_default_context()
        except Exception:  # noqa: BLE001
            return None


_SSL_CTX = _ssl_context()


@dataclass
class ComponentDownloadResult:
    key: str
    name: str
    target_dir: Path
    downloaded_file: Path
    extracted: bool
    status: dict
    message: str


@dataclass
class VerifiedDownloadResult:
    target: Path
    urls: list[str]
    resumed: bool
    bytes_written: int
    message: str


def find_component(key: str) -> ToolPlugin:
    for item in plugin_catalog():
        if item.key == key:
            return item
    raise KeyError(f"未找到组件：{key}")


def append_part_bytes(part: Path, chunk: bytes) -> int:
    part.parent.mkdir(parents=True, exist_ok=True)
    with part.open("ab") as handle:
        handle.write(chunk)
    return part.stat().st_size


def _content_range_total(value: str) -> int | None:
    if "/" not in value:
        return None
    tail = value.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


def _download_once(url: str, target: Path, progress: ProgressCallback | None = None) -> tuple[bool, bool, int]:
    part = target.with_suffix(target.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    existing = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "ShuiXingJianJi-ComponentDownloader"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    _ctx = _SSL_CTX if url.lower().startswith("https") else None
    with urllib.request.urlopen(request, timeout=60, context=_ctx) as response:
        status = getattr(response, "status", response.getcode())
        restarted_without_range = False
        if existing and status != 206:
            part.unlink(missing_ok=True)
            existing = 0
            restarted_without_range = True
        total_header = response.headers.get("Content-Length")
        total = int(total_header) + existing if total_header and total_header.isdigit() else None
        content_range = response.headers.get("Content-Range")
        if content_range:
            total = _content_range_total(content_range) or total
        done = existing
        if progress:
            progress(done, total)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            done = append_part_bytes(part, chunk)
            if progress:
                progress(done, total)
    return existing > 0, restarted_without_range, part.stat().st_size if part.exists() else 0


def download_verified_file(
    urls: list[str],
    target: Path,
    progress: ProgressCallback | None = None,
    size_bytes: int | None = None,
    sha256: str | None = None,
    manual_target_dir: Path | None = None,
) -> VerifiedDownloadResult:
    if not urls:
        raise RuntimeError("未登记可下载地址。")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    if target.exists() and (size_bytes or sha256):
        validation = validate_component_file(target, size_bytes, sha256)
        if validation.ok:
            return VerifiedDownloadResult(target, urls, False, target.stat().st_size, "已存在且校验通过。")
    target.unlink(missing_ok=True)

    errors: list[str] = []
    resumed = False
    restarted_without_range = False
    bytes_written = 0
    for url in urls:
        for attempt in range(2):
            try:
                did_resume, restarted, bytes_written = _download_once(url, target, progress)
                resumed = resumed or did_resume
                restarted_without_range = restarted_without_range or restarted
                if bytes_written <= 0:
                    part.unlink(missing_ok=True)
                    raise RuntimeError("下载结果为空文件。")
                validation = validate_component_file(part, size_bytes, sha256)
                if not validation.ok:
                    part.unlink(missing_ok=True)
                    target.unlink(missing_ok=True)
                    raise RuntimeError(
                        "文件校验失败，可能下载不完整或源被污染，请重试或手动下载。"
                        f" {validation.message}"
                    )
                target.unlink(missing_ok=True)
                part.replace(target)
                message = "下载完成并校验通过。"
                if restarted_without_range:
                    message = "服务器不支持断点续传，已删除 .part 并重新下载；" + message
                return VerifiedDownloadResult(target, urls, resumed, bytes_written, message)
            except Exception as exc:
                errors.append(f"{url}：{exc}")
                if attempt == 0:
                    time.sleep(3)
                else:
                    part.unlink(missing_ok=True)
    manual_dir = manual_target_dir or target.parent
    raise RuntimeError(
        "下载失败。"
        f"原因摘要：{errors[-1] if errors else '未知错误'}；"
        f"手动下载 URL：{'; '.join(urls)}；"
        f"目标放置目录：{manual_dir}；"
        "放置后点刷新即可识别。"
    )


def _download_name(url: str, key: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(parsed.path).name
    if not name or "." not in name:
        return f"{key}.zip"
    return name


def _status_for(key: str) -> dict:
    for item in plugin_statuses():
        if item.get("key") == key:
            return item
    return {}


def download_component(key: str, progress: ProgressCallback | None = None) -> ComponentDownloadResult:
    component = find_component(key)
    if not component.download_url:
        raise ValueError(f"{component.name} 暂未登记可自动下载地址，请打开 GitHub 页面手动查看。")

    target_dir = component.source_path()
    target_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir = target_dir / "_downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    filename = _download_name(component.download_url, component.key)
    downloaded = downloads_dir / filename

    try:
        download_verified_file([component.download_url], downloaded, progress, manual_target_dir=target_dir)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(
            f"下载失败。手动下载地址：{component.download_url}；目标放置目录：{target_dir}；原因：{exc}"
        ) from exc

    extracted = False
    if zipfile.is_zipfile(downloaded):
        with zipfile.ZipFile(downloaded) as archive:
            archive.extractall(target_dir)
        extracted = True
    elif downloaded.suffix.lower() == ".exe":
        shutil.copy2(downloaded, target_dir / downloaded.name)

    status = _status_for(key)
    ready = bool(status.get("entry_ready") or status.get("python_import_ready") or status.get("path_ready"))
    if ready:
        message = f"{component.name} 已下载并可用。"
    elif component.tier == "reference":
        message = f"{component.name} 源码已就绪，启用仍需编译或安装依赖；请按自检修复建议处理。"
    else:
        message = f"{component.name} 已下载，状态仍以能力自检为准；如仍待启用，请检查可执行文件位置或依赖。"

    return ComponentDownloadResult(
        key=key,
        name=component.name,
        target_dir=target_dir,
        downloaded_file=downloaded,
        extracted=extracted,
        status=status,
        message=message,
    )
