# 云 dots.tts 配音服务端 · 部署文档

本机 3050 配一句要 3 分钟，太慢。这套「混合方案」把**唯一吃显卡的那一步（配音克隆推理）**
搬到云 GPU，其余全留在你本地：本地路径、分镜/输出目录、成片渲染、去重、存档**全都不变**。
本地软件里选引擎 **`dots.tts（云 GPU · 远程）`**，就会调云显卡；带宽只走「参考音频+文案上、
合成 wav 下」，每句一两 MB，很轻。

```
你的电脑（本地软件，本地路径照旧）
   └─ 选「dots.tts 云 GPU」→ HTTPS + API Key → 云 GPU 服务器（dots_tts_server.py）
                                                    └─ RTX 4090 跑 runtime.generate
```

---

## 最省事：Windows 一键 bat（推荐给不熟命令行的你）

租好 GPU 实例后（见下方第 0 节），**不用敲任何命令**：

1. 把本 `server/` 文件夹整个下载到 Windows（至少要有 `一键部署到云GPU.bat`、
   `dots_tts_server.py`、`cloud_bootstrap.sh`、`requirements.txt` 四个文件在一起）。
2. 双击 **`一键部署到云GPU.bat`**。
3. 按提示**粘贴 AutoDL 面板的「SSH 登录指令」**，回车；出现 `password` 时**输入面板的「密码」**
   （可能要输 1–2 次）。
4. 脚本会自动：上传服务端 → 装 dots.tts + 依赖 → 下载模型 → 在 6006 端口启动服务，
   最后**打印一串 API Key**。记下它。
5. 到 **AutoDL 控制台 → 本实例 →「自定义服务」** 拿公网 https 地址。
6. 打开软件 → **设置 → ☁ 云配音**：填「地址 + API Key」→ 💾 保存 → 🔌 测试连接 →
   引擎下拉选 **dots.tts（云 GPU · 远程）**。完成。

> 模型是 HF 仓库 `rednote-hilab/dots.tts-soar`，**首次自动下载，无需手动拷检查点**；
> 脚本已设 `HF_ENDPOINT=https://hf-mirror.com` 国内镜像加速。torch 由镜像自带。

下面是手动分步版（想自己控制或排错时看）。

---

## 0. 先租 GPU（AutoDL 为例）

- **GPU**：选 **RTX 4090**（约 ¥2/小时）。单句能从 3 分钟压到几秒~十几秒。**不配音就关机**
  （AutoDL 关机不收算力费，只留少量存储费）。
- **镜像**：选 **基础镜像 → PyTorch**（已带 torch + CUDA，省最多事）。点卡片 `▼` 选
  **CUDA 12.x / torch ≥ 2.1 / Python 3.10 或 3.11**。
  - 不要选 CUDA/Miniconda（要自己装 torch）、不要选 ComfyUI/vLLM 等（别的应用）、不要选系统镜像。

---

## 1. 传代码 + 装依赖

把本仓库的 `server/` 目录传到云主机（`scp` 或 AutoDL 的 JupyterLab 上传），然后：

```bash
cd server
pip install -r requirements.txt          # fastapi/uvicorn/soundfile/pydantic
```

**安装 dots.tts 本体 + 运行依赖**（与本地 App 完全同一口径；`cloud_bootstrap.sh` 已封装这步）：

```bash
export HF_ENDPOINT=https://hf-mirror.com                 # 国内镜像，模型才拉得动
pip install --no-deps dots.tts                           # 本体（--no-deps，绕开 pynini）
pip install transformers==4.57.0 accelerate==1.12.0 huggingface-hub loguru \
  "langcodes[data]" einops librosa soundfile numpy pydantic PyYAML safetensors \
  torchdiffeq tqdm lingua-language-detector               # 运行依赖（不含 torch，镜像自带）
```

> **模型无需手动下载**：默认检查点 `rednote-hilab/dots.tts-soar`（与本地一致），首次
> `from_pretrained` 自动从 HF 镜像拉取。服务端只认 `dots_tts.runtime.DotsTtsRuntime`；
> 若导入路径不同，用环境变量 `DOTS_RUNTIME_MODULE` 覆盖。

