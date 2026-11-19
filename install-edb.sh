#!/usr/bin/env bash
# Install EDB Postgres with AIDB on this machine and leave it ready for ./start.sh.
#
#   ./install-edb.sh                 native install (Linux: Debian 12, Ubuntu 22.04/24.04, RHEL 8/9 and clones)
#   ./install-edb.sh --docker        run it in a Linux container (needed on macOS; also works on Linux)
#   ./install-edb.sh --check         show what would happen and exit
#   ./install-edb.sh --configure-only   Postgres is already installed: skip repos and packages (no token needed), just create the
#                                    aibox login, preload AIDB and enable the extensions
#   Options: --version 18   --flavor as|pg|pge   --repo NAME   -y (no prompts)
#   Flavors: as = EDB Postgres Advanced Server (default), pg = community PostgreSQL, pge = EDB Postgres Extended
#   Installs AIDB, pgvector, PGFS, Vector Plus, pg_textsearch and (where supported) Analytics Accelerator (PGAA).
#
# You need an EDB subscription token (a free EDB account gives a 60-day trial):
#   https://www.enterprisedb.com/docs/repos/getting_started/get_your_token/
# Export EDB_SUBSCRIPTION_TOKEN, or the script asks for it. It is never written to env.sh.
# Steps follow the EDB docs for AIDB, pgvector, PGFS, Vector Plus and pg_textsearch (Nov 2026).
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

PGVER="${PGVER:-18}"; FLAVOR="${FLAVOR:-as}"; REPO="${EDB_REPO:-}"; YES=""; CHECK=""; DOCKER=""; CONFIGURE_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) PGVER="$2"; shift ;; --flavor) FLAVOR="$2"; shift ;; --repo) REPO="$2"; shift ;;
    --docker) DOCKER=1 ;; --configure-only) CONFIGURE_ONLY=1 ;; --check) CHECK=1 ;; -y|--yes) YES=1 ;;
    -h|--help) sed -n 2,17p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac; shift
done
PGAA_REPO="${EDB_PGAA_REPO:-postgres_distributed}"
case "$FLAVOR" in
  as)  X="edb-as${PGVER}";               REPO="${REPO:-enterprise}";   OSUSER=enterprisedb; PGHOME="/usr/edb/as${PGVER}";  PGDB=edb ;;
  pge) X="edb-postgresextended${PGVER}"; REPO="${REPO:-standard}";     OSUSER=postgres;     PGHOME="/usr/edb/pge${PGVER}"; PGDB=postgres
       warn "Flavor pge (EDB Postgres Extended) is best-effort: its paths are less tested than EPAS." ;;
  pg)  X="edb-pg${PGVER}";               REPO="${REPO:-community360}"; OSUSER=postgres;     PGHOME="/usr/lib/postgresql/${PGVER}"; PGDB=postgres ;;
  *) die "--flavor must be as, pg or pge" ;;
esac

OS="$(uname -s)"; ARCH="$(uname -m)"
if [ "$OS" = Darwin ] && [ -z "$DOCKER" ]; then
  die "AIDB has no macOS build (the docs list Linux only). Use a Linux container instead:  ./install-edb.sh --docker   (Docker Desktop on Apple silicon runs linux/arm64 natively)."
fi

