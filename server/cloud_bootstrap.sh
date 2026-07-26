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

# 0.1) 激活 conda base（若有；非交互 shell 默认不激活）
for c in "$HOME/miniconda3" "/opt/conda" "$HOME/anaconda3" "/root/miniconda3" "/root/anaconda3"; do
  if [ -f "$c/etc/profile.d/conda.sh" ]; then
    . "$c/etc/profile.d/conda.sh"; conda activate base 2>/dev/null || true; break
  fi
done

# 0.2) 找「真正带 torch + pip」的 Python——镜像预装的 torch 常在 conda/venv 里，
#      而不是 /usr/bin/python3（系统自带、无 torch 无 pip）。逐个候选试，选第一个能 import torch 且有 pip 的。
_has_torch_pip() { [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1; "$1" -c "import torch" >/dev/null 2>&1 && "$1" -m pip --version >/dev/null 2>&1; }
_has_pip()       { [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1; "$1" -m pip --version >/dev/null 2>&1; }

CANDS=()
[ -n "${PYTHON:-}" ] && CANDS+=("$PYTHON")
CANDS+=(python python3.12 python3.11 python3.10 python3)
for base in /opt/conda /root/miniconda3 /root/anaconda3 /usr/local /root/venv /workspace/venv /root/.venv; do
  for p in "$base"/bin/python "$base"/bin/python3 "$base"/bin/python3.12; do [ -x "$p" ] && CANDS+=("$p"); done
  for ep in "$base"/envs/*/bin/python; do [ -x "$ep" ] && CANDS+=("$ep"); done
done

PY=""
for c in "${CANDS[@]}"; do if _has_torch_pip "$c"; then PY="$c"; break; fi; done   # 优先带 torch 的
if [ -z "$PY" ]; then                                                             # 全盘兜底找带 torch 的
  echo "候选里没找到带 torch 的，正在全盘搜索（/opt /root /usr/local /workspace，稍等）…"
  for p in $(find /opt /root /usr/local /workspace -maxdepth 5 -name 'python3*' -type f 2>/dev/null); do
    if _has_torch_pip "$p"; then PY="$p"; break; fi
  done
fi
if [ -z "$PY" ]; then                                                             # 退而求其次：有 pip 就行
  for c in "${CANDS[@]}"; do if _has_pip "$c"; then PY="$c"; break; fi; done
fi
if [ -z "$PY" ]; then
  echo "❌ 没找到可用的 Python（要能 import torch 且有 pip）。请把这段窗口截图发我，我按你这台环境再调。"; exit 1
fi
echo "使用 Python：$PY"; "$PY" --version
"$PY" -c "import torch;print('torch',torch.__version__,'CUDA可用',torch.cuda.is_available())" 2>/dev/null \
  || echo "⚠ 选中的 Python 仍没有 torch（会继续，但配音可能起不来）。请把窗口截图发我。"

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

# 4.5) 注入 tn 桩包：dots_tts 导入时硬性 `from tn.chinese.normalizer import Normalizer`，
#      tn 来自 WeTextProcessing(--no-deps 跳过)。真 tn 不在时，写一个「原样返回」的 tn 包到
#      site-packages，让 import tn 真能成功（与本地口径一致；normalize 默认关，不影响音色）。
if ! "$PY" -c "import tn" >/dev/null 2>&1; then
  SITE="$("$PY" -c "import sysconfig;print(sysconfig.get_paths()['purelib'])" 2>/dev/null)"
  if [ -n "$SITE" ]; then
    mkdir -p "$SITE/tn/chinese" "$SITE/tn/english"
    : > "$SITE/tn/__init__.py"
    for lang in chinese english; do
      : > "$SITE/tn/$lang/__init__.py"
      cat > "$SITE/tn/$lang/normalizer.py" <<'PYEOF'
class Normalizer:
    def __init__(self, *a, **k):
        pass
    def normalize(self, text, *a, **k):
        return text
PYEOF
    done
    "$PY" -c "import tn.chinese.normalizer as m; m.Normalizer().normalize('测试')" >/dev/null 2>&1 \
      && echo "已注入 tn 桩包 → $SITE/tn" || echo "⚠ tn 桩注入校验失败（继续，服务端自带桩兜底）"
  fi
fi

# 5) 起服务（后台常驻，监听所有网卡；配合公网 IP + 防火墙放行同一端口）
PORT="${SERVER_PORT:-8000}"
echo "[4/4] 启动服务（端口 $PORT，后台常驻）…"
pkill -f dots_tts_server.py 2>/dev/null || true
sleep 1
nohup "$PY" dots_tts_server.py --host 0.0.0.0 --port "$PORT" --preload > "$LOG" 2>&1 &
sleep 3

# 尽力探测公网 IP（拿不到就用占位，不影响启动）
PUBIP="$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo '你的公网IP')"

echo
echo "==================== 完成 ===================="
echo ">>> 你的 API Key（填到软件『设置 → 云配音 → API Key』）："
echo
echo "        $DOTS_SERVER_API_KEY"
echo
echo ">>> 服务地址（填到软件『设置 → 云配音 → 地址』）："
echo
echo "        http://$PUBIP:$PORT"
echo
echo ">>> 重要：先到云控制台『外网防火墙』放行 TCP $PORT（动作=接受，源=0.0.0.0/0），否则本地连不上。"
echo ">>> 首次会自动下载模型（几分钟）才能用；进度看日志：tail -f $LOG"
echo "=============================================="
