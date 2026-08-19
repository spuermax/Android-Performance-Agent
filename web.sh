#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "[错误] 请先运行 ./setup.sh" >&2
  exit 1
fi
if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
  echo "[错误] 缺少 .env，请先配置 DEEPSEEK_API_KEY" >&2
  exit 2
fi

exec "${VENV_PYTHON}" "${SCRIPT_DIR}/web_app.py"
