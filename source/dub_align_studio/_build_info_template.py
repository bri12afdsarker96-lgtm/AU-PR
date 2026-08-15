# -*- coding: utf-8 -*-
"""Build-time constants — 打包脚本每次打包时**重新生成** _build_info.py。

这是模板 / 兜底：
  * 开发时 _build_info.py 不存在，代码 fallback 到"未打包"分支（gate 默认关，
    走 env 兼容路径），不打断本地开发和 pytest；
  * 打包时 build_dist.write_build_info() 生成真正的 _build_info.py：
      - PACKAGED = True                    → 强制启用 gate + RASP strict
                                             （env 环境变量再也拧不动）
      - BUILD_TIMESTAMP                    → 当前打包时刻
      - INTEGRITY_HMAC_KEY = <随机 32 B>   → 完整性哈希用 HMAC 不是裸 SHA
                                             （攻击者不知道 key 就伪造不了）
      - XOR_SEED = <随机 32 B>             → 会话本地加密的 XOR 种子
                                             （每包不同，破一个不会通杀）
      - SERVER_URL_ENC = <XOR 密文 hex>    → 服务端地址不再字面出现在 .pyc
      - APP_ID_ENC = <XOR 密文 hex>        → 同上

调用方（web_server / rasp / client / crypto_store）：
    try:
        from . import _build_info as _bi
        PACKAGED = _bi.PACKAGED
    except ImportError:
        PACKAGED = False
"""

from __future__ import annotations

# ============================================================
# 未打包 fallback（本模板值）
# ============================================================
PACKAGED = False
BUILD_TIMESTAMP = ""
INTEGRITY_HMAC_KEY = b""      # 空 → integrity_check 走老路径
XOR_SEED = b""                # 空 → crypto_store 走 legacy 种子
SERVER_URL_ENC = ""           # 空 → client 用 client.DEFAULT_SERVER
APP_ID_ENC = ""               # 空 → client 用 client.APP_ID
