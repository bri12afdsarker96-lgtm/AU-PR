#!/usr/bin/env bash
# 云 dots.tts 服务端一键启动。首次用法见 README_云端部署.md。
set -euo pipefail
cd "$(dirname "$0")"

# 1) API Key：首次自动生成并存到 .env（客户端要填同一个）
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; source .env; set +a
fi
if [ -z "${DOTS_SERVER_API_KEY:-}" ]; then
  DOTS_SERVER_API_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
  echo "DOTS_SERVER_API_KEY=$DOTS_SERVER_API_KEY" >> .env
  echo "已生成 API Key（也写入 .env）：$DOTS_SERVER_API_KEY"
  echo ">>> 把这个 Key 填到本地软件『设置 → 云配音 → API Key』"
  export DOTS_SERVER_API_KEY
fi

# 2) dots 检查点目录（改成你实际的路径或 HF 名）
export DOTS_CHECKPOINT="${DOTS_CHECKPOINT:-/root/models/dots.tts}"

# 3) 启动（--preload 开机即把模型进显存，首行不再等 1–3 分钟）
exec python dots_tts_server.py --host 0.0.0.0 --port "${PORT:-8000}" --preload
