#!/usr/bin/env bash
# Entrypoint of the all-in-one image: start Postgres (first run configures AIDB), then the app and JupyterLab.
set -uo pipefail
say() { echo "[aibox] $*"; }
DATA=/var/lib/edb-as
PWFILE="$DATA/.aibox-dbpass"

# One database password for the life of the volume, so restarts and a replaced container still connect.
if [ -s "$PWFILE" ]; then AIBOX_DB_PASSWORD="$(cat "$PWFILE")"
else
  AIBOX_DB_PASSWORD="${AIBOX_DB_PASSWORD:-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 24)}"
  mkdir -p "$DATA"; (umask 077; printf '%s' "$AIBOX_DB_PASSWORD" > "$PWFILE")
fi
export AIBOX_DB_PASSWORD

/opt/aibox/docker-entrypoint.sh &
DB=$!
APP=""
stop() { say "stopping"; [ -n "$APP" ] && kill -TERM "$APP" 2>/dev/null; kill -TERM "$DB" 2>/dev/null; wait; exit 0; }
trap stop TERM INT

say "starting Postgres (the first start configures AIDB and takes a minute or two)"
for i in $(seq 1 200); do
  [ -f "$DATA/.aibox-configured" ] && break
  kill -0 "$DB" 2>/dev/null || { say "Postgres setup stopped, see the log above"; exit 1; }
  sleep 3
done
[ -f "$DATA/.aibox-configured" ] || { say "Postgres did not finish its first-run setup"; exit 1; }

export PGHOST=127.0.0.1 PGPORT=5444 PGDATABASE=aibox PGUSER=aibox PGPASSWORD="$AIBOX_DB_PASSWORD"
if [ -n "${AIBOX_NO_AUTH:-}" ] && [ "$AIBOX_NO_AUTH" != 0 ]; then
  say "NO PASSWORD on the app (AIBOX_NO_AUTH). Anyone who can reach the ports can use it. Testing only."
else
  [ -n "${AIBOX_PASSWORD:-}" ] || AIBOX_PASSWORD="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 16)"
  export AIBOX_PASSWORD
  say "Sign in to the app with user: aibox   password: $AIBOX_PASSWORD"
fi
say "App: http://localhost:8650   Introspector: http://localhost:8650/introspect/   JupyterLab: http://localhost:8888 (open it from the app)"
cd /opt/aibox/app && python3 box_server.py &
APP=$!
wait -n "$DB" "$APP"
say "a process exited, shutting down"
stop
