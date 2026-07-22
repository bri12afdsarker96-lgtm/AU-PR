"""`python -m dub_align_studio` 启动水星配音对齐工作室（浏览器版界面）。

打包发行请用 launcher.py 作为 PyInstaller 入口（见 README 打包指令）。
"""

if __package__:
    from .web_server import main
else:  # 被当作顶层脚本执行（如误用本文件打包）时退回绝对导入
    from dub_align_studio.web_server import main

raise SystemExit(main())
