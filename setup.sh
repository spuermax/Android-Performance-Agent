#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"

find_python312() {
    local candidate

    for candidate in python3.12 python3; do
        if command -v "${candidate}" >/dev/null 2>&1 && \
            "${candidate}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done

    return 1
}

PYTHON_BIN="$(find_python312 || true)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "[错误] 未找到 Python 3.12。" >&2
    echo "请先安装 Python 3.12，并确保 python3.12（或指向 3.12 的 python3）可在 PATH 中使用。" >&2
    exit 1
fi

if [[ ! -f "${REQUIREMENTS_FILE}" ]]; then
    echo "[错误] 找不到依赖文件：${REQUIREMENTS_FILE}" >&2
    exit 1
fi

if [[ -d "${VENV_DIR}" && ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "[错误] ${VENV_DIR} 已存在，但不是可用的虚拟环境。" >&2
    echo "请移走该目录后重新运行 ./setup.sh。" >&2
    exit 1
fi

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "[setup] 使用 ${PYTHON_BIN} 创建 .venv..."
    if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
        echo "[错误] 创建虚拟环境失败。请确认 Python 3.12 的 venv 模块可用。" >&2
        exit 1
    fi
else
    VENV_VERSION="$("${VENV_DIR}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [[ "${VENV_VERSION}" != "3.12" ]]; then
        echo "[错误] 现有 .venv 使用 Python ${VENV_VERSION}，项目要求 Python 3.12。" >&2
        echo "请移走 .venv 后重新运行 ./setup.sh。" >&2
        exit 1
    fi
    echo "[setup] 使用现有 Python 3.12 虚拟环境。"
fi

# 激活后再执行安装，确保所有命令均使用项目自己的虚拟环境。
source "${VENV_DIR}/bin/activate"

echo "[setup] 升级 pip..."
if ! python -m pip install --upgrade pip; then
    echo "[错误] pip 升级失败。请检查网络或 Python 包源配置后重试 ./setup.sh。" >&2
    exit 1
fi

echo "[setup] 安装项目依赖..."
if ! python -m pip install -r "${REQUIREMENTS_FILE}"; then
    echo "[错误] 依赖安装失败。请检查网络、包源和 requirements.txt，然后重试 ./setup.sh。" >&2
    exit 1
fi

echo "[完成] 开发环境已准备好。"
echo "下一步：./run.sh \"/你的/Android/项目路径\""
