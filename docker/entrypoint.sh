#!/bin/bash
set -e

BOOTSTRAP_TODO="$PGDATA/aibox-bootstrap.todo"

# First start only: create the cluster.
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "${POSTGRES_PASSWORD:-postgres}" > /tmp/pwfile
  initdb -D "$PGDATA" -E UTF8 --locale=C.UTF-8 --auth-local=trust --auth-host=scram-sha-256 --pwfile=/tmp/pwfile
  rm -f /tmp/pwfile

  echo "listen_addresses = '*'"            >> "$PGDATA/postgresql.conf"
  echo "shared_preload_libraries = 'aidb,pg_tokenizer'" >> "$PGDATA/postgresql.conf"
  echo "aidb.otel_client = 'database'"       >> "$PGDATA/postgresql.conf"   # governance notebooks: decisions land in aidb_otel.traces
  echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
  touch "$BOOTSTRAP_TODO"
fi

# Create the database and run bootstrap.sql until that has succeeded once. The marker is removed only at the end, so
# a first start that fails halfway is retried on the next start; volumes set up before the marker existed are skipped.
if [ -f "$BOOTSTRAP_TODO" ]; then
  pg_ctl -D "$PGDATA" -w start
  psql -d postgres -qAt -c "SELECT 1 FROM pg_database WHERE datname = 'aidb'" | grep -q 1 || psql -d postgres -c "CREATE DATABASE aidb;"
  # Extensions, demo roles, sample data and the AIDB egress allowlist (all in bootstrap.sql).
  psql -d aidb -v ON_ERROR_STOP=1 -q -f /bootstrap.sql || { pg_ctl -D "$PGDATA" -m fast stop; exit 1; }
  pg_ctl -D "$PGDATA" -w stop
  rm -f "$BOOTSTRAP_TODO"
fi

# Start Postgres in the background so a restart from a notebook doesn't stop the container.
pg_ctl -D "$PGDATA" -w start

# Before AIDB's SQL is updated, drop the views of older Token Boss versions: they read AIDB views and tables directly,
# and an update script that drops or changes one fails while they exist (7.7 -> 7.8 drops aidb.models). They hold no
# data, and Token Boss replaces them with views that read AIDB only through monitor_feed's functions (roles.sql).
psql -d aidb -qAt -c "
  DO \$\$
  BEGIN
    IF EXISTS (SELECT FROM pg_extension e JOIN pg_available_extensions a ON a.name = e.extname
               WHERE e.extname = 'aidb' AND e.extversion <> a.default_version)
       AND EXISTS (  -- a view in schema monitor that reads an object of extension aidb
         SELECT FROM pg_depend d
         JOIN pg_rewrite rw ON d.classid = 'pg_rewrite'::regclass AND rw.oid = d.objid
         JOIN pg_class v ON v.oid = rw.ev_class
         JOIN pg_namespace n ON n.oid = v.relnamespace AND n.nspname = 'monitor'
         JOIN pg_depend m ON m.classid = d.refclassid AND m.objid = d.refobjid AND m.deptype = 'e'
         JOIN pg_extension x ON x.oid = m.refobjid AND x.extname = 'aidb') THEN
      DROP VIEW IF EXISTS monitor.task_stats, monitor.agent_last_change, monitor.recent_task_stats,
        monitor.agent_scorecard, monitor.error_summary, monitor.repeated_calls, monitor.tool_usage, monitor.model_ids,
        monitor.task_usage CASCADE;  -- the names in DROP_VIEWS_SQL (webapps/tokenboss_sql.py)
    END IF;
  END \$\$;" || echo "WARN: could not drop the old Token Boss views before the AIDB update"

for ext in vector pgfs aidb; do
  psql -d aidb -qAt -c "
    DO \$\$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = '$ext') THEN
        EXECUTE 'ALTER EXTENSION $ext UPDATE';
      END IF;
    END \$\$;" || {
    echo "ERROR: ALTER EXTENSION $ext UPDATE failed (the error is above). The database still has the SQL objects of the"
    echo "       old $ext version, which the installed $ext library does not work with. Fix the cause, then restart."
  }
