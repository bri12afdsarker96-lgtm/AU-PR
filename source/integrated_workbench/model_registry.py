from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import DIR_CONFIG
from .plugins import vendor_root


MODEL_SOURCE_OVERRIDE = "模型源.json"

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "whisper_cli": {
        "name": "whisper-cli",
        "filename": "whisper-bin-x64.zip",
        "size_bytes": 7_982_101,
        "sha256": "7d8be46ecd31828e1eb7a2ecdd0d6b314feafd82163038ab6092594b0a063539",
        "target_file": "build/bin/Release/whisper-cli.exe",
        "urls": [
            "https://github.com/ggml-org/whisper.cpp/releases/download/v1.9.1/whisper-bin-x64.zip",
        ],
        "source_note": "GitHub Releases API digest for ggml-org/whisper.cpp v1.9.1 asset whisper-bin-x64.zip.",
    },
    "tiny": {
        "name": "tiny",
        "filename": "ggml-tiny.bin",
        "size_bytes": 77_691_713,
        "sha256": "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
        "urls": [
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin?download=true",
        ],
        "source_note": "Hugging Face ggerganov/whisper.cpp LFS pointer oid and size.",
    },
    "base": {
        "name": "base",
        "filename": "ggml-base.bin",
        "size_bytes": 147_951_465,
        "sha256": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
        "urls": [
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin?download=true",
        ],
        "source_note": "Hugging Face ggerganov/whisper.cpp LFS pointer oid and size.",
    },
    "small": {
        "name": "small",
        "filename": "ggml-small.bin",
        "size_bytes": 487_601_967,
        "sha256": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        "urls": [
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin?download=true",
        ],
        "source_note": "Hugging Face ggerganov/whisper.cpp LFS pointer oid and size.",
    },
}


@dataclass
class FileValidation:
    ok: bool
    state: str
    message: str
    size_bytes: int = 0
    sha256: str = ""


@dataclass
class ModelFileStatus:
    key: str
    name: str
    filename: str
    path: Path
    state: str
    label: str
    level: str
    detail: str
    size_bytes: int
    sha256: str

    @property
    def usable(self) -> bool:
        return self.state == "ok"


def whisper_root() -> Path:
    return vendor_root() / "whisper.cpp"


def whisper_models_dir() -> Path:
    return whisper_root() / "models"


def whisper_runtime_downloads_dir() -> Path:
    return whisper_root() / "runtime_downloads"


def whisper_cli_path() -> Path | None:
    entry = Path(str(MODEL_REGISTRY["whisper_cli"]["target_file"]))
    candidate = whisper_root() / entry
    if candidate.exists():
        return candidate
    found = shutil.which("whisper-cli") or shutil.which("whisper-cli.exe")
    return Path(found) if found else None


def model_path(model_key: str) -> Path:
    entry = registry_entry(model_key)
    return whisper_models_dir() / str(entry["filename"])


def runtime_zip_path() -> Path:
    return whisper_runtime_downloads_dir() / str(MODEL_REGISTRY["whisper_cli"]["filename"])


def registry_entry(key: str, project_root: str | Path | None = None) -> dict[str, Any]:
    if key not in MODEL_REGISTRY:
        raise KeyError(key)
    entry = dict(MODEL_REGISTRY[key])
    overrides = load_model_source_overrides(project_root)
    if key in overrides:
        entry["urls"] = overrides[key]
    return entry


def load_model_source_overrides(project_root: str | Path | None) -> dict[str, list[str]]:
    if not project_root:
        return {}
    path = Path(project_root) / DIR_CONFIG / MODEL_SOURCE_OVERRIDE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in payload.items():
        if key not in MODEL_REGISTRY:
            continue
        if isinstance(value, str) and value.strip():
            result[key] = [value.strip()]
        elif isinstance(value, list):
            urls = [str(item).strip() for item in value if str(item).strip()]
            if urls:
                result[key] = urls
    return result


