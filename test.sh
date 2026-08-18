#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
    echo "[错误] 尚未初始化 Python 3.12 虚拟环境。请先运行 ./setup.sh。" >&2
    exit 1
fi

cd "${SCRIPT_DIR}"
exec "${VENV_PYTHON}" -m pytest
