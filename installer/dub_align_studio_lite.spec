# -*- mode: python ; coding: utf-8 -*-
# ============================================================
#  水星配音对齐工作室 · 轻量云配版 · PyInstaller spec（含完整防护层）
#
#  这份 spec 明确带上：
#    - licensing 完整子包（激活码 gate + HWID + Nonce/HMAC + DPAPI）
#    - RASP 完整子模块（调试器/frida/vm/完整性 检测）
#    - web/ 前端资源（含激活遮罩层 index.html）
#    - app.ico 图标（Windows exe 图标 + 任务栏 + 快捷方式）
#
#  运行时靠 启动软件.bat 里的两个环境变量最终启用防护：
#    set DUB_ALIGN_LICENSE_REQUIRED=1   -> 激活码 gate 生效
#    set DUB_ALIGN_RASP_STRICT=1         -> 反调试/反注入 exit(3)
#  （不设 = 开发/兼容模式，gate 放行）
# ============================================================
from pathlib import Path

# spec 位于 installer\，项目根是它的上一级
HERE = Path(SPEC).resolve().parent.parent
SOURCE = HERE / "source"
ICON = HERE / "installer" / "app.ico"

# 显式列出 licensing 子模块 —— 保底防止 PyInstaller 静态分析漏
LICENSING_MODS = [
    "dub_align_studio.licensing",
    "dub_align_studio.licensing.client",
    "dub_align_studio.licensing.crypto_store",
    "dub_align_studio.licensing.heartbeat",
    "dub_align_studio.licensing.machine_id",
    "dub_align_studio.licensing.rasp",
    "dub_align_studio.licensing.session",
]

# 主程序常用但可能被静态分析漏的模块
EXTRA_HIDDEN = [
    "dub_align_studio",
    "dub_align_studio.web_server",
    "dub_align_studio.syspy",
]

# 数据文件（web 前端资源必须一起打）
DATAS = [(str(SOURCE / "dub_align_studio" / "web"), "dub_align_studio/web")]

# fonts 目录如果存在也带上
_fonts = SOURCE / "dub_align_studio" / "fonts"
if _fonts.exists():
    DATAS.append((str(_fonts), "dub_align_studio/fonts"))

a = Analysis(
    [str(SOURCE / "dub_align_studio" / "launcher.py")],
    pathex=[str(SOURCE)],
    binaries=[],
    datas=DATAS,
    hiddenimports=LICENSING_MODS + EXTRA_HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='水星配音对齐工作室_云配版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX 压缩会触发一部分杀软误报，留原始
    console=False,   # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='水星配音对齐工作室_云配版',
)
