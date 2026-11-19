# edb/configure.sh - sourced by install-edb.sh and by the Docker entrypoint.
# Caller provides: OSUSER (OS superuser of the cluster), PSQL (path to psql), PGDB (maintenance db),
#                  restart_db() (restarts the cluster), and the say/warn/die helpers.
# Optional:        PRELOAD_LIBS (default "aidb pg_textsearch"), AIBOX_REMOTE=1 (listen on all addresses, port 5444),
#                  AIBOX_REQUIRE_AIDB=0 (only for testing without AIDB).
as_pg() { if [ "$(id -u)" = 0 ]; then runuser -u "$OSUSER" -- "$@"; else sudo -u "$OSUSER" "$@"; fi; }
sql()   { (cd / && as_pg "$PSQL" -X -q -v ON_ERROR_STOP=1 -d "$PGDB" "$@"); }

wait_ready() {
  local i
  for i in $(seq 1 90); do sql -Atc 'select 1' >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

add_hba() {   # add one pg_hba.conf line at the top, once
  as_pg bash -c 'grep -qxF -- "$1" "$2" || sed -i "1i $1" "$2"' _ "$1" "$HBA_FILE"
}

aibox_configure() {
  local pass="$1" cur new lib restart=0
  wait_ready || die "Postgres did not become ready. Check the server log."

  # 1. shared_preload_libraries: append, never overwrite
  cur="$(sql -Atc 'show shared_preload_libraries')"; new="$cur"
  for lib in ${PRELOAD_LIBS-aidb pg_textsearch}; do
    case ",${new// /}," in *",$lib,"*) ;; *) new="${new:+$new,}$lib"; restart=1 ;; esac
  done
  if [ "$new" != "$cur" ]; then   # one quoted literal per library: 'a,b' would be stored as a single bad name
    local q="" item; IFS=, read -ra items <<< "${new// /}"
    for item in "${items[@]}"; do [ -n "$item" ] && q="${q:+$q, }'$item'"; done
    sql -c "ALTER SYSTEM SET shared_preload_libraries = $q"
  fi
  # pgaa.autostart_seafowl does not exist until pgaa is loaded, so ALTER SYSTEM rejects it: write the line to postgresql.auto.conf instead
  case " ${PRELOAD_LIBS-} " in *" pgaa "*)
    as_pg bash -c 'f="$1/postgresql.auto.conf"; grep -q "^pgaa.autostart_seafowl" "$f" 2>/dev/null || echo "pgaa.autostart_seafowl = '"'on'"'" >> "$f"' _ "$(sql -Atc 'show data_directory')"
    restart=1 ;; esac
  if [ "${AIBOX_REMOTE:-0}" = 1 ]; then
    sql -c "ALTER SYSTEM SET listen_addresses = '*'" -c "ALTER SYSTEM SET port = 5444"; restart=1
  fi
  if [ "$restart" = 1 ]; then say "    restarting Postgres to load: $new"; restart_db; wait_ready || die "Postgres did not come back after the restart."; fi

  # 2. demo role and database (the role is a superuser: this is a local demo box)
  sql -v pw="$pass" <<'SQL'
SET password_encryption = 'scram-sha-256';
SELECT 'CREATE ROLE aibox LOGIN SUPERUSER' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aibox') \gexec
ALTER ROLE aibox PASSWORD :'pw';
SELECT 'CREATE DATABASE aibox OWNER aibox' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aibox') \gexec
SQL

  # 3. let the aibox role log in over TCP with a password
  HBA_FILE="$(sql -Atc 'show hba_file')"
  add_hba "host all aibox 127.0.0.1/32 scram-sha-256"
  add_hba "host all aibox ::1/128 scram-sha-256"
  [ "${AIBOX_REMOTE:-0}" = 1 ] && add_hba "host all aibox 0.0.0.0/0 scram-sha-256"
  sql -c 'select pg_reload_conf()' >/dev/null

  # 4. extensions (docs: CREATE EXTENSION aidb CASCADE pulls in vector)
  if [ "${AIBOX_REQUIRE_AIDB:-1}" = 1 ]; then
    sql -d aibox -c 'CREATE EXTENSION IF NOT EXISTS aidb CASCADE' || die "CREATE EXTENSION aidb failed. Is shared_preload_libraries set and the server restarted?"
    sql -d aibox -c 'GRANT aidb_users TO aibox'
  fi
  set -- pgfs "edb_vectorplus CASCADE" pg_textsearch
  case " ${PRELOAD_LIBS-} " in *" pgaa "*) set -- "$@" "pgaa CASCADE" ;; esac
  for e in "$@"; do
    sql -d aibox -c "CREATE EXTENSION IF NOT EXISTS $e" 2>/dev/null && say "    extension ${e%% *}: ok" || warn "extension ${e%% *} not enabled (package missing or unsupported here)"
  done
  DB_PORT="$(sql -Atc 'show port')"
}
