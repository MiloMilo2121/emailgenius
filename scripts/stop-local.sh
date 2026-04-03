#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
source "${SCRIPT_DIR}/_common.sh"

load_local_env
ensure_docker

compose_cmd stop postgres
echo "Postgres locale fermato. I dati restano nel volume Docker."
