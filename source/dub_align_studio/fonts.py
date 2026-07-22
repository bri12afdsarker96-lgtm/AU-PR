"""字体库：字幕/文本框可选字体——下载常用开源字体 + 用户自行放入 + 渲染解析。

需求（用户 2026-07-22 第三轮）：
    - 字幕与文本框字体均可自定义选择，但保持简单：内置十余款自媒体常用
      开源中文字体的一键下载（GitHub 发布源 + 国内镜像轮询）；
    - 字体统一存档在 总目录/字体/，用户可直接往该文件夹放自己的
      ttf/otf/ttc（或 zip 包），软件扫描即用，无需安装到系统。

渲染：drawtext fontfile= 指向所选字体文件；未选/缺失时回退系统中文字体。
字体文件无官方 SHA 清单，下载校验放宽为「非空 + 可读」；来源均为开源可商用/
可免费使用的字体项目（得意黑/霞鹜文楷/站酷系列/思源系列等）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Callable

from . import settings as studio_settings

LogFn = Callable[[str], None]

FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}

# GitHub 直连在部分网络不可达：每个 URL 自动生成镜像候选（与组件下载同策略，
# 2026-07 扩充轮换池；仓库内文件另加 jsDelivr CDN 候选，国内可达性最好）
_GH_MIRRORS = ["https://ghproxy.net/", "https://gh-proxy.com/", "https://ghfast.top/",
               "https://github.moeyy.xyz/", "https://gh.llkk.cc/", "https://mirror.ghproxy.com/", ""]
_RAW_PATTERN = __import__("re").compile(r"github\.com/([^/]+)/([^/]+)/raw/([^/]+)/(.+)")


def mirror_urls(url: str) -> list[str]:
    if "github.com" not in url:
        return [url]
    urls: list[str] = []
    raw = _RAW_PATTERN.search(url)
    if raw:  # 仓库内文件 → jsDelivr CDN 优先（release 资产无此形态）
        owner, repo, branch, path = raw.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")
    urls.extend(prefix + url for prefix in _GH_MIRRORS)
    return urls


# 自媒体常用开源字体包（名称 → 下载定义；file 为落地文件名）
FONT_PACK: list[dict] = [
    {"key": "smiley_sans", "name": "得意黑", "file": "SmileySans-Oblique.ttf",
     "url": "https://github.com/atelier-anchor/smiley-sans/releases/download/v2.0.1/smiley-sans-v2.0.1.zip",
     "zip_member_suffix": "SmileySans-Oblique.ttf",
     "note": "自媒体标题最常用的斜体黑，开源可商用"},
    {"key": "lxgw_wenkai", "name": "霞鹜文楷", "file": "LXGWWenKai-Regular.ttf",
     "url": "https://github.com/lxgw/LxgwWenKai/releases/download/v1.510/LXGWWenKai-Regular.ttf",
     "note": "温润楷体，旁白/文艺风"},
    {"key": "lxgw_wenkai_bold", "name": "霞鹜文楷 中粗", "file": "LXGWWenKai-Medium.ttf",
     "url": "https://github.com/lxgw/LxgwWenKai/releases/download/v1.510/LXGWWenKai-Medium.ttf",
     "note": "楷体加粗，书名/强调（v1.510 无 Bold，Medium 为最粗档）"},
    {"key": "zcool_kuaile", "name": "站酷快乐体", "file": "ZCOOLKuaiLe-Regular.ttf",
     "url": "https://github.com/googlefonts/zcool-kuaile/raw/main/fonts/ttf/ZCOOLKuaiLe-Regular.ttf",
     "note": "圆润活泼，搞笑/生活类"},
    {"key": "zcool_xiaowei", "name": "站酷小薇LOGO体", "file": "ZCOOLXiaoWei-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/zcoolxiaowei/ZCOOLXiaoWei-Regular.ttf",
     "note": "标题LOGO感"},
    {"key": "zcool_qingke", "name": "站酷庆科黄油体", "file": "ZCOOLQingKeHuangYou-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf",
     "note": "厚实黄油感，综艺花字"},
    {"key": "ma_shan_zheng", "name": "马善政毛笔楷", "file": "MaShanZheng-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/mashanzheng/MaShanZheng-Regular.ttf",
     "note": "毛笔手写，国风/武侠"},
    {"key": "long_cang", "name": "龙藏体", "file": "LongCang-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/longcang/LongCang-Regular.ttf",
     "note": "行书手写，情感文案"},
    {"key": "zhi_mang_xing", "name": "指尖芒星体", "file": "ZhiMangXing-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/zhimangxing/ZhiMangXing-Regular.ttf",
     "note": "行草手写"},
    {"key": "liu_jian_mao_cao", "name": "刘建毛草体", "file": "LiuJianMaoCao-Regular.ttf",
     "url": "https://github.com/google/fonts/raw/main/ofl/liujianmaocao/LiuJianMaoCao-Regular.ttf",
     "note": "草书手写"},
    {"key": "noto_sans_sc", "name": "思源黑体(Noto)", "file": "NotoSansSC-Regular.ttf",
     "url": "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf",
     "note": "标准黑体，正文字幕百搭"},
    {"key": "noto_sans_sc_bold", "name": "思源黑体 粗(Noto)", "file": "NotoSansSC-Bold.otf",
     "url": "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/SC/NotoSansSC-Bold.otf",
     "note": "黑体加粗，主字幕/标题"},
    {"key": "noto_serif_sc", "name": "思源宋体(Noto)", "file": "NotoSerifSC-Regular.otf",
     "url": "https://github.com/notofonts/noto-cjk/raw/main/Serif/SubsetOTF/SC/NotoSerifSC-Regular.otf",
     "note": "宋体，知识/文化类"},
]

DEFAULT_FONT_LABEL = "默认（系统字体）"


def list_fonts() -> list[dict]:
    """扫描字体目录（含用户手动放入的），按文件名去重返回。"""
    seen: dict[str, Path] = {}
    root = studio_settings.fonts_dir()
    for file in sorted(root.iterdir()) if root.is_dir() else []:
        if file.suffix.lower() in FONT_SUFFIXES and file.is_file() and file.stat().st_size > 0:
            seen[file.stem] = file
    pack_names = {item["file"]: item["name"] for item in FONT_PACK}
    return [{"name": pack_names.get(path.name, stem), "file": path.name, "path": str(path)}
            for stem, path in seen.items()]


def resolve_font(name_or_file: str | None) -> Path | None:
    """按展示名或文件名解析到字体文件；找不到返回 None（渲染回退系统字体）。"""
    if not name_or_file or name_or_file == DEFAULT_FONT_LABEL:
        return None
    for item in list_fonts():
        if name_or_file in (item["name"], item["file"], Path(item["file"]).stem):
            return Path(item["path"])
    return None


def save_uploaded_font(filename: str, payload: bytes) -> list[str]:
    """保存网页上传的字体（ttf/otf/ttc 直接落盘；zip 解出其中的字体文件）。"""
    root = studio_settings.fonts_dir()
    suffix = Path(filename).suffix.lower()
    saved: list[str] = []
    if suffix in FONT_SUFFIXES:
        target = root / Path(filename).name
        target.write_bytes(payload)
        saved.append(target.name)
    elif suffix == ".zip":
        import io

        with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
            for member in bundle.namelist():
                if Path(member).suffix.lower() in FONT_SUFFIXES and not member.endswith("/"):
                    data = bundle.read(member)
                    if data:
                        target = root / Path(member).name
                        target.write_bytes(data)
                        saved.append(target.name)
        if not saved:
            raise ValueError("zip 里没有找到 ttf/otf/ttc 字体文件。")
    else:
        raise ValueError(f"不支持的字体格式：{suffix}（支持 ttf/otf/ttc 或含字体的 zip）。")
    return saved


def font_statuses() -> list[dict]:
    installed = {item["file"] for item in list_fonts()}
    return [{"key": item["key"], "name": item["name"], "file": item["file"],
             "note": item["note"], "installed": item["file"] in installed}
            for item in FONT_PACK]


def install_font(key: str, log: LogFn) -> None:
    """下载字体包字体（GitHub + 镜像轮询；zip 自动解出目标字体）。"""
    from integrated_workbench.component_download import download_verified_file

    item = next((f for f in FONT_PACK if f["key"] == key), None)
    if item is None:
        raise KeyError(f"未知字体：{key}")
    root = studio_settings.fonts_dir()
    target = root / str(item["file"])
    if target.exists() and target.stat().st_size > 0:
        log(f"✅ {item['name']} 已存在，无需重复下载。")
        return
    member_suffix = item.get("zip_member_suffix")
    download_target = root / (Path(str(item["url"])).name if member_suffix else str(item["file"]))
    log(f"开始下载 {item['name']}（jsDelivr/GitHub + 国内镜像轮询）…")
    try:
        result = download_verified_file(mirror_urls(str(item["url"])), download_target)
    except Exception as exc:
        raise RuntimeError(
            f"所有下载源均失败（{exc}）。手动兜底：浏览器打开 {item['url']} 下载后，"
            f"把文件改名为 {item['file']} 放入字体目录 {root}，软件即自动识别（点「↻ 刷新」）。") from exc
    log(f"  {result.message}")
    if member_suffix:
        with zipfile.ZipFile(download_target) as bundle:
            member = next((m for m in bundle.namelist() if m.endswith(str(member_suffix))), None)
            if not member:
                raise RuntimeError(f"压缩包里未找到 {member_suffix}。")
            target.write_bytes(bundle.read(member))
        download_target.unlink(missing_ok=True)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"下载后未得到有效字体文件：{target}")
    log(f"✅ 字体「{item['name']}」已入库：{target.name}")
