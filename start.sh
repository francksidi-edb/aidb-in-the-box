#!/usr/bin/env bash
# Start AI in the Box (port 8650), which includes the AIDB Introspector. Ctrl-C stops it.
#   ./start.sh                start the apps and open the browser
#   ./start.sh --setup-demo   load the Northwind demo data and AIDB objects first
#   ./start.sh --install-db   install EDB Postgres + AIDB first (./install-edb.sh; add --docker on a Mac)
#   ./start.sh --update       update the showcase from GitHub, then start
#   ./start.sh --jupyter      open the demo notebooks in JupyterLab instead
#   ./start.sh --docker       run everything in ONE Docker container (Postgres + AIDB + app + JupyterLab); see ./docker-box.sh --help
#   AIBOX_HOST=0.0.0.0 AIBOX_NO_AUTH=1 ./start.sh   same, with no password (testing only)
#   AIBOX_HOST=0.0.0.0 ./start.sh   listen on all addresses behind a password (user aibox; AIBOX_PASSWORD or a random one)
# Set AIBOX_NO_BROWSER=1 to skip opening the browser (for example over SSH).
set -e
cd "$(dirname "$0")"
[ "${1:-}" = --docker ] && { shift; exec bash ./docker-box.sh "$@"; }
[ -f env.sh ] && . ./env.sh
[ -f venv/bin/activate ] && [ -d aidb-showcase ] || bash ./install.sh
. venv/bin/activate
export AIBOX_PORT="${AIBOX_PORT:-8650}"

case "${1:-}" in
  --install-db)
    shift; bash ./install-edb.sh "$@"; [ -f env.sh ] && . ./env.sh
    ;;
  --update)
    (cd aidb-showcase && git pull --ff-only) || echo "Could not update the showcase (not a git checkout?). Continuing."
    ;;
  --setup-demo)
    bash aidb-showcase/demo-data/run_demo_setup.sh
    ;;
  --jupyter)
    pip install -q jupyterlab jupysql sqlalchemy psycopg2-binary pandas datasets
    echo "The notebooks connect to Postgres over the local socket, so run this on the box that hosts Postgres."
    exec jupyter lab --notebook-dir=notebooks
    ;;
esac

# AIBOX_HOST=0.0.0.0 listens on all addresses behind a password (user aibox). Default: this machine only.
AIBOX_HOST="${AIBOX_HOST:-127.0.0.1}"; export AIBOX_HOST
case "$AIBOX_HOST" in
  127.0.0.1|localhost|::1) ;;
  *) if [ -n "${AIBOX_NO_AUTH:-}" ] && [ "$AIBOX_NO_AUTH" != 0 ]; then echo "Network mode with NO password (AIBOX_NO_AUTH): anyone who can reach port ${AIBOX_PORT} can use this app. Testing only."
     else
     [ -n "${AIBOX_PASSWORD:-}" ] || AIBOX_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')"
     export AIBOX_PASSWORD; echo "Network mode. Sign in with user: aibox   password: $AIBOX_PASSWORD"
     fi ;;
esac

URL="http://127.0.0.1:${AIBOX_PORT}"
echo "AI in the Box: $URL      (the AIDB Introspector is built in: $URL/introspect/)"
case "$AIBOX_HOST" in 127.0.0.1|localhost|::1) ;; *) echo "From other machines: http://<this-machine-ip>:${AIBOX_PORT}  (open port ${AIBOX_PORT} in the firewall or EC2 security group)";; esac
if [ -z "${AIBOX_NO_BROWSER:-}" ]; then
  (command -v open >/dev/null && open "$URL") || (command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1) || true
fi
python3 box_server.py
