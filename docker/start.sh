#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PROFILE_URL="https://www.enterprisedb.com/accounts/profile"

ask_token() {
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
}

if ! docker info >/dev/null 2>&1; then
	echo "Docker isn't running. Start Docker Desktop (or Rancher Desktop) and try again." >&2
	exit 1
fi

# The token is only needed to install the EDB packages. Once the image exists, that build step comes from the cache, so
# ask only for a first build, or when a build without the token fails (for example after a change to that step).
if [ -z "${EDB_TOKEN:-}" ] && ! docker image inspect aidb-in-the-box >/dev/null 2>&1; then
	ask_token
fi

if ! docker compose build; then
	if [ -n "${EDB_TOKEN:-}" ]; then
		exit 1
	fi
	echo ""
	echo "The build failed without a token; it probably has to download the EDB packages again."
	ask_token
	docker compose build
fi
docker compose up -d

echo ""
echo "AI in the Box: http://localhost:8650"
echo "Jupyter:       open it from Run it in the app (it needs the token the app adds)"

sleep 3
open http://localhost:8650 || xdg-open http://localhost:8650 || true
