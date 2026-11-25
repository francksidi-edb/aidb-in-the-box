#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

docker compose down --volumes --remove-orphans
docker compose build --no-cache --pull
docker compose up -d
