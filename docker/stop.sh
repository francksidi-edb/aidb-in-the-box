#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Stops and removes the container. The database, document and notebook volumes are kept; ./start.sh brings everything
# back.
docker compose down
