#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
source "${SCRIPT_DIR}/_common.sh"

load_local_env
ensure_docker

export EMAILGENIUS_HOME="${EMAILGENIUS_HOME:-${ROOT_DIR}/.emailgenius}"
export EMAILGENIUS_DATABASE_URL="${EMAILGENIUS_DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:5432/emailgenius}"
export EMAILGENIUS_HOST="${EMAILGENIUS_HOST:-127.0.0.1}"
export EMAILGENIUS_PORT="${EMAILGENIUS_PORT:-8080}"

mkdir -p "${EMAILGENIUS_HOME}"

compose_cmd up -d postgres
wait_for_postgres

PYTHON_BIN="$(detect_python)"
ensure_venv "${PYTHON_BIN}"

cd "${ROOT_DIR}"
exec env PYTHONPATH=src "${ROOT_DIR}/.venv/bin/python" -m emailgenius.cli app serve --host "${EMAILGENIUS_HOST}" --port "${EMAILGENIUS_PORT}"