def validate_component_file(path: Path, size_bytes: int | None = None, sha256: str | None = None) -> FileValidation:
    if not path.exists():
        return FileValidation(False, "missing", "文件不存在。")
    actual_size = path.stat().st_size
    if size_bytes is not None and actual_size != size_bytes:
        return FileValidation(False, "damaged", f"文件大小不符：{actual_size} != {size_bytes}", actual_size)
    actual_sha = sha256_file(path) if sha256 else ""
    if sha256 and actual_sha.lower() != sha256.lower():
        return FileValidation(False, "damaged", "sha256 校验失败。", actual_size, actual_sha)
    return FileValidation(True, "ok", "校验通过。", actual_size, actual_sha)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_status(project_root: str | Path | None = None) -> ModelFileStatus:
    entry = registry_entry("whisper_cli", project_root)
    exe = whisper_cli_path()
    zip_file = runtime_zip_path()
    if not exe:
        return ModelFileStatus(
            "whisper_cli",
            str(entry["name"]),
            str(entry["filename"]),
            zip_file,
            "missing",
            "未下载",
            "warning",
            "缺少 whisper-cli.exe。",
            int(entry["size_bytes"]),
            str(entry["sha256"]),
        )
    validation = validate_component_file(zip_file, int(entry["size_bytes"]), str(entry["sha256"]))
    if zip_file.exists() and not validation.ok:
        return ModelFileStatus(
            "whisper_cli",
            str(entry["name"]),
            str(entry["filename"]),
            zip_file,
            "damaged",
            "文件损坏",
            "danger",
            validation.message,
            int(entry["size_bytes"]),
            str(entry["sha256"]),
        )
    return ModelFileStatus(
        "whisper_cli",
        str(entry["name"]),
        str(entry["filename"]),
        exe,
        "ok",
        "校验通过·可用",
        "success",
        "whisper-cli.exe 存在；下载包 sha 在下载时校验。",
        int(entry["size_bytes"]),
        str(entry["sha256"]),
    )


def model_status(model_key: str, project_root: str | Path | None = None) -> ModelFileStatus:
    entry = registry_entry(model_key, project_root)
    path = model_path(model_key)
    validation = validate_component_file(path, int(entry["size_bytes"]), str(entry["sha256"]))
    if validation.state == "missing":
        return ModelFileStatus(
            model_key,
            str(entry["name"]),
            str(entry["filename"]),
            path,
            "missing",
            "未下载",
            "warning",
            validation.message,
            int(entry["size_bytes"]),
            str(entry["sha256"]),
        )
    if not validation.ok:
        return ModelFileStatus(
            model_key,
            str(entry["name"]),
            str(entry["filename"]),
            path,
            "damaged",
            "文件损坏",
            "danger",
            validation.message,
            int(entry["size_bytes"]),
            str(entry["sha256"]),
        )
    return ModelFileStatus(
        model_key,
        str(entry["name"]),
        str(entry["filename"]),
        path,
        "ok",
        "校验通过·可用",
        "success",
        validation.message,
        int(entry["size_bytes"]),
        str(entry["sha256"]),
    )


def model_library_statuses(project_root: str | Path | None = None) -> list[ModelFileStatus]:
    return [runtime_status(project_root), *(model_status(key, project_root) for key in ["tiny", "base", "small"])]


def verified_model_statuses(project_root: str | Path | None = None) -> list[ModelFileStatus]:
    return [status for status in (model_status(key, project_root) for key in ["small", "base", "tiny"]) if status.usable]


def best_verified_model(project_root: str | Path | None = None) -> Path | None:
    statuses = verified_model_statuses(project_root)
    return statuses[0].path if statuses else None


def whisper_available(project_root: str | Path | None = None) -> tuple[bool, str]:
    runtime = runtime_status(project_root)
    models = verified_model_statuses(project_root)
    if runtime.usable and models:
        return True, f"whisper-cli 可用；模型 {models[0].name} 校验通过。"
    missing: list[str] = []
    if not runtime.usable:
        missing.append(runtime.detail)
    if not models:
        missing.append("未发现 sha 校验通过的 ggml 模型。")
    return False, "；".join(missing)
