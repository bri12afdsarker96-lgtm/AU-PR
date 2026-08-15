# 软件加密与防破译方案（Python 源码保护）

> 面向本项目（水星配音对齐工作室 · AU-PR）的实操指南。
> 更新时间：2026-08，Python 3.11 环境。
> 本项目已实装：**在线激活 gate + HWID 绑定 + Nonce 防重放 + RASP + DPAPI 加密 session + Cython/Nuitka 打包脚本**。

---

## 0. 本项目当前防线（已实装）

| # | 防线 | 位置 | 状态 |
| --- | --- | --- | --- |
| 1 | **在线激活码 gate** | `licensing/__init__.py` + `web_server` | ✅ |
| 2 | **一机一码（HWID 绑定）** | `licensing/machine_id.py` | ✅ |
| 3 | **心跳踢下线** | `licensing/heartbeat.py` | ✅ |
| 4 | **Nonce + HMAC 签名** | `licensing/client.py::_base_payload` | ✅ 客户端；服务端待启用 |
| 5 | **DPAPI 加密 session** | `licensing/crypto_store.py` | ✅ Windows；其他平台 xor 兜底 |
| 6 | **RASP** 反调试/环境/完整性 | `licensing/rasp.py` | ✅ 检测；strict 模式需 env 启用 |
| 7 | **敏感字符串 xor 加密** | `licensing/rasp.py::decrypt_str` | ✅ 工具 |
| 8 | **Cython 编译授权模块** | `build_dist.py::cython_compile_licensing` | ✅ 打包时执行 |
| 9 | **Nuitka 主程序编译** | `build_dist.py::nuitka_build` | ✅ 打包时执行 |
| 10 | **发行包敏感文件清扫** | `build_dist.py::scrub_sensitive` | ✅ 打包时执行 |
| 11 | **exe 完整性 baseline** | `build_dist.py::write_integrity_hash` | ✅ 打包时生成 |
| 12 | **启动脚本注入 strict env** | `打包_轻量云配版包.bat` + `启动软件.bat` | ✅ |

## 0.1 用户操作

**开发/测试**（默认）：
```bash
python -m dub_align_studio
# 无 gate，无 RASP，直接进主界面
```

