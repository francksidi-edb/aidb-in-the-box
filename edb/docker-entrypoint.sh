#!/usr/bin/env bash
# Container entrypoint: start the EPAS cluster, configure AIDB on first run, then keep running.
set -euo pipefail
say()  { echo "[aibox] $*"; }
warn() { echo "[aibox] WARN: $*" >&2; }
die()  { echo "[aibox] ERROR: $*" >&2; exit 1; }

V="${PGVER:-18}"; OSUSER=enterprisedb; PGHOME="/usr/edb/as$V"; PSQL="$PGHOME/bin/psql"; PGDB=edb
CTL="$PGHOME/bin/epas_ctlcluster"
MARK=/var/lib/edb-as/.aibox-configured
restart_db() { "$CTL" "$V" main restart; }
. /opt/aibox/configure.sh

"$CTL" "$V" main status >/dev/null 2>&1 || "$CTL" "$V" main start || true
trap '"$CTL" "$V" main stop; exit 0' TERM INT

if [ ! -f "$MARK" ]; then
  [ -n "${AIBOX_DB_PASSWORD:-}" ] || die "AIBOX_DB_PASSWORD is not set."
  PRELOAD_LIBS="aidb"
  if dpkg -s "edb-as${V}-pg-textsearch" >/dev/null 2>&1 && [ "$V" -ge 17 ]; then PRELOAD_LIBS="$PRELOAD_LIBS pg_textsearch"; fi
  if dpkg -s "edb-as${V}-pgaa" >/dev/null 2>&1; then PRELOAD_LIBS="$PRELOAD_LIBS pgaa pgfs"; fi
  AIBOX_REMOTE=1 aibox_configure "$AIBOX_DB_PASSWORD"
  touch "$MARK"; say "first-run setup finished"
fi
say "ready on port 5444"
while "$CTL" "$V" main status >/dev/null 2>&1; do sleep 5; done
die "the cluster stopped"
