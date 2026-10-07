"""xlsx 文案读取：纯标准库解析（zipfile + xml.etree），零第三方依赖。

需求（用户 2026-07-22）：从表格读文案——默认 B 列，可自选其他列；一格一句。
只解析第一个工作表；支持共享字符串（t="s"）与内联字符串（t="inlineStr"）；
跳过空格子；按行号升序返回。
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")


def read_column(source: bytes | str | Path, column: str = "B") -> list[str]:
    """读取第一个工作表指定列的全部非空文本（按行号升序）。

    source 可为 xlsx 文件路径或原始字节（网页上传）。列名不区分大小写。
    """
    column = (column or "B").strip().upper()
    return [cells[column] for _row, cells in read_columns(source, (column,))]


def read_columns(source: bytes | str | Path,
                 columns: Iterable[str], *, strip: bool = True) -> list[tuple[int, dict[str, str]]]:
    """Read selected columns together, preserving Excel row numbers and cell gaps.

    Column keys are uppercase; blank cells and rows without selected text are omitted.
    Set strip=False to preserve nonblank cell text exactly as stored.
    """
    selected = {column.strip().upper() for column in columns}
    for column in selected:
        if not re.fullmatch(r"[A-Z]{1,3}", column):
            raise ValueError(f"列名无效：{column}（应为 A、B、C… 形式）")

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        bundle = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 xlsx 文件（无法按 zip 打开）。") from exc

    with bundle:
        shared = _shared_strings(bundle)
        sheet_name = _first_sheet_path(bundle)
        try:
            root = ET.fromstring(bundle.read(sheet_name))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError(f"无法解析工作表：{sheet_name}") from exc

        rows: dict[int, dict[str, str]] = {}
        for cell in root.iterfind(".//m:sheetData/m:row/m:c", _NS):
            ref = cell.get("r") or ""
            match = _CELL_REF.match(ref)
            if not match or match.group(1) not in selected:
                continue
            text = _cell_text(cell, shared, strip=strip)
            if text.strip():
                rows.setdefault(int(match.group(2)), {})[match.group(1)] = text
        return sorted(rows.items())


def _first_sheet_path(bundle: zipfile.ZipFile) -> str:
    if "xl/workbook.xml" in bundle.namelist():
        try:
            workbook = ET.fromstring(bundle.read("xl/workbook.xml"))
            relationships = ET.fromstring(bundle.read("xl/_rels/workbook.xml.rels"))
            targets = {item.get("Id"): item.get("Target") for item in relationships
                       if (item.get("Type") or "").endswith("/worksheet")}
            for sheet in workbook.iterfind("m:sheets/m:sheet", _NS):
                relationship = sheet.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
                )
                target = targets.get(relationship)
                if target:
                    return posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("无法解析工作簿的工作表顺序。") from exc
        raise ValueError("xlsx 内没有工作表。")
    # Compatibility for the minimal worksheet-only workbooks accepted previously.
    names = [n for n in bundle.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    if not names:
        raise ValueError("xlsx 内没有工作表。")
    return sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))[0]


def _shared_strings(bundle: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(bundle.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: list[str] = []
    for item in root.iterfind("m:si", _NS):
        strings.append("".join(t.text or "" for t in item.iterfind(".//m:t", _NS)))
    return strings


def _cell_text(cell: ET.Element, shared: list[str], *, strip: bool = True) -> str:
    kind = cell.get("t") or ""
    if kind == "s":
        value = cell.find("m:v", _NS)
        try:
            text = shared[int(value.text)] if value is not None else ""
        except (ValueError, IndexError, TypeError):
            return ""
    elif kind == "inlineStr":
        text = "".join(t.text or "" for t in cell.iterfind(".//m:t", _NS))
    else:
        value = cell.find("m:v", _NS)
        text = (value.text or "") if value is not None else ""
    return text.strip() if strip else text