**发行打包**（Windows）：
```bat
双击 打包_轻量云配版包.bat
```
产物在 `dist\轻量云配版包\`，用户复制该目录 → 双击 `启动软件.bat` 即可。

## 0.2 敏感文件清单（**发行包严禁包含**）

`build_dist.py::SENSITIVE_PATTERNS` 已列表清除；再次强调：

| 类别 | 具体 | 泄露风险 |
| --- | --- | --- |
| **源码** | `*.py` `*.pyc` `*.pyo` | 直接白嫖 |
| **本地用户数据** | `settings.json` `license.json` `gpu_state.json` `queue.sqlite3` | 泄露旧激活码/使用记录 |
| **反破解文档** | `docs/PROTECTION.md` `README*.md` `*.md` | 教破解者绕过 |
| **仓库 metadata** | `.git/` `.github/` `.claude/` `.gitignore` | 提交历史/密钥/CI 令牌 |
| **测试代码** | `tests/` `conftest.py` | 含 mock 逻辑可被反向利用 |
| **构建 metadata** | `pyproject.toml` `setup.py` `requirements*.txt` | 依赖树/内部包名 |
| **虚拟环境** | `.venv/` `venv/` | 巨大且含全部依赖源 |

## 0.3 一键 exe 安装器（用户零依赖）

面向最终用户的发行形态：**单文件 setup exe**，双击安装向导，无需 Python / ffmpeg / vcredist。

### 打包流程（开发者机器一次配置）

1. **装 Inno Setup 6**：https://jrsoftware.org/isdl.php （国内镜像也有）
2. **装编译依赖**：`pip install nuitka cython`
3. **准备 ffmpeg**：把 `ffmpeg.exe` 和 `ffprobe.exe` 放到 `tools/ffmpeg/`（essentials 裁剪版约 40MB，LZMA2 后约 15MB）
   - 也可让 build_dist 从 PATH 兜底找，但不推荐（用户机构版本可能差异过大）
4. **一键打包**：
   ```bat
   双击 打包_安装器.bat
   ```
   → 自动串联 `build_dist.py --with-ffmpeg --require-ffmpeg` + `ISCC.exe installer.iss`
   → 产物：`dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe`

### 减负核心（installer.iss）

| 手段 | 效果 |
| --- | --- |
| `Compression=lzma2/ultra64` | payload 通常压到 40~60% 体积 |
| `SolidCompression=yes` | 相似文件共享词典，进一步压缩 |
| `LZMAUseSeparateProcess=yes` | 加快编译（多核） |
| `Excludes=*.py,*.pyc,__pycache__,tests,docs,settings.json,license.json,queue.sqlite3,gpu_state.json,*.md` | 双保险，防误打包敏感/无用文件 |
| ffmpeg 用 essentials 版 | 完整版 ~150MB → essentials ~40MB → LZMA2 后 ~15MB |
| Nuitka `--standalone` 已剥离调试符号 | 主 exe 也小一圈 |

### 用户体验

1. 双击 `setup_水星配音对齐工作室_v0.7.71_lite.exe`
2. Windows 10 及以上 → 通过 `InitializeSetup()` 版本检查
3. 选安装目录 → 选是否创建桌面快捷方式
4. 进度条完成 → 可选立即启动
5. 首次启动进入激活码 gate（`DUB_ALIGN_LICENSE_REQUIRED=1` 由 `启动软件.bat` 注入）

### 卸载

- 控制面板 → 程序和功能 → 找到 "水星配音对齐工作室 v0.7.71" 卸载
- **保留用户数据**：只删安装目录，不动 `~/我的文档\水星配音数据` 和 `~/.dub_align_studio/`
- 卸载器会清理 `_MEIPASS*` / `__pycache__` 等临时残留

### 代码签名（可选，商用推荐）

`installer.iss` 里的 `SignTool` 指令已留占位；有 EV 证书时取消注释即可，产物签名后能：
- 免 SmartScreen 警告
- 提高 Windows Defender 信任度
- 显示发布者名称而不是 "Unknown"

---


## TL;DR 三档推荐

| 场景 | 方案 | 破解难度 | 落地成本 |
| --- | --- | --- | --- |
| **快速上线**（1 天）| **PyArmor Free** + PyInstaller | 中 | 低 |
| **稳定发行**（1 周）| **Nuitka Commercial** 单模块编译 + PyArmor 保护关键逻辑 | 高 | 中 |
| **强保护**（1 月）| **Cython** 编译核心模块 + **PyArmor** RFT 混淆 + 授权服务器双绑 | 极高 | 高 |

任何本地方案都**只能提高破解成本，不能消除**。真正让软件"用不了破解版"的核心是**在线激活 + 心跳 gate**（本项目已实装，见 `source/dub_align_studio/licensing/`）。

---

## 1. 三种主流技术选型对比

### 1.1 PyArmor（首选，商业友好）

**原理**：把 `.py` 编译成加密的 code object；运行时用 native `.pyd/.so` 动态解密执行。

- 官网：https://pyarmor.readthedocs.io/
- License：Free 版（个人 + 商用限量）/ Basic $99 / Pro $299（含 RFT 更强混淆）
- 支持 Python 3.7~3.13

**能防**：
- ✅ 直接 `strings` / `cat` 看源码
- ✅ 反编译 .pyc（uncompyle6/decompyle3）
- ✅ 字面量提取（字符串常量在 obfuscated 后加密）

**不能防**：
- ⚠ 内存 dump：运行时 code object 会解密到内存
- ⚠ Frida / debug attach：动态 hook 能拿到解密后的 bytecode
- ⚠ **Free 版对已知混淆工具的反混淆工具存在**（PyArmor-Unpacker 等）

**用法（本项目适配）**：
```bat
REM 在项目根目录
pip install pyarmor

REM 一次性把整个 source/dub_align_studio/ 混淆到 dist_obfuscated/
pyarmor gen -O dist_obfuscated -r source/dub_align_studio

REM 混淆后目录直接可跑：python dist_obfuscated/dub_align_studio/__main__.py

REM 集成到 PyInstaller
pyarmor gen --pack onedir -e "--name 水星配音对齐工作室" source/dub_align_studio
```

**关键**：把 `licensing/client.py`、`licensing/heartbeat.py`（授权验证逻辑）作为**必混淆**目标；这些代码一旦被 patch，激活流就绕过了。

### 1.2 Nuitka（真编译，最快最难破）

**原理**：把 Python 编译成 C，再编译成 native `.pyd`（Windows）/`.so`（Linux）。产物是**真机器码**，不再是 bytecode。

- 官网：https://nuitka.net/
- License：MIT（免费），Commercial 版有额外反调试 / anti-tamper
- 支持 Python 3.4~3.13

**能防**：
- ✅ 反编译（无 bytecode 可反）
- ✅ IDA/Ghidra 看到的是 C→机器码，符号表也可 strip
- ✅ 字面量在编译时可内联/加密（Commercial 版）

**不能防**：
- ⚠ 逆向工程时间成本高但不无限：熟练逆向工程师仍可动态分析
- ⚠ Free 版符号未混淆；函数名 `activate` 依然清晰可搜

**用法（本项目）**：
```bat
pip install nuitka

