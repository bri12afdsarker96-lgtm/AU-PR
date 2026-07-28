"""我的预设：把常用的一整套生产配置存起来，下次一键调用。

赛道 + 画面比例 + 去重强度 + 导出参数一套存为命名预设，持久化到用户目录。
纯逻辑，可单元测试。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ProductionPreset:
    name: str
    genre: str = "影视解说"
    aspect_ratio: str = "9:16 竖屏"
    dedup_level: str = "标准"
    resolution: str = "1080P"
    fmt: str = "MP4"
    codec: str = "H.264"
    fps: int = 30
    audio_match_mode: str = "裁剪多余画面"


def _settings_path() -> Path:
    return Path.home() / ".shuixing" / "presets.json"


def load_presets() -> dict[str, ProductionPreset]:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
        items = data.get("presets", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}
    presets: dict[str, ProductionPreset] = {}
    for name, payload in items.items():
        if isinstance(payload, dict):
            try:
                presets[name] = ProductionPreset(name=name, **{k: v for k, v in payload.items() if k != "name"})
            except TypeError:
                continue
    return presets


def save_preset(preset: ProductionPreset) -> None:
    if not preset.name.strip():
        raise ValueError("预设名称不能为空。")
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    presets = load_presets()
    presets[preset.name] = preset
    _write(presets)


def delete_preset(name: str) -> None:
    presets = load_presets()
    if name in presets:
        del presets[name]
        _write(presets)


def get_preset(name: str) -> ProductionPreset | None:
    return load_presets().get(name)


def preset_names() -> list[str]:
    return sorted(load_presets().keys())


def _write(presets: dict[str, ProductionPreset]) -> None:
    payload = {"presets": {name: asdict(preset) for name, preset in presets.items()}}
    _settings_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
