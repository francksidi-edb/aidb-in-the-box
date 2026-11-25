#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PROFILE_URL="https://www.enterprisedb.com/accounts/profile"

if [ -z "${EDB_TOKEN:-}" ]; then
	echo "An EDB Repos 2.0 token is required to build the image."
	read -p "Press enter to open a new browser window and log in to $PROFILE_URL to get the \"Repos 2.0 token\"..."
	open "$PROFILE_URL" || xdg-open "$PROFILE_URL" || true

	read -r -s -p "Paste your token: " EDB_TOKEN
	echo ""
	if [ -z "$EDB_TOKEN" ]; then
		echo "No token entered. Exiting." >&2
		exit 1
	fi
	export EDB_TOKEN
fi

if ! docker info >/dev/null 2>&1; then
	echo "Docker isn't running. Start Docker Desktop (or Rancher Desktop) and try again." >&2
	exit 1
fi

docker compose up -d --build

echo ""
echo "AI in the Box: http://localhost:8650"
echo "Jupyter:       http://localhost:8888"

sleep 3
open http://localhost:8650 || xdg-open http://localhost:8650 || true
