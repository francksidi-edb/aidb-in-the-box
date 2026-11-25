#!/usr/bin/env bash
# One-time setup for AI in the Box. Safe to run again.
# Works on Linux and macOS, arm64 or x86_64. Installs what is missing (python3 3.9+, venv, git, psql client) using
# apt, dnf, yum or Homebrew, after asking. Set AIBOX_YES=1 to skip the question.
set -e
cd "$(dirname "$0")"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "0/3  System dependencies"
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
OS="$(uname -s)"; PM=""
if [ "$OS" = Darwin ]; then command -v brew >/dev/null && PM=brew
elif command -v apt-get >/dev/null; then PM=apt
elif command -v dnf >/dev/null; then PM=dnf
elif command -v yum >/dev/null; then PM=yum; fi

py_ok() { command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; }
venv_ok() { python3 -c 'import venv, ensurepip' >/dev/null 2>&1; }
MISSING=""
py_ok || MISSING="$MISSING python3(3.9+)"
{ py_ok && venv_ok; } || MISSING="$MISSING python-venv"
command -v git >/dev/null || command -v curl >/dev/null || MISSING="$MISSING git/curl"
command -v git >/dev/null || MISSING="$MISSING git"
command -v psql >/dev/null || MISSING="$MISSING psql"

if [ -z "$MISSING" ]; then
  echo "    all present"
else
  echo "    missing:$MISSING"
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO=sudo
  case "$PM" in
    "") die "Cannot install them automatically here (no apt, dnf, yum or Homebrew). Install python3 (3.9+), venv support, git and the psql client, then run ./install.sh again." ;;
    brew) ;;
    *) if [ -n "$SUDO" ] && ! command -v sudo >/dev/null; then die "Need root or sudo to install:$MISSING"; fi ;;
  esac
  if [ -z "${AIBOX_YES:-}" ] && [ -t 0 ]; then
    read -rp "    Install the missing packages now${SUDO:+ (uses sudo)}? [Y/n] " ans
    case "$ans" in n|N) die "Stopped. Install:$MISSING, then run ./install.sh again." ;; esac
  fi
  pm_install() {
    case "$PM" in
      apt) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
      dnf) $SUDO dnf install -y "$@" ;;
      yum) $SUDO yum install -y "$@" ;;
      brew) brew install "$@" ;;
    esac
  }
  [ "$PM" = apt ] && $SUDO apt-get update -qq
  # required: each line is one install call so a missing optional package cannot block the rest
  if ! { py_ok && venv_ok; }; then
    case "$PM" in
      apt) pm_install python3 python3-venv python3-pip ;;
      dnf|yum) pm_install python3 python3-pip ;;
      brew) pm_install python ;;
    esac || true
  fi
  if ! command -v git >/dev/null; then pm_install git || warn "    git not installed (curl will be used instead)"; fi
  if ! command -v git >/dev/null && ! command -v curl >/dev/null; then pm_install curl || true; fi
  if ! command -v psql >/dev/null; then
    case "$PM" in
      apt) cands="postgresql-client" ;;
      dnf|yum) cands="postgresql postgresql16 postgresql15 postgresql17" ;;
      brew) cands="libpq" ;;
    esac
    for c in $cands; do pm_install "$c" >/dev/null 2>&1 && break; done
    [ "$PM" = brew ] && brew link --force libpq >/dev/null 2>&1 || true
  fi
fi
[ "$OS" = Darwin ] && [ -d /opt/homebrew/opt/libpq/bin ] && export PATH="/opt/homebrew/opt/libpq/bin:$PATH"
py_ok || die "python3 3.9 or newer is required (found: $(python3 --version 2>&1 || echo none)). On Amazon Linux 2 use Amazon Linux 2023, or install a newer Python."
if command -v psql >/dev/null; then
  echo "    psql: $(command -v psql)"
else
  warn "    psql client not installed. Continuing: ./install-edb.sh installs a server that includes it, or install the client yourself."
fi

say "1/3  Python environment (./venv)"
# A venv missing its activate script is a failed earlier attempt: start clean.
[ -f venv/bin/activate ] || rm -rf venv
if [ ! -d venv ]; then
  if ! python3 -m venv venv 2>/dev/null; then
    rm -rf venv
    echo "    python3 -m venv failed; trying virtualenv instead"
    python3 -m pip install --user -q virtualenv 2>/dev/null || python3 -m pip install --user -q --break-system-packages virtualenv 2>/dev/null || true
    python3 -m virtualenv -q venv 2>/dev/null || { rm -rf venv; die "Could not create ./venv. On Debian/Ubuntu run: sudo apt install python3-venv   (then ./install.sh again)"; }
  fi
fi
. venv/bin/activate
pip install -q --upgrade pip >/dev/null 2>&1 || true
pip install -q flask || die "pip could not install Flask. Check your network or proxy, then run ./install.sh again."
python -c "import flask" || die "Flask is not importable in ./venv."
echo "    ready: $(python --version 2>&1), Flask $(python -c 'import flask,importlib.metadata as m;print(m.version("flask"))')"

say "2/3  Connection settings"
if [ ! -f env.sh ]; then cp env.sh.example env.sh; echo "    created env.sh - edit it, or put the settings in .env (see .env.example)"; else echo "    env.sh exists"; fi

say "Done. Next:  ./start.sh"