ask_token() {
  if [ -z "${EDB_SUBSCRIPTION_TOKEN:-}" ]; then
    [ -t 0 ] || die "Set EDB_SUBSCRIPTION_TOKEN first (get one at https://www.enterprisedb.com/repos-downloads)."
    echo "An EDB subscription token is required. A free EDB account includes a 60-day trial:"
    echo "  https://www.enterprisedb.com/docs/repos/getting_started/get_your_token/"
    read -rsp "EDB subscription token: " EDB_SUBSCRIPTION_TOKEN; echo
  fi
  [ -n "$EDB_SUBSCRIPTION_TOKEN" ] || die "No token given."
  export EDB_SUBSCRIPTION_TOKEN
}
rand() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 24; }
write_env() {   # host port password [extra PATH]
  [ -f env.sh ] && cp env.sh "env.sh.bak.$(date +%Y%m%d%H%M%S)"
  { echo "# Written by install-edb.sh. Superuser demo role for a local box."
    [ -n "${4:-}" ] && echo "export PATH=\"$4:\$PATH\""
    echo "export PGHOST=$1 PGPORT=$2 PGDATABASE=aibox PGUSER=aibox"
    echo "export PGPASSWORD=$3"; } > env.sh
  chmod 600 env.sh
}
confirm() { [ -n "$YES" ] && return 0; read -rp "$1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]; }

# ------------------------------------------------------------------ Docker
if [ -n "$DOCKER" ]; then
  [ "$FLAVOR" = as ] || die "The Docker path supports --flavor as only."
  say "Plan (Docker): build image aibox-db:$PGVER on Debian 12 with $X-server, -aidb, -pgvector, -pgfs, -vectorplus, -pg-textsearch from repo '$REPO', plus -pgaa from '$PGAA_REPO' on x86_64 images (PGAA has no Debian arm64 build); run it as container aibox-db on 127.0.0.1:5444; write env.sh."
  [ -n "$CHECK" ] && exit 0
  command -v docker >/dev/null || die "Docker is not installed or not running."
  docker info >/dev/null 2>&1 || die "Docker is installed but not running."
  ask_token; PASS="$(rand)"
  say "1/3  Building the image (the token is passed as a build secret, not stored in the image)"
  DOCKER_BUILDKIT=1 docker build -f edb/Dockerfile --secret id=edb_token,env=EDB_SUBSCRIPTION_TOKEN \
    --build-arg PGVER="$PGVER" --build-arg EDB_REPO="$REPO" --build-arg PGAA_REPO="$PGAA_REPO" -t "aibox-db:$PGVER" edb/
  say "2/3  Starting container aibox-db"
  if docker ps -a --format '{{.Names}}' | grep -qx aibox-db; then
    confirm "A container named aibox-db exists. Replace it (its data volume aibox-pgdata is kept)?" || die "Stopped."
    docker rm -f aibox-db >/dev/null
  fi
  docker run -d --name aibox-db --restart unless-stopped -p 127.0.0.1:5444:5444 \
    -v aibox-pgdata:/var/lib/edb-as -e AIBOX_DB_PASSWORD="$PASS" "aibox-db:$PGVER" >/dev/null
  say "3/3  Waiting for first-run setup (up to 5 minutes)"
  for i in $(seq 1 100); do docker exec aibox-db test -f /var/lib/edb-as/.aibox-configured 2>/dev/null && break; sleep 3; done
  docker exec aibox-db test -f /var/lib/edb-as/.aibox-configured || { docker logs --tail 30 aibox-db; die "Setup did not finish. See the log above."; }
  # a replaced container keeps the old volume, so the password in the volume may differ: set it explicitly
  docker exec -e PW="$PASS" aibox-db bash -c 'runuser -u enterprisedb -- /usr/edb/as*/bin/psql -X -q -d edb -v pw="$PW" -c "SET password_encryption = '"'"'scram-sha-256'"'"'" -c "ALTER ROLE aibox PASSWORD :'"'"'pw'"'"'"' >/dev/null 2>&1 || true
  write_env 127.0.0.1 5444 "$PASS"
  say "Done. Postgres with AIDB is running in Docker. Next:  ./start.sh --setup-demo   (loads the demo data)   or   ./start.sh"
  exit 0
fi

# ------------------------------------------------------------------ Native Linux
[ "$OS" = Linux ] || die "Unsupported OS: $OS"
. /etc/os-release 2>/dev/null || true
OSID="${ID:-unknown}"; OSVER="${VERSION_ID:-}"
if command -v apt-get >/dev/null; then PM=deb; elif command -v dnf >/dev/null; then PM=rpm; else die "Need apt-get or dnf."; fi
supported=no
case "$OSID:$OSVER:$ARCH" in
  ubuntu:22.04:x86_64|ubuntu:24.04:x86_64|debian:12:x86_64|debian:12:aarch64) supported=yes ;;
  rhel:8*:x86_64|rocky:8*:x86_64|almalinux:8*:x86_64|rhel:9*:x86_64|rocky:9*:x86_64|almalinux:9*:x86_64|rhel:9*:aarch64|rocky:9*:aarch64|almalinux:9*:aarch64) supported=yes ;;