REM 单文件模式（最难破，但启动稍慢）
python -m nuitka --standalone --onefile ^
  --enable-plugin=multiprocessing ^
  --include-package=dub_align_studio ^
  --include-data-dir=source/dub_align_studio/web=dub_align_studio/web ^
  --windows-icon-from-ico=icon.ico ^
  --windows-console-mode=disable ^
  --output-filename="水星配音对齐工作室.exe" ^
  source/dub_align_studio/launcher.py

REM 目录模式（启动快，破解稍易但仍远难于 PyInstaller）
python -m nuitka --standalone ^
  --enable-plugin=multiprocessing ^
  ...同上...
```

**Commercial 版额外**：
- `--anti-tamper` 检测 exe 篡改
- `--python-flag=no_bytecode` 完全去掉 .pyc
- `--include-module` 白名单，避免多余 Python 库被打包
- Windows 上 code signing

### 1.3 Cython（部分编译，商用软件常用）

**原理**：把 `.py` 转成 `.pyx` → 编译成 C 扩展 `.pyd`。发行 `.pyd` 时源码彻底不可见（无 bytecode）。

- 官网：https://cython.readthedocs.io/
- License：Apache 2.0 免费

**用法**：
```bash
pip install cython

# 把 licensing/ 全部编译成 .pyd（示例 setup.py）
python setup_cython.py build_ext --inplace
```

Cython 特别适合把**授权相关的关键模块**（`client.py` / `heartbeat.py` / `machine_id.py`）单独编译，然后 PyInstaller/Nuitka 打包其他部分。这样破解者拿到 `licensing.pyd` 完全没有源码可读，只能反汇编 native 代码。

---

## 2. 推荐组合（本项目）

```
┌──────────────────────────────────────────────────┐
│  发行包（Windows exe）                             │
│                                                    │
│  ┌────────────────────────────────────────────┐  │
│  │  Nuitka 编译（--standalone）                │  │
│  │  ├─ 所有 .py → C → native pyd              │  │
│  │  └─ 授权失败时立刻 exit()                    │  │
│  └────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────┐  │
│  │  Cython 编译（额外一层）                     │  │
│  │  ├─ licensing/client.py    → client.pyd    │  │
│  │  ├─ licensing/heartbeat.py → heartbeat.pyd │  │
│  │  └─ licensing/machine_id.py → machine.pyd  │  │
│  └────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────┐  │
│  │  在线激活 gate（服务端强制）                  │  │
│  │  ├─ 首次输入激活码 → 服务端签发 session      │  │
│  │  ├─ 心跳 30s / 次 → 服务端 action 判定       │  │
│  │  └─ machine_id 强绑定，换机激活码失效         │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

**为什么这样组合**：
- Nuitka 把 `.py` 变机器码 → 反编译无门
- Cython 把授权模块单独编译 → 即使别人 Nuitka 逆向出主程序，授权模块仍是 native
- 授权服务器 → 破解本地代码也没用，服务端不签发 session token 就不能用

## 3. 具体打包脚本（Windows）

已放在项目根目录 `打包.bat`（如存在），下一步会新增 `打包_加密版.bat`。示意：

```bat
@echo off
REM 打包加密版（Nuitka + Cython + 授权开关）
setlocal
cd /d "%~dp0"

REM 1. Cython 编译 licensing 模块
python -m Cython.Build.Cythonize -i -3 source\dub_align_studio\licensing\*.py

REM 2. Nuitka 打包全体
set NUITKA_OPTS=--standalone --enable-plugin=multiprocessing ^
                --include-package=dub_align_studio ^
                --include-data-dir=source\dub_align_studio\web=dub_align_studio\web ^
                --windows-console-mode=disable ^
                --output-filename=水星配音对齐工作室.exe ^
                --output-dir=dist_encrypted

python -m nuitka %NUITKA_OPTS% source\dub_align_studio\launcher.py

REM 3. 生成启动 bat，注入 DUB_ALIGN_LICENSE_REQUIRED=1
(
echo @echo off
echo set DUB_ALIGN_LICENSE_REQUIRED=1
echo start "" "%%~dp0水星配音对齐工作室.exe"
) > dist_encrypted\launcher.dist\启动软件.bat

echo Done. 分发 dist_encrypted\launcher.dist\ 整个目录
pause
```

