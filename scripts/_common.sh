#!/bin/bash
set -euo pipefail

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env.local"

load_local_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi
}

compose_cmd() {
  local compose_args=("-f" "${ROOT_DIR}/docker-compose.yml")
  if [[ -f "${ENV_FILE}" ]]; then
    compose_args+=("--env-file" "${ENV_FILE}")
  fi

  if docker compose version >/dev/null 2>&1; then
    docker compose "${compose_args[@]}" "$@"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    docker-compose "${compose_args[@]}" "$@"
    return
  fi

  echo "Docker Compose non trovato. Installa Docker Desktop." >&2
  exit 1
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker non trovato. Installa Docker Desktop." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker e' installato ma non risponde. Apri Docker Desktop e riprova." >&2
    exit 1
  fi
}

detect_python() {
  local candidate
  if [[ -n "${PYTHON_BIN:-}" ]] && [[ -x "${PYTHON_BIN}" ]]; then
    printf '%s\n' "${PYTHON_BIN}"
    return
  fi
  for candidate in /usr/local/bin/python3.13 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return
    fi
  done
  echo "Python 3 non trovato. Installa Python 3.13 o esporta PYTHON_BIN." >&2
  exit 1
}

ensure_venv() {
  local python_bin="${1}"
  local venv_dir="${ROOT_DIR}/.venv"
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    "${python_bin}" -m venv "${venv_dir}"
  fi
  "${venv_dir}/bin/python" -m pip install -e "${ROOT_DIR}"
}

wait_for_postgres() {
  local attempts=0
  until compose_cmd exec -T postgres pg_isready -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-emailgenius}" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [[ "${attempts}" -ge 30 ]]; then
      echo "Postgres non e' pronto dopo 30 tentativi." >&2
      exit 1
    fi
    sleep 1
  done
}

timestamp_utc() {
  /bin/date -u +"%Y%m%dT%H%M%SZ"
}
