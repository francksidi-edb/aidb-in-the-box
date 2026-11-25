#!/bin/bash
set -e

# First start only: create the cluster and the database, then run bootstrap.sql.
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "${POSTGRES_PASSWORD:-postgres}" > /tmp/pwfile
  initdb -D "$PGDATA" -E UTF8 --locale=C.UTF-8 --auth-local=trust --auth-host=scram-sha-256 --pwfile=/tmp/pwfile
  rm -f /tmp/pwfile

  echo "listen_addresses = '*'"            >> "$PGDATA/postgresql.conf"
  echo "shared_preload_libraries = 'aidb,pg_tokenizer'" >> "$PGDATA/postgresql.conf"
  echo "aidb.otel_client = 'database'"       >> "$PGDATA/postgresql.conf"   # governance notebooks: decisions land in aidb_otel.traces
  echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"

  pg_ctl -D "$PGDATA" -w start
  psql -d postgres -c "CREATE DATABASE aidb;"
  # Extensions, demo roles, sample data and the AIDB egress allowlist (all in bootstrap.sql).
  psql -d aidb -v ON_ERROR_STOP=1 -q -f /bootstrap.sql
  pg_ctl -D "$PGDATA" -w stop
fi

# Start Postgres in the background so a restart from a notebook doesn't stop the container.
pg_ctl -D "$PGDATA" -w start

for ext in vector pgfs aidb; do
  psql -d aidb -qAt -c "
    DO \$\$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = '$ext') THEN
        EXECUTE 'ALTER EXTENSION $ext UPDATE';
      END IF;
    END \$\$;" || echo "WARN: ALTER EXTENSION $ext UPDATE failed"
done

# Jupyter in the background, started in the postgres home directory where the notebooks are (no token: demo only,
# bound to localhost on the host).
cd /var/lib/pgsql
~/jupyter_env/bin/jupyter notebook --ip=0.0.0.0 --port=8888 --no-browser \
  --IdentityProvider.token='' --IdentityProvider.password_required=False \
  > ~/jupyter.log 2>&1 &
JUPYTER_PID=$!

# AI in the Box (Flask) in the background, connected to the aidb database. No sign-in: demo only.
export PGHOST=127.0.0.1 PGPORT=5432 PGDATABASE=aidb PGUSER=postgres PGPASSWORD="${POSTGRES_PASSWORD:-postgres}"
export AIBOX_NO_AUTH=1
cd /var/lib/pgsql/app
python3 box_server.py > /var/lib/pgsql/aibox.log 2>&1 &
APP_PID=$!

# On docker stop, shut Postgres down cleanly.
trap 'pg_ctl -D "$PGDATA" -m fast stop || true; kill $APP_PID $JUPYTER_PID 2>/dev/null || true' TERM INT

# The container lives as long as the app does.
wait $APP_PID