## 4. 反调试 / 反 dump 补充（进阶）

以下增强性技术，MVP 阶段**不建议**上（成本 vs 收益不划算），但如遇专业破解可加：

- **代码签名**：Windows 用 SignTool 给 exe 加数字签名 —— 篡改 exe 后签名失效，用户能看出
- **完整性校验**：软件启动时校验自己 exe 的 SHA-256；被 patch 就退出
- **反调试**：`IsDebuggerPresent()` / `CheckRemoteDebuggerPresent()` 检测调试器；发现即退出
- **反 dump**：`VirtualQuery` 检测代码段被读取
- **字符串加密**：把 `"http://101.201.108.8:8001"` 等敏感字符串在编译时用 xor 加密，运行时解密
- **多点校验**：授权检查散布在多个模块的多个位置，破解者需要 patch N 处

## 5. 授权服务端加固

**软件本地防破解只能治标**；真正的护城河在服务端。已实装的措施：

- **单机绑定**：`machine_id` = SHA256(MachineGuid + Volume Serial + CPU)；换机重激活失败
- **心跳强制**：`heartbeat_interval` 秒不心跳 → 服务端标为离线；再次上线 kick 旧 session
- **一码一机**：服务端记录已激活设备数；到上限就拒
- **动态到期**：`expire_at` 由服务端管理，客户端本地时间被改也没用
- **黑名单**：发现被破解的激活码，后台一键 `banned` → 下次心跳直接 stop

**推荐再加**（服务端侧，非本 PR 范围）：
- 定期轮换 `session_token` 加密材料
- 追踪同一 `code` 多次不同 `machine_id` 的活动 → 疑似泄露 → 提示用户重置
- 版本禁用（force_update）配合客户端强制升级

## 6. 现实提醒（诚实说明）

**没有任何 Python 方案能达到 C++ 商业软件级的反破解强度**。破解成本可以推到"专业逆向工程师花 1~2 周"的水平，但：

- **不能防**内部员工/前员工带走源码
- **不能防**运行时内存 dump（PyArmor 官方也承认）
- **不能防**服务端一旦宕机、破解版可"离线永久使用"（本项目 heartbeat 有 `soft_grace_seconds` 宽限，恶意场景仍能利用）
- **不能防**同一台机器多用户共用激活码（这是"一机多用"合法特性，不算破解）

**结论**：
1. **本地加密防"随手拷贝一份源码就能白嫖"** → PyArmor / Nuitka / Cython 三选一即可
2. **在线激活防"批量白嫖 + 商业倒卖"** → 已实装（本项目 licensing 模块）
3. **法律 + 商务** 才是最终护栏 —— 用户协议、水印溯源、渠道分销授权

---

## 附：常见破解手段对照

| 破解手段 | 未保护 | PyInstaller | PyArmor | Nuitka | Cython+Nuitka |
| --- | --- | --- | --- | --- | --- |
| `unzip .exe && strings` 拉字符串 | ✗ 秒破 | ✗ 秒破 | ⚠ 拿到密文 | ✅ 无源 | ✅ 无源 |
| `uncompyle6 *.pyc` 反编译 | N/A | ✗ 秒破 | ⚠ 拿到密文 | ✅ 无 pyc | ✅ 无 pyc |
| IDA/Ghidra 反汇编 | N/A | 中等 | 中等 | 高 | **极高** |
| Frida hook `activate()` | 低 | 低 | 低 | 中 | 中（函数名可搜） |
| Wireshark 抓包 → 伪造服务器 | 低 | 低 | 低 | 低 | 低 |
| **对策** | 全裸 | PyInstaller | PyArmor | Nuitka | Cython+Nuitka+SSL pinning |

## 参考资料

- [PyArmor Documentation](https://pyarmor.readthedocs.io/en/latest/)
- [Nuitka User Manual](https://nuitka.net/user-documentation/user-manual.html)
- [Cython — Compiling Python code](https://cython.readthedocs.io/en/latest/src/tutorial/cython_tutorial.html)
- [Keygen Software Licensing](https://keygen.sh/docs/) —— 商业授权系统参考
- [Cryptlex Software Licensing](https://cryptlex.com/) —— 商业授权系统参考
- OWASP Reverse Engineering & Code Tampering: https://mas.owasp.org/MASTG/
