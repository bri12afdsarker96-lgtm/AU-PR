#!/usr/bin/env bash
# 云 GPU 一键装机 + 起服务（由 Windows 一键 bat 上传后远程执行；也可自己在 AutoDL 终端里跑）。
# 复刻本地 App 的 dots.tts 安装口径，检查点走 HF 自动下载，无需手动拷模型。
set -uo pipefail
cd "$(dirname "$0")"
LOG=/root/server/server.log
echo "==================== 云配音一键装机 ===================="

# 0) 国内机器走 HF 镜像，避免 huggingface.co 拉不动
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
echo "HF 镜像：$HF_ENDPOINT"

# 0.1) 非交互 SSH shell 默认不激活 conda base（→ python 不在 PATH）。找到并激活它。
for c in "$HOME/miniconda3" "/opt/conda" "$HOME/anaconda3" "/root/miniconda3" "/root/anaconda3"; do
  if [ -f "$c/etc/profile.d/conda.sh" ]; then
    . "$c/etc/profile.d/conda.sh"; conda activate base 2>/dev/null || true; break
  fi
done

# 0.2) 选 Python 解释器：优先 $PYTHON，其次 python，再 python3（本镜像是 py312，多为 python3）
PY=""
for cand in "${PYTHON:-}" python python3; do
  if [ -n "$cand" ] && command -v "$cand" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break; fi
done
if [ -z "$PY" ]; then
  echo "❌ 找不到 python/python3。请确认镜像自带 Python 环境（本镜像应为 py312）。"; exit 1
fi
echo "使用 Python：$PY"; "$PY" --version
# 校验 torch（镜像应自带 CUDA 版；缺了说明选错解释器/环境没激活）
if "$PY" -c "import torch;print('torch',torch.__version__,'CUDA可用',torch.cuda.is_available())" 2>/dev/null; then :; else
  echo "⚠ 当前 Python 里没有 torch —— 可能没激活到镜像预装的那个环境。仍继续；若后面报缺 torch，请告诉我。"
fi

# 1) dots.tts 本体（--no-deps，与本地 App 完全一致：绕开 Windows 装不了的 pynini）
echo "[1/4] 安装 dots.tts 本体…"
$PY -m pip install -q --no-deps dots.tts

# 2) dots.tts 运行依赖（不含 torch —— 镜像自带 CUDA 版 torch，别覆盖）
echo "[2/4] 安装 dots.tts 运行依赖（transformers/accelerate/…）…"
$PY -m pip install -q transformers==4.57.0 accelerate==1.12.0 huggingface-hub loguru \
  "langcodes[data]" einops librosa soundfile numpy pydantic PyYAML safetensors \
  torchdiffeq tqdm lingua-language-detector

# 3) 服务端框架
echo "[3/4] 安装服务端框架（fastapi/uvicorn/…）…"
$PY -m pip install -q "fastapi>=0.110" "uvicorn[standard]>=0.29" "soundfile>=0.12" "pydantic>=2.0"

# 4) API Key（已存则复用，保证客户端填的 Key 长期不变）
if [ -f .env ]; then set -a; source .env; set +a; fi
if [ -z "${DOTS_SERVER_API_KEY:-}" ]; then
  DOTS_SERVER_API_KEY="$($PY -c 'import secrets;print(secrets.token_urlsafe(24))')"
  echo "DOTS_SERVER_API_KEY=$DOTS_SERVER_API_KEY" > .env
fi
export DOTS_SERVER_API_KEY
export DOTS_CHECKPOINT="${DOTS_CHECKPOINT:-rednote-hilab/dots.tts-soar}"

# 5) 起服务（后台常驻；6006 = AutoDL「自定义服务」端口，配合公网 HTTPS）
echo "[4/4] 启动服务（端口 6006，后台常驻）…"
pkill -f dots_tts_server.py 2>/dev/null || true
sleep 1
nohup $PY dots_tts_server.py --host 0.0.0.0 --port 6006 --preload > "$LOG" 2>&1 &
sleep 3

echo
echo "==================== 完成 ===================="
echo ">>> 你的 API Key（填到软件『设置 → 云配音 → API Key』）："
echo
echo "        $DOTS_SERVER_API_KEY"
echo
echo ">>> 服务地址：到 AutoDL 控制台 → 本实例 →「自定义服务」拿公网 https 地址（对应 6006 端口）。"
echo ">>> 首次会自动下载模型（几分钟），下载/运行日志：tail -f $LOG"
echo "=============================================="
