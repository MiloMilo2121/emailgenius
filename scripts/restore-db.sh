#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
source "${SCRIPT_DIR}/_common.sh"

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 /percorso/backup.sql [--yes]" >&2
  exit 1
fi

BACKUP_PATH="$1"
CONFIRM_FLAG="${2:-}"

if [[ ! -f "${BACKUP_PATH}" ]]; then
  echo "Backup non trovato: ${BACKUP_PATH}" >&2
  exit 1
fi

if [[ "${CONFIRM_FLAG}" != "--yes" ]]; then
  echo "Restore distruttivo: sovrascrive il database locale. Rilancia con --yes per confermare." >&2
  exit 1
fi

load_local_env
ensure_docker

compose_cmd up -d postgres
wait_for_postgres
compose_cmd exec -T postgres psql -U "${POSTGRES_USER:-postgres}" -d postgres -c "DROP DATABASE IF EXISTS ${POSTGRES_DB:-emailgenius};"
compose_cmd exec -T postgres psql -U "${POSTGRES_USER:-postgres}" -d postgres -c "CREATE DATABASE ${POSTGRES_DB:-emailgenius};"
compose_cmd exec -T postgres psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-emailgenius}" < "${BACKUP_PATH}"

echo "Restore completato da ${BACKUP_PATH}"