---

## 2. 设 API Key + 启动

```bash
bash run_server.sh          # 检查点默认 rednote-hilab/dots.tts-soar，自动下载；如需自定义再 export DOTS_CHECKPOINT
```

`run_server.sh` 首次会**自动生成一个 API Key** 并写入 `server/.env`，同时打印出来：

```
已生成 API Key（也写入 .env）：Xk9...（这串就是要填到本地软件的 Key）
```

- **把这串 Key 记下**，下面第 4 步要填到本地软件。
- 服务端**没设 Key 会拒绝启动**（避免接口裸奔被别人白嫖/打爆你的显卡）。
- 带 `--preload`：开机即把模型加载进显存，第一句不用再等 1–3 分钟。

自测（在云主机上）：
```bash
curl -H "X-API-Key: 你的Key" http://127.0.0.1:8000/health
# 期望：{"status":"ok","gpu":"NVIDIA GeForce RTX 4090",...}
```

---

## 3. 用公网 HTTPS 暴露（你选的方案）

### AutoDL：用「自定义服务」（最省事的公网 HTTPS）
AutoDL 会把容器的 **6006 端口**通过它的 https 代理暴露成一个公网地址。所以：

```bash
PORT=6006 bash run_server.sh
```
然后在 **AutoDL 控制台 → 你的实例 → 自定义服务**，拿到形如
`https://u****-****.****.seetacloud.com` 的**公网 https 地址**。这就是要填到本地软件的「服务器地址」。

> 鉴权仍由我们自己的 **X-API-Key** 兜底：即使地址被人知道，没有 Key 也调不动、`/health` 直接 401。

### 其它云主机：Caddy 反代自动 HTTPS（要有域名）
```bash
# 已有域名 dots.example.com 解析到本机、80/443 放行：
caddy reverse-proxy --from dots.example.com --to 127.0.0.1:8000
```
Caddy 会自动签发并续期 Let's Encrypt 证书，本地软件填 `https://dots.example.com`。

> 备选：Cloudflare Tunnel（`cloudflared tunnel --url http://127.0.0.1:8000`）也能给一个 https 地址，
> 无需开放端口。不管哪种，**API Key 都不能省**。

---

## 4. 本地软件配置

打开本地软件 → **工具箱自检 / 设置**页 → **☁ 云配音（远程 GPU）**：

1. **云服务器地址**：填第 3 步拿到的 https 地址（**填到服务根，不要带 `/synthesize`**）。
2. **API Key**：填第 2 步那串。
3. 点 **💾 保存云配置**，再点 **🔌 测试连接** → 显示 `✅ 云 dots.tts 可用…GPU：RTX 4090` 即通。
4. 到 **配音引擎** 下拉选 **`dots.tts（云 GPU · 远程）`**，照常配音即可。

> 也可用环境变量代替设置：`DOTS_REMOTE_ENDPOINT` / `DOTS_REMOTE_API_KEY`（优先级高于设置面板）。

---

## 5. 省钱与安全

- **不配音就关机**：AutoDL 关机不计算力费。下次开机 `bash run_server.sh` 即可（AutoDL 自定义服务地址通常不变；换实例会变，记得回本地更新地址）。
- **Key 不要外泄**：它等于你显卡的使用权。泄露了就删 `server/.env` 重跑生成新 Key，并在本地重填。
- 只暴露 `/health` 与 `/synthesize` 两个接口，且都要 Key；没有任何文件系统/命令接口。
- 客户端与服务端**同一版 dots.tts + 同一份检查点**，才能保证云端与本地音色一致。

---

## 接口速览（供排错）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/health` | GET | 需 `X-API-Key`。返回 GPU、模型是否已加载。 |
| `/synthesize` | POST JSON | 需 `X-API-Key`。字段：`text`（客户端已含引子「嗯。」）、`prompt_audio_b64`、`prompt_text`、`num_steps`、`guidance_scale`、`seed`、`normalize_text`。返回 **PCM16 WAV** 字节。|

服务端**不做**引子注入与起音裁切——那些在客户端完成，服务端保持纯 GPU worker。
