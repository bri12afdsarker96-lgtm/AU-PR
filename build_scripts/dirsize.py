"""目录体积自检（供打包 .bat 调用）：只打印整数 MB，供 for /f 抓取。

为什么用它而不是在 .bat 里塞 PowerShell 单行：仓库路径含 `&`（D:\...\AU&PR\...），
把绝对路径拼进 cmd 的 for/f 单引号命令里会被 `&` 拆成两条命令 → PowerShell 拿到残缺命令
→ 之前一直打印「0 MB」。这里把路径作为 argv 传入（双引号包住，cmd 不再拆 `&`），
用 Python 自己 os.walk 量体积，长路径/中文/`&` 全部无所谓。

用法：python build_scripts\dirsize.py "dist\某目录"
"""

import os
import sys


def dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def main() -> int:
    if len(sys.argv) < 2:
        print("0")
        return 0
    path = sys.argv[1]
    if not os.path.isdir(path):
        print("0")
        return 0
    print(dir_size_bytes(path) // (1024 * 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
