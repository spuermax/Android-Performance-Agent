#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "[错误] 尚未初始化 Python 3.12 虚拟环境。请先运行 ./setup.sh。" >&2
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/.env" ]]; then
    echo "[配置缺失] 找不到 ${SCRIPT_DIR}/.env。" >&2
    echo "请根据 .env.example 创建 .env，并自行填写 DEEPSEEK_API_KEY。" >&2
    echo "示例：cp .env.example .env" >&2
    exit 2
fi

exec "${VENV_PYTHON}" "${SCRIPT_DIR}/main.py" "$@"
