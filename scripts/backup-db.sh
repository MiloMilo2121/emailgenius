#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
source "${SCRIPT_DIR}/_common.sh"

load_local_env
ensure_docker

export EMAILGENIUS_HOME="${EMAILGENIUS_HOME:-${ROOT_DIR}/.emailgenius}"
BACKUP_DIR="${EMAILGENIUS_HOME}/backups"
mkdir -p "${BACKUP_DIR}"

OUTPUT_PATH="${1:-${BACKUP_DIR}/emailgenius-$(timestamp_utc).sql}"

compose_cmd up -d postgres
wait_for_postgres
compose_cmd exec -T postgres pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-emailgenius}" --no-owner --no-privileges > "${OUTPUT_PATH}"

echo "Backup scritto in ${OUTPUT_PATH}"