esac
# PGAA: Debian/Ubuntu amd64 and RHEL 8/9 amd64 + arm64 (EDB PGAA compatibility page)
PGAA_OK=yes; { [ "$PM" = deb ] && [ "$ARCH" != x86_64 ]; } && PGAA_OK=no
[ "$PGVER" -ge 16 ] || PGAA_OK=no

if [ "$FLAVOR" = pg ]; then
  if [ "$PM" = deb ]; then SERVER_PKGS="postgresql-${PGVER}"; PGHOME="/usr/lib/postgresql/${PGVER}"
  else SERVER_PKGS="postgresql${PGVER}-server postgresql${PGVER}-contrib"; PGHOME="/usr/pgsql-${PGVER}"; fi
else SERVER_PKGS="${X}-server"; fi
PSQL="$PGHOME/bin/psql"
if [ "$PM" = deb ]; then PKG_VEC="${X}-pgvector-0"; else PKG_VEC="${X}-pgvector0"; fi
if [ "$FLAVOR" = pge ]; then AIDB_CANDS="edb-postgresextended-${PGVER}-aidb ${X}-aidb"; else AIDB_CANDS="${X}-aidb"; fi
case "$FLAVOR:$PM" in
  as:*)      PGAA_CANDS="${X}-pgaa" ;;
  pge:*)     PGAA_CANDS="edb-postgresextended-${PGVER}-pgaa ${X}-pgaa" ;;
  pg:deb)    PGAA_CANDS="postgresql-${PGVER}-pgaa ${X}-pgaa" ;;
  pg:rpm)    PGAA_CANDS="${X}-pgaa postgresql${PGVER}-pgaa" ;;
esac
OPTIONAL="${X}-pgfs ${X}-vectorplus ${X}-pg-textsearch"

say "Plan (native): $OSID $OSVER $ARCH, package manager $PM, supported by AIDB: $supported"
[ -n "$CONFIGURE_ONLY" ] && echo "  Configure only: steps 1 and 2 (repositories, packages) are skipped; Postgres must already be installed."
echo "  1. add EDB repositories '$REPO' and '${EDB_EXT_REPO:-standard}' (AIDB lives in standard; needs your subscription token)"
[ "$PGAA_OK" = yes ] && echo "     and '$PGAA_REPO' (hosts Analytics Accelerator, PGAA; skipped if your subscription lacks it)"
echo "  2. install: $SERVER_PKGS $PKG_VEC ${AIDB_CANDS%% *}"
echo "     optional: $OPTIONAL"
if [ "$PGAA_OK" = yes ]; then echo "     optional: ${PGAA_CANDS%% *} (Analytics Accelerator)"; else echo "     Analytics Accelerator (PGAA): not available for $OSID/$ARCH, skipped"; fi
echo "  3. create the cluster if needed, preload aidb (plus pg_textsearch, pgaa, pgfs when installed), restart"
echo "  4. create role and database 'aibox', enable aidb, pgfs, edb_vectorplus, pg_textsearch, pgaa"
echo "  5. write env.sh (and put $PGHOME/bin on PATH so psql is available)"
[ -n "$CHECK" ] && exit 0
if [ "$supported" != yes ]; then
  warn "This platform ($OSID $OSVER $ARCH) is not on the AIDB support list: Ubuntu 22.04/24.04 x86_64, Debian 12 x86_64/arm64, RHEL 8 x86_64, RHEL 9 x86_64/arm64 and clones (Rocky, Alma)."
  case "$OSID" in amzn) warn "Amazon Linux is not supported by the EDB packages. Use an EC2 image of RHEL 9, Rocky 9, Ubuntu 22.04/24.04 or Debian 12, or run ./install-edb.sh --docker." ;; esac
  confirm "Try anyway?" || die "Stopped."
