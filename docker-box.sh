#!/usr/bin/env bash
# Run AI in the Box as ONE Docker container: EDB Postgres + AIDB, the app, the AIDB Introspector and JupyterLab.
# Works on a Mac, on Amazon Linux and on any host with Docker, because the EDB packages live inside the container.
#
#   ./docker-box.sh                    build (asks for your EDB token once) and start, reachable from this machine only
#   ./docker-box.sh --public           publish the ports on all addresses (EC2). Add --no-auth for no password (testing only)
#   ./docker-box.sh --base IMAGE       use a prebuilt base image that already holds EDB Postgres + AIDB (no token needed)
#   ./docker-box.sh --image IMAGE      run a prebuilt all-in-one image (nothing is built, no token needed)
#   ./docker-box.sh --rebuild          rebuild the images even if they exist
#   ./docker-box.sh --stop | --logs | --down     stop, follow the log, or remove the container (data volume is kept)
#   Options: --version 18   -y
# Ports: 8650 app and Introspector, 8888 JupyterLab, 5444 Postgres. Data lives in the volume aibox-pgdata.
set -euo pipefail
cd "$(dirname "$0")"
say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

PGVER="${PGVER:-18}"; BIND=127.0.0.1; NOAUTH=""; BASE=""; REBUILD=""; YES=""; IMAGE=""
EDB_REPO="${EDB_REPO:-enterprise}"; PGAA_REPO="${EDB_PGAA_REPO:-postgres_distributed}"; NAME=aibox
while [ $# -gt 0 ]; do
  case "$1" in
    --public) BIND=0.0.0.0 ;; --no-auth) NOAUTH=1 ;; --base) BASE="$2"; shift ;; --image) IMAGE="$2"; shift ;; --rebuild) REBUILD=1 ;;
    --version) PGVER="$2"; shift ;; -y|--yes) YES=1 ;;
    --stop) docker stop "$NAME"; exit 0 ;; --logs) exec docker logs -f "$NAME" ;;
    --down) docker rm -f "$NAME"; echo "Removed. The data volume aibox-pgdata is kept (docker volume rm aibox-pgdata deletes it)."; exit 0 ;;
    -h|--help) sed -n 2,12p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac; shift
done
command -v docker >/dev/null || die "Docker is not installed. Install Docker Desktop (Mac) or Docker Engine (Linux), then run this again."
docker info >/dev/null 2>&1 || die "Docker is installed but not running."
[ -z "$NOAUTH" ] || [ "$BIND" = 127.0.0.1 ] || warn "NO PASSWORD with --public: anyone who can reach this host on ports 8650, 8888 and 5444 gets in. Testing only."

if [ -n "$IMAGE" ]; then
  say "1/1  Pulling $IMAGE"; docker pull "$IMAGE" || die "Could not pull $IMAGE. Log in first (docker login ghcr.io) if the image is private."
  RUNIMG="$IMAGE"
else
# 1. base image: EDB Postgres + AIDB (needs your token, passed as a BuildKit secret that is not stored in the image)
if [ -z "$BASE" ]; then
  BASE="aibox-db:$PGVER"
  if [ -n "$REBUILD" ] || ! docker image inspect "$BASE" >/dev/null 2>&1; then
    if [ -z "${EDB_SUBSCRIPTION_TOKEN:-}" ]; then
      [ -t 0 ] || die "Set EDB_SUBSCRIPTION_TOKEN first (https://www.enterprisedb.com/docs/repos/getting_started/get_your_token/)."
      echo "An EDB subscription token is required to build the Postgres image. A free EDB account includes a 60-day trial."
      read -rsp "EDB subscription token: " EDB_SUBSCRIPTION_TOKEN; echo
    fi
    [ -n "$EDB_SUBSCRIPTION_TOKEN" ] || die "No token given."
    export EDB_SUBSCRIPTION_TOKEN
    say "1/3  Building $BASE (EDB Postgres Advanced Server $PGVER with AIDB, pgvector, PGFS, Vector Plus, pg_textsearch)"
    DOCKER_BUILDKIT=1 docker build -f edb/Dockerfile --secret id=edb_token,env=EDB_SUBSCRIPTION_TOKEN \
      --build-arg PGVER="$PGVER" --build-arg EDB_REPO="$EDB_REPO" --build-arg PGAA_REPO="$PGAA_REPO" -t "$BASE" edb/
  else say "1/3  Using the existing image $BASE (--rebuild to rebuild)"; fi
else say "1/3  Using the base image $BASE"; fi

# 2. the all-in-one image: adds the app, the Introspector, the notebooks and JupyterLab
say "2/3  Building aibox:all (adds the app, the Introspector, JupyterLab and the notebooks)"
docker build -f Dockerfile.box --build-arg BASE="$BASE" -t aibox:all .

  RUNIMG=aibox:all
fi

# 3. run
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  if [ -z "$YES" ] && [ -t 0 ]; then read -rp "A container named $NAME exists. Replace it (the data volume is kept)? [y/N] " a; [ "$a" = y ] || [ "$a" = Y ] || die "Stopped."; fi
  docker rm -f "$NAME" >/dev/null
fi
say "Starting the container $NAME"
ENVS=(); [ -z "$NOAUTH" ] || ENVS+=(-e AIBOX_NO_AUTH=1); [ -z "${AIBOX_PASSWORD:-}" ] || ENVS+=(-e AIBOX_PASSWORD="$AIBOX_PASSWORD")
docker run -d --name "$NAME" --restart unless-stopped \
  -p "$BIND:8650:8650" -p "$BIND:8888:8888" -p "$BIND:5444:5444" \
  -v aibox-pgdata:/var/lib/edb-as ${ENVS[@]+"${ENVS[@]}"} "$RUNIMG" >/dev/null
echo "    first start configures AIDB and takes a minute or two"
for i in $(seq 1 120); do
  curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8650/" 2>/dev/null && break
  docker ps --format '{{.Names}}' | grep -qx "$NAME" || { docker logs --tail 30 "$NAME"; die "The container stopped. See the log above."; }
  sleep 3
done
echo; docker logs "$NAME" 2>&1 | grep -E "Sign in|NO PASSWORD" | tail -1 || true
URLHOST=localhost; [ "$BIND" = 0.0.0.0 ] && URLHOST="<this-machine-ip>"
say "Ready:  http://$URLHOST:8650   (AIDB Introspector at /introspect/, JupyterLab on port 8888, started from the app's notebooks page)"
[ "$BIND" = 0.0.0.0 ] && echo "Open ports 8650 and 8888 in the firewall or EC2 security group."
echo "Logs: ./docker-box.sh --logs     Stop: ./docker-box.sh --stop     Remove: ./docker-box.sh --down"