done

# The demo roles, also on volumes created before a role was added (roles.sql can run again).
PGOPTIONS="-c client_min_messages=warning" psql -d aidb -v ON_ERROR_STOP=1 -q -f /roles.sql || echo "WARN: roles.sql failed"

# The notebooks live on their own volume, so edits made in Jupyter survive a new container. Copy in the image's version
# of each file that is new, or that nobody edited since it was last copied (its checksum is the one recorded then);
# edited files are kept. The record is NOTEBOOKS/.image-checksums (checksum, tab, file name).
# A notebook's checksum covers its cells' sources only: running it saves outputs, which is not an edit.
content_sum() {
  case "$1" in
    *.ipynb) python3 -c 'import hashlib, json, sys
cells = json.load(open(sys.argv[1]))["cells"]
print(hashlib.sha256(json.dumps([[c["cell_type"], "".join(c["source"])] for c in cells]).encode()).hexdigest())' "$1" \
               2>/dev/null || echo unreadable ;;
    *) sha256sum < "$1" | cut -d' ' -f1 ;;
  esac
}
sync_notebooks() {
  local src=/var/lib/pgsql/app/notebooks dst=/var/lib/pgsql/notebooks
  local record="$dst/.image-checksums" next
  next="$(mktemp)"
  touch "$record"
  for f in "$src"/*; do
    [ -f "$f" ] || continue
    local name image copied current
    name="$(basename "$f")"
    image="$(content_sum "$f")"
    copied="$(awk -F'\t' -v n="$name" '$2 == n {print $1}' "$record")"
    current=""
    if [ -e "$dst/$name" ]; then current="$(content_sum "$dst/$name")"; fi
    if [ -z "$current" ] || { [ "$current" = "$copied" ] && [ "$current" != "$image" ]; }; then
      cp "$f" "$dst/$name"
      current="$image"
    elif [ "$current" != "$image" ] && [ "$current" != "$copied" ]; then
      echo "Keeping your edited $name (the image has another version: $f)"
    fi
    # what is in the folder now counts as not edited if it is the image's version
    printf '%s\t%s\n' "$( [ "$current" = "$image" ] && echo "$image" || echo "$copied" )" "$name" >> "$next"
  done
  mv "$next" "$record"
}
sync_notebooks

# Jupyter in the background, started in the notebooks folder. Jupyter reads its token from JUPYTER_TOKEN (a new one on
# every start unless set in .env); the app passes it to its "Open in Jupyter" links.
export JUPYTER_TOKEN="${JUPYTER_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}"
cd /var/lib/pgsql/notebooks
~/jupyter_env/bin/jupyter notebook --ip=0.0.0.0 --port=8888 --no-browser > ~/jupyter.log 2>&1 &
JUPYTER_PID=$!

# AI in the Box (Flask) in the background, connected to the aidb database. No sign-in: demo only.
# The status page and Inspect connect as postgres; the apps connect as demo_user and Token Boss as demo_monitor
# (roles.sql).
export PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=aidb PGUSER=postgres PGPASSWORD="${POSTGRES_PASSWORD:-postgres}"
export AIBOX_DB_USER=demo_user AIBOX_DB_PASSWORD=demo AIBOX_MONITOR_USER=demo_monitor AIBOX_MONITOR_PASSWORD=demo
export AIBOX_NO_AUTH=1
cd /var/lib/pgsql/app
python3 box_server.py > /var/lib/pgsql/aibox.log 2>&1 &
APP_PID=$!

# Shut Postgres down cleanly on docker stop, and also when the app exits (otherwise Docker kills it).
stop_all() {
  kill $APP_PID $JUPYTER_PID 2>/dev/null || true
  pg_ctl -D "$PGDATA" -m fast stop || true
}
trap 'stop_all; exit 0' TERM INT

# The container lives as long as the app does.
status=0
wait $APP_PID || status=$?
echo "AI in the Box exited (status $status), stopping. See /var/lib/pgsql/aibox.log."
stop_all
exit $status