fi
SUDO=""
[ "$(id -u)" = 0 ] || { command -v sudo >/dev/null || die "Run as root or install sudo."; SUDO=sudo; }
confirm "This ${CONFIGURE_ONLY:+configures Postgres and restarts it}${CONFIGURE_ONLY:-installs packages and restarts Postgres} on this machine. Continue?" || die "Stopped."
[ -n "$CONFIGURE_ONLY" ] || ask_token

add_repo() {   # name
  curl -1sSLf "https://downloads.enterprisedb.com/$EDB_SUBSCRIPTION_TOKEN/$1/setup.$PM.sh" | $SUDO -E bash >/dev/null
}
inst() {       # package... (output hidden; callers report)
  if [ "$PM" = deb ]; then $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"; else $SUDO dnf install -y "$@"; fi
}
try_first() {  # first candidate that installs, printed on stdout
  local c; for c in "$@"; do inst "$c" >/dev/null 2>&1 && { echo "$c"; return 0; }; done; return 1
}
pkg_has() { if [ "$PM" = deb ]; then dpkg -s "$1" >/dev/null 2>&1; else rpm -q "$1" >/dev/null 2>&1; fi; }

PGAA_PKG=""
if [ -z "$CONFIGURE_ONLY" ]; then
say "1/5  EDB repositories"
add_repo "$REPO" || die "Could not add repository '$REPO'. Check the token, and that your subscription includes it (enterprise: EPAS, standard: EDB Postgres Extended, community360: PostgreSQL). Override with --repo."
EXT_REPO="${EDB_EXT_REPO:-standard}"; if [ "$EXT_REPO" != "$REPO" ]; then if add_repo "$EXT_REPO"; then echo "    $EXT_REPO: added"; else warn "    $EXT_REPO: not available with this token (AIDB packages live in the standard repository)"; fi; fi
if [ "$PGAA_OK" = yes ]; then
  add_repo "$PGAA_REPO" && echo "    $PGAA_REPO: added" || { warn "    $PGAA_REPO: not available with this token, skipping Analytics Accelerator"; PGAA_OK=no; }
fi
[ "$PM" = deb ] && $SUDO apt-get update -qq
if [ "$PM" = rpm ]; then
  $SUDO dnf -qy module disable postgresql >/dev/null 2>&1 || true
  [ "$FLAVOR" = pg ] && { $SUDO dnf -y install epel-release >/dev/null 2>&1 || warn "    could not enable EPEL (needed by community PostgreSQL on some RHEL clones)"; }
fi

say "2/5  Packages"
# shellcheck disable=SC2086
inst $SERVER_PKGS $PKG_VEC || die "Server or pgvector install failed. Check the repository ('$REPO'), flavor and version."
# shellcheck disable=SC2086
AIDB_PKG="$(try_first $AIDB_CANDS)" || die "AIDB package not found ($AIDB_CANDS). The repository may not include AIDB for this flavor and version (try --repo, --flavor or --version)."
echo "    $AIDB_PKG: installed"
for p in $OPTIONAL; do inst "$p" >/dev/null 2>&1 && echo "    $p: installed" || warn "    $p: not available, skipping"; done
PGAA_PKG=""
if [ "$PGAA_OK" = yes ]; then
  # shellcheck disable=SC2086
  PGAA_PKG="$(try_first $PGAA_CANDS)" && echo "    $PGAA_PKG: installed" || { PGAA_PKG=""; warn "    Analytics Accelerator package not found ($PGAA_CANDS), skipping"; }
