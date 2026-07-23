"""打包收尾：写版本文件 → 自检产物 → 生成带版本号的发布压缩包。

由 打包.bat 在 PyInstaller 构建成功后调用（python 打包收尾.py）。
独立脚本，避免在 .bat 里用 python -c 复杂引号被 cmd 拆断。
成功退出码 0；产物缺失退出码 1（供 .bat 判定自检）。
"""

import datetime
import os
import shutil
import sys

sys.path.insert(0, "source")

from dub_align_studio.version import APP_VERSION, full_version  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
PRODUCT_NAME = "水星配音对齐工作室"
PROD_DIR = os.path.join(ROOT, "dist", PRODUCT_NAME)
EXE = os.path.join(PROD_DIR, PRODUCT_NAME + ".exe")


def release_dir() -> str:
    explicit = os.environ.get("AU_PR_RELEASE_DIR")
    if explicit:
        return os.path.abspath(explicit)
    parent = os.path.abspath(os.path.join(ROOT, os.pardir))
    if os.path.isdir(os.path.join(parent, "发布包")) or os.path.isdir(os.path.join(parent, "水星配音数据")):
        return os.path.join(parent, "发布包")
    return os.path.join(ROOT, "发布包")


def main() -> int:
    # ① 自检：产物是否真的生成
    if not os.path.isfile(EXE):
        print("[自检失败] 未找到 exe：" + EXE + "，打包可能未成功。请把上方 PyInstaller 输出发给开发。")
        return 1

    # ② 写版本文件（含基础版本 + 构建戳）
    version = full_version()
    with open(os.path.join(PROD_DIR, "版本.txt"), "w", encoding="utf-8") as handle:
        handle.write(version + "\n")
    print("[自检] exe 已生成；产物版本：" + version)

    # ③ 生成带版本号 + 时间戳的发布压缩包（多版本一眼可分）
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    target_release = release_dir()
    base = os.path.join(target_release, PRODUCT_NAME + "_v" + APP_VERSION + "_" + stamp)
    os.makedirs(target_release, exist_ok=True)
    try:
        archive = shutil.make_archive(base, "zip", os.path.join(ROOT, "dist"), PRODUCT_NAME)
        print("[打包] 压缩包已生成：" + archive)
    except Exception as exc:  # 压缩失败不影响 dist 成品可用
        print("[提示] 压缩包生成失败（" + str(exc) + "），可手动压缩 dist\\" + PRODUCT_NAME + " 文件夹。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