fi

fi
[ -x "$PSQL" ] || die "Expected $PSQL but it is missing. Is Postgres ($FLAVOR $PGVER) installed? Use --flavor and --version to match it, or run without --configure-only."
if [ -n "$CONFIGURE_ONLY" ]; then
  for c in $PGAA_CANDS; do pkg_has "$c" && { PGAA_PKG="$c"; break; }; done
  say "Configure only: using the existing $FLAVOR $PGVER installation"
fi

say "3/5  Postgres cluster"
if [ "$PM" = deb ] && [ "$FLAVOR" = as ]; then
  CTL="$PGHOME/bin/epas_ctlcluster"
  restart_db() { $SUDO "$CTL" "$PGVER" main restart; }
  $SUDO "$CTL" "$PGVER" main status >/dev/null 2>&1 || { $SUDO "$PGHOME/bin/epas_createcluster" "$PGVER" main 2>/dev/null || true; $SUDO "$CTL" "$PGVER" main start || true; }
elif [ "$PM" = deb ] && [ "$FLAVOR" = pg ]; then
  restart_db() { $SUDO pg_ctlcluster "$PGVER" main restart; }
  $SUDO pg_lsclusters 2>/dev/null | awk -v v="$PGVER" '$1==v&&$2=="main"{f=1} END{exit !f}' || $SUDO pg_createcluster "$PGVER" main
  $SUDO pg_ctlcluster "$PGVER" main status >/dev/null 2>&1 || $SUDO pg_ctlcluster "$PGVER" main start
elif [ "$FLAVOR" = as ]; then
  SVC="edb-as-${PGVER}"
  restart_db() { $SUDO systemctl restart "$SVC"; }
  if ! $SUDO systemctl is-active --quiet "$SVC"; then
    $SUDO env PGSETUP_INITDB_OPTIONS="-E UTF-8" "$PGHOME/bin/edb-as-${PGVER}-setup" initdb 2>/dev/null || true
    $SUDO systemctl enable --now "$SVC"
  fi
elif [ "$FLAVOR" = pg ]; then
  SVC="postgresql-${PGVER}"
  restart_db() { $SUDO systemctl restart "$SVC"; }
  if ! $SUDO systemctl is-active --quiet "$SVC"; then
    $SUDO "$PGHOME/bin/postgresql-${PGVER}-setup" initdb 2>/dev/null || true
    $SUDO systemctl enable --now "$SVC"
  fi
else
  SVC="edb-pge-${PGVER}"
  restart_db() { $SUDO systemctl restart "$SVC"; }
  $SUDO systemctl is-active --quiet "$SVC" || $SUDO systemctl enable --now "$SVC" || die "Could not start $SVC. Initialise the cluster per the EDB docs, then run this script again."
fi

say "4/5  Configure AIDB and the extensions"
PRELOAD_LIBS="aidb"
{ pkg_has "${X}-pg-textsearch" && [ "$PGVER" -ge 17 ]; } && PRELOAD_LIBS="$PRELOAD_LIBS pg_textsearch"
[ -n "$PGAA_PKG" ] && PRELOAD_LIBS="$PRELOAD_LIBS pgaa pgfs"
. ./edb/configure.sh
PASS="$(rand)"
aibox_configure "$PASS"

say "5/5  Connection settings"
write_env 127.0.0.1 "$DB_PORT" "$PASS" "$PGHOME/bin"
if PGPASSWORD="$PASS" "$PSQL" -h 127.0.0.1 -p "$DB_PORT" -U aibox -d aibox -Atc "select 'AIDB ' || extversion from pg_extension where extname='aidb'" ; then
  say "Done. Postgres with AIDB is ready. Next:  ./start.sh --setup-demo   (loads the demo data)   or   ./start.sh"
else
  die "Installed, but the aibox login over TCP failed. Check pg_hba.conf and the port ($DB_PORT)."
fi
