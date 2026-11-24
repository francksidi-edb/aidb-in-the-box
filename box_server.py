#!/usr/bin/env python3
"""
box_server.py - local backend for "AI in the Box".

Serves the catalog UI, the bundled demo notebooks, and a read-only status check
of the Postgres you point it at. Every database query goes through your own
`psql`, using the usual PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD
variables (loaded from env.sh by start.sh).

Binds to 127.0.0.1 only by default. To use it from another machine, either tunnel the port:
    ssh -L 8650:127.0.0.1:8650 -L 8643:127.0.0.1:8643 user@box
or listen on the network with a password (user "aibox"):
    AIBOX_HOST=0.0.0.0 AIBOX_PASSWORD=choose-one ./start.sh
"""
import base64
import glob
import hmac
import secrets
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))

import envfile  # noqa: E402
ENVFILE_KEYS = envfile.apply(os.path.join(BASE, ".env"))   # .env settings reach the app and every notebook (Jupyter inherits them)
STATIC = os.path.join(BASE, "static")
NOTEBOOKS = os.path.join(BASE, "notebooks")
PORT = int(os.environ.get("AIBOX_PORT", "8650"))
SHOWCASE_PORT = int(os.environ.get("SHOWCASE_PORT", "8643"))

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


LOCAL = ("127.0.0.1", "localhost")
HOST = os.environ.get("AIBOX_HOST", "127.0.0.1")
EXPOSED = HOST not in ("127.0.0.1", "localhost", "::1")
NO_AUTH = os.environ.get("AIBOX_NO_AUTH", "") not in ("", "0")   # testing only: network mode without a password
AUTH_ON = EXPOSED and not NO_AUTH
PASSWORD = os.environ.get("AIBOX_PASSWORD", "") or (secrets.token_urlsafe(12) if AUTH_ON else "")
ENV_FILE = os.path.join(BASE, "env.sh")
INSTALL_LOG = os.path.join(BASE, "install.log")
PGKEYS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


def basic_ok():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, "aibox") and hmac.compare_digest(pw, PASSWORD)


@app.before_request
def only_local():
    host = (request.host or "").split(":")[0]
    if AUTH_ON:
        # Network mode: a password replaces the loopback-only rule (this app can install software and save database passwords).
        if not basic_ok():
            return Response("Sign in with user aibox.", 401, {"WWW-Authenticate": 'Basic realm="AI in the Box"'})
    elif not EXPOSED and host not in LOCAL:
        return jsonify({"ok": False, "error": "Forbidden host."}), 403
    if request.method == "POST":
        # JSON only (forces a CORS preflight) and same-origin only, so other websites cannot drive this server.
        origin = request.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc != request.host and (EXPOSED or urllib.parse.urlparse(origin).hostname not in LOCAL):
            return jsonify({"ok": False, "error": "Forbidden origin."}), 403
        if not request.is_json:
            return jsonify({"ok": False, "error": "JSON body required."}), 415


def extend_path():
    """Find psql in the usual EDB / PGDG / Homebrew places if it is not on PATH."""
    if shutil.which("psql"):
        return
    for pat in ("/usr/edb/*/bin", "/usr/lib/postgresql/*/bin", "/opt/homebrew/opt/libpq/bin", "/usr/local/opt/libpq/bin"):
        for d in sorted(glob.glob(pat), reverse=True):
            if os.path.exists(os.path.join(d, "psql")):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                return


def load_env_file():
    """Apply env.sh (written by install-edb.sh or the Connect form) to this process."""
    try:
        text = open(ENV_FILE, encoding="utf-8").read()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        try:
            parts = shlex.split(line[7:])
        except ValueError:
            continue
        for part in parts:
            k, _, v = part.partition("=")
            if k in PGKEYS:
                os.environ[k] = v
            elif k == "PATH" and ":$PATH" in v:
                d = v.split(":$PATH")[0]
                if d and d not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def psql(sql, timeout=15, extra_env=None):
    """Run one read-only statement through psql. Returns (ok, rows | error text)."""
    if not shutil.which("psql"):
        return False, "psql is not installed or not on PATH."
    env = os.environ.copy()
    env.update(extra_env or {})
    env.setdefault("PGCONNECT_TIMEOUT", "5")
    env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    try:
        p = subprocess.run(
            ["psql", "-X", "-q", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "psql timed out."
    if p.returncode != 0:
        return False, ((p.stderr or "psql failed").strip().splitlines() or ["psql failed"])[0]
    return True, [line.split("|") for line in p.stdout.splitlines() if line.strip()]


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/status")
def status():
    conn = {k: os.environ.get(k, "") or ("(socket)" if k == "PGHOST" else "")
            for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")}
    out = {"psql": shutil.which("psql") is not None, "connection": conn,
           "introspector": True, "showcase_running": True, "showcase_port": SHOWCASE_PORT,
           "showcase_installed": os.path.isdir(os.path.join(BASE, "aidb-showcase"))}
    ok, rows = psql("select version()")
    out["connected"] = ok
    if not ok:
        out["error"] = rows
        return jsonify(out)
    out["server"] = rows[0][0] if rows else ""
    wanted = ("aidb", "vector", "edb_vectorplus", "pgfs", "pg_textsearch", "pgaa", "age")
    ok, rows = psql("select extname, extversion from pg_extension where extname in (%s)"
                    % ",".join("'%s'" % w for w in wanted))
    installed = {r[0]: r[1] for r in rows} if ok else {}
    ok2, avail = psql("select name from pg_available_extensions where name in (%s)"
                      % ",".join("'%s'" % w for w in wanted))
    available = {r[0] for r in avail} if ok2 else set()
    out["extensions"] = [{"name": w, "installed": installed.get(w),
                          "available": w in available or w in installed} for w in wanted]
    ok, rows = psql("select to_regnamespace('shop') is not null")
    out["demo_data"] = bool(ok and rows and rows[0][0] == "t")
    if "aidb" in installed:
        ok, rows = psql("select (select count(*) from aidb.models), (select count(*) from aidb.agents)")
        if ok and rows:
            out["aidb_counts"] = {"models": int(rows[0][0]), "agents": int(rows[0][1])}
    return jsonify(out)


def clean(v, name, maxlen=200):
    v = str(v or "").strip()
    if len(v) > maxlen or any(c in v for c in "\r\n\x00"):
        raise ValueError("Invalid " + name + ".")
    return v


@app.post("/api/connect")
def connect():
    """Test a connection with the given settings; on success keep it and save it to env.sh."""
    d = request.get_json(silent=True) or {}
    try:
        vals = {"PGHOST": clean(d.get("host"), "host") or "127.0.0.1",
                "PGPORT": clean(d.get("port"), "port", 5) or "5432",
                "PGDATABASE": clean(d.get("database"), "database") or "postgres",
                "PGUSER": clean(d.get("user"), "user") or "postgres",
                "PGPASSWORD": clean(d.get("password"), "password", 500)}
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not vals["PGPORT"].isdigit():
        return jsonify({"ok": False, "error": "Port must be a number."}), 400
    if not vals["PGPASSWORD"] and not d.get("keep_password"):
        vals["PGPASSWORD"] = ""
    elif not vals["PGPASSWORD"]:
        vals["PGPASSWORD"] = os.environ.get("PGPASSWORD", "")
    ports = [vals["PGPORT"]] if str(d.get("port") or "").strip() else ["5432", "5444"]   # blank: try Postgres, then EDB Advanced Server
    for port in ports:
        vals["PGPORT"] = port
        ok, res = psql("select version()", extra_env=vals)
        if ok or "refused" not in str(res).lower():
            break
    if not ok:
        hint = ""
        if "refused" in str(res).lower() or "no such file" in str(res).lower():
            hint = (" Nothing is listening on %s:%s. Start Postgres with the Database service panel below, or check the port"
                    " (EDB Postgres Advanced Server defaults to 5444, PostgreSQL to 5432)." % (vals["PGHOST"], ports[-1]))
        elif "password" in str(res).lower() or "authentication" in str(res).lower():
            hint = " Check the user and password (EDB Advanced Server's default superuser is enterprisedb, database edb)."
        elif "does not exist" in str(res).lower():
            hint = " Check the database and user names."
        return jsonify({"ok": False, "error": str(res) + hint})
    keep = []
    try:
        for line in open(ENV_FILE, encoding="utf-8").read().splitlines():
            if not re.match(r"\s*export\s+PG(HOST|PORT|DATABASE|USER|PASSWORD)=", line) and "PGHOST=" not in line:
                keep.append(line)
    except OSError:
        pass
    body = "\n".join(keep + ["# Saved from the Connect page.",
                            "export PGHOST=%s PGPORT=%s PGDATABASE=%s PGUSER=%s" % tuple(
                                shlex.quote(vals[k]) for k in PGKEYS[:4])])
    if vals["PGPASSWORD"]:
        body += "\nexport PGPASSWORD=" + shlex.quote(vals["PGPASSWORD"])
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    os.chmod(ENV_FILE, 0o600)
    for k, v in vals.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return jsonify({"ok": True, "server": res[0][0] if res else ""})


JOB = {"proc": None, "token": "", "args": []}
JOB_LOCK = threading.Lock()


def job_running():
    p = JOB["proc"]
    return p is not None and p.poll() is None


@app.post("/api/install")
def install():
    """Run install-edb.sh with the token passed through the environment (never an argument, never logged)."""
    d = request.get_json(silent=True) or {}
    flavor = str(d.get("flavor") or "as")
    version = str(d.get("version") or "18")
    repo = str(d.get("repo") or "")
    token = str(d.get("token") or "").strip()
    docker, check = bool(d.get("docker")), bool(d.get("check"))
    conf_only = bool(d.get("configure_only"))
    remote = bool(d.get("remote"))
    if flavor not in ("as", "pg", "pge") or not re.fullmatch(r"1[4-9]", version) or (repo and not re.fullmatch(r"[a-z0-9_-]{1,40}", repo)):
        return jsonify({"ok": False, "error": "Invalid options."}), 400
    if not check and not conf_only and not re.fullmatch(r"[A-Za-z0-9_.~-]{8,200}", token):
        return jsonify({"ok": False, "error": "Enter your EDB subscription token."}), 400
    if not check and not docker and os.name == "posix" and os.geteuid() != 0:
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
            return jsonify({"ok": False, "error": "Installing packages needs root or passwordless sudo, and this server has neither. "
                            "Run ./install-edb.sh in a terminal instead, or start this app with sudo."}), 400
    with JOB_LOCK:
        if job_running():
            return jsonify({"ok": False, "error": "An install is already running."}), 409
        args = ["bash", os.path.join(BASE, "install-edb.sh"), "--flavor", flavor, "--version", version, "-y"]
        if repo:
            args += ["--repo", repo]
        if conf_only:
            args.append("--configure-only")
        if docker:
            args.append("--docker")
        if check:
            args.append("--check")
        env = os.environ.copy()
        env["TERM"] = "dumb"
        if remote:
            env["AIBOX_REMOTE"] = "1"   # listen on all addresses, port 5444, and add the 0.0.0.0/0 pg_hba.conf line (edb/configure.sh)
        if token and not check and not conf_only:
            env["EDB_SUBSCRIPTION_TOKEN"] = token
        else:
            env.pop("EDB_SUBSCRIPTION_TOKEN", None)
        log = open(INSTALL_LOG, "wb")
        os.chmod(INSTALL_LOG, 0o600)
        JOB["proc"] = subprocess.Popen(args, cwd=BASE, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        JOB["token"] = token
        JOB["check"] = check
        log.close()
    return jsonify({"ok": True})


@app.get("/api/install/log")
def install_log():
    try:
        text = open(INSTALL_LOG, encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    if JOB.get("token"):
        text = text.replace(JOB["token"], "***")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    p = JOB["proc"]
    state = "idle" if p is None else ("running" if p.poll() is None else ("done" if p.returncode == 0 else "failed"))
    if state == "done" and not JOB.get("check"):
        load_env_file()
    return jsonify({"state": state, "text": text[-20000:], "check": bool(JOB.get("check"))})


# ---------------------------------------------------------------- database service (start / stop / restart)
SVC_RE = re.compile(r"^(edb-as-\d+|edb-pge-\d+|postgresql-\d+|postgresql)\.service", re.M)


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def priv(user=None):
    """Command prefix to run as root, or as `user` (the data directory's owner)."""
    if os.geteuid() == 0:
        return ["runuser", "-u", user, "--"] if user else []
    return ["sudo", "-n"] + (["-u", user] if user else [])


def can_priv():
    return os.geteuid() == 0 or run(["sudo", "-n", "true"])[0] == 0


def vernum(path):
    m = re.search(r"(\d+)", re.sub(r".*/(as|pge|pgsql-|postgresql/)", "", path))
    return int(m.group(1)) if m else 0


def find_service():
    """Find how this machine runs Postgres: systemd unit, EPAS/PG cluster tools (Debian), or plain pg_ctl."""
    if os.path.isdir("/run/systemd/system") and shutil.which("systemctl"):
        rc, out = run(["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"])
        units = sorted({m.group(1) for m in SVC_RE.finditer(out)},
                       key=lambda u: (-int((re.findall(r"\d+", u) or ["0"])[0]), u))
        if units:
            return {"kind": "systemd", "name": units[0], "label": units[0]}
    for ctl in sorted(glob.glob("/usr/edb/as*/bin/epas_ctlcluster"), key=vernum, reverse=True):
        ver = re.search(r"/as(\d+)/", ctl).group(1)
        return {"kind": "epas", "ctl": ctl, "ver": ver, "label": "EDB Postgres Advanced Server %s (cluster main)" % ver}
    if shutil.which("pg_lsclusters"):
        rc, out = run(["pg_lsclusters", "--no-header"])
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4:
                return {"kind": "pgcluster", "ver": f[0], "cluster": f[1], "label": "PostgreSQL %s (cluster %s)" % (f[0], f[1])}
    for pat in ("/var/lib/edb/as*/data", "/var/lib/edb/pge*/data", "/var/lib/pgsql/*/data"):
        for data in sorted(glob.glob(pat), key=vernum, reverse=True):
            ver = vernum(data)
            for bpat in ("/usr/edb/as%d/bin/pg_ctl", "/usr/edb/pge%d/bin/pg_ctl", "/usr/pgsql-%d/bin/pg_ctl"):
                b = bpat % ver
                if os.path.exists(b):
                    import pwd
                    return {"kind": "pgctl", "pgctl": b, "data": data, "user": pwd.getpwuid(os.stat(data).st_uid).pw_name,
                            "label": "Postgres data directory " + data}
    return None


def svc_state(svc):
    k = svc["kind"]
    if k == "systemd":
        return run(["systemctl", "is-active", svc["name"]])[1].strip() or "unknown"
    if k == "epas":
        return "active" if run(priv() + [svc["ctl"], svc["ver"], "main", "status"])[0] == 0 else "inactive"
    if k == "pgcluster":
        out = run(["pg_lsclusters", "--no-header"])[1]
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4 and f[0] == svc["ver"] and f[1] == svc["cluster"]:
                return "active" if f[3].startswith("online") else "inactive"
        return "unknown"
    if k == "pgctl":
        return "active" if run(priv(svc["user"]) + [svc["pgctl"], "-D", svc["data"], "status"])[0] == 0 else "inactive"
    return "unknown"


def svc_cmd(svc, action):
    k = svc["kind"]
    if k == "systemd":
        return priv() + ["systemctl", action, svc["name"]]
    if k == "epas":
        return priv() + [svc["ctl"], svc["ver"], "main", action]
    if k == "pgcluster":
        return priv() + ["pg_ctlcluster", svc["ver"], svc["cluster"], action]
    extra = ["-m", "fast"] if action in ("stop", "restart") else []
    return priv(svc["user"]) + [svc["pgctl"], "-D", svc["data"], "-w", "-t", "90"] + extra + (
        ["-l", os.path.join(svc["data"], "aibox-pg.log")] if action in ("start", "restart") else []) + [action]


@app.get("/api/service")
def service_info():
    svc = find_service()
    if not svc:
        return jsonify({"found": False})
    return jsonify({"found": True, "label": svc["label"], "state": svc_state(svc), "can_control": can_priv()})


@app.post("/api/service")
def service_act():
    action = str((request.get_json(silent=True) or {}).get("action") or "")
    if action not in ("start", "stop", "restart", "reload"):
        return jsonify({"ok": False, "error": "Unknown action."}), 400
    svc = find_service()
    if not svc:
        return jsonify({"ok": False, "error": "No Postgres service or data directory found on this machine."}), 404
    if not can_priv():
        return jsonify({"ok": False, "error": "Starting and stopping needs root or passwordless sudo, and this server has neither. "
                        "Run the command in a terminal, or start this app with sudo."}), 400
    cmd = svc_cmd(svc, action)
    rc, out = run(cmd, timeout=150)
    return jsonify({"ok": rc == 0, "output": out.strip()[-1500:], "state": svc_state(svc),
                    "error": None if rc == 0 else (out.strip().splitlines() or ["The command failed."])[-1]})



# ---------------------------------------------------------------- enable the AI extensions in the connected database
EXT_ORDER = (("aidb", "aidb CASCADE"), ("pgfs", "pgfs"), ("edb_vectorplus", "edb_vectorplus CASCADE"),
             ("pg_textsearch", "pg_textsearch"), ("pgaa", "pgaa CASCADE"))
PRELOAD_FOR = {"aidb": ["aidb"], "pg_textsearch": ["pg_textsearch"], "pgaa": ["pgaa", "pgfs"]}


def psql_rw(sql, timeout=120):
    """One writing statement (superuser needed for ALTER SYSTEM / CREATE EXTENSION)."""
    if not shutil.which("psql"):
        return False, "psql is not installed or not on PATH."
    env = os.environ.copy()
    env.setdefault("PGCONNECT_TIMEOUT", "5")
    p = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        return False, ((p.stderr or "psql failed").strip().splitlines() or ["psql failed"])[0]
    return True, p.stdout.strip()


@app.post("/api/enable")
def enable_extensions():
    ok, rows = psql("select name from pg_available_extensions where name in ('aidb','pgfs','edb_vectorplus','pg_textsearch','pgaa','vector')")
    if not ok:
        return jsonify({"ok": False, "error": rows}), 400
    avail = {r[0] for r in rows}
    if "aidb" not in avail:
        return jsonify({"ok": False, "error": "The aidb extension is not installed on this server (package missing). Run the installer first."}), 400
    ok, rows = psql("select current_setting('server_version_num')::int, (select rolsuper from pg_roles where rolname = current_user), current_setting('shared_preload_libraries')")
    if not ok:
        return jsonify({"ok": False, "error": rows}), 400
    major, sup, cur = int(rows[0][0]) // 10000, rows[0][1] == "t", rows[0][2]
    if not sup:
        return jsonify({"ok": False, "error": "This login is not a superuser, so it cannot enable extensions. Use Prepare this instance (creates the aibox superuser), or connect as a superuser."}), 400
    steps, have = [], [x.strip() for x in cur.split(",") if x.strip()]
    want = list(have)
    for ext, libs in PRELOAD_FOR.items():
        if ext in avail and not (ext == "pg_textsearch" and major < 17):
            for lib in libs:
                if lib not in want:
                    want.append(lib)
    if want != have:
        lits = ", ".join("'%s'" % w for w in want)
        ok, out = psql_rw("ALTER SYSTEM SET shared_preload_libraries = " + lits)
        if not ok:
            return jsonify({"ok": False, "error": out}), 400
        if "pgaa" in want:
            ok, out = psql_rw("ALTER SYSTEM SET pgaa.autostart_seafowl = on")   # fails before the library is loaded: ignored, set after restart
        steps.append("preload list set to: " + ", ".join(want))
        svc = find_service()
        if not svc or not can_priv():
            return jsonify({"ok": False, "steps": steps, "error": "Saved the preload list, but this app cannot restart Postgres (needs root or passwordless sudo). "
                            "Restart Postgres yourself, then press Enable again."}), 400
        rc, out = run(svc_cmd(svc, "restart"), timeout=150)
        if rc != 0:
            return jsonify({"ok": False, "steps": steps, "error": "Restart failed: " + ((out.strip().splitlines() or [""])[-1])}), 500
        steps.append("restarted Postgres")
        import time
        for _ in range(60):
            if psql("select 1")[0]:
                break
            time.sleep(1)
        else:
            return jsonify({"ok": False, "steps": steps, "error": "Postgres did not come back after the restart."}), 500
        if "pgaa" in want:
            psql_rw("ALTER SYSTEM SET pgaa.autostart_seafowl = on")
            psql_rw("select pg_reload_conf()")
    results = []
    for ext, stmt in EXT_ORDER:
        if ext not in avail:
            results.append({"name": ext, "ok": False, "detail": "not installed on this server"})
            continue
        ok, out = psql_rw("CREATE EXTENSION IF NOT EXISTS " + stmt)
        results.append({"name": ext, "ok": ok, "detail": "enabled" if ok else out})
    if any(r["name"] == "aidb" and r["ok"] for r in results):
        psql_rw("GRANT aidb_users TO current_user")
    return jsonify({"ok": all(r["ok"] or r["detail"].startswith("not installed") for r in results), "steps": steps, "results": results})



# ---------------------------------------------------------------- JupyterLab for the demo notebooks
JUP_DIR = os.path.join(BASE, "notebooks-run")
JUP_LOG = os.path.join(BASE, "jupyter.log")
JUP_PORT = int(os.environ.get("AIBOX_JUPYTER_PORT", "8888"))
JUP_PKGS = ["jupyterlab", "jupysql", "sqlalchemy", "psycopg2-binary", "pandas", "datasets"]
JUP = {"state": "stopped", "proc": None, "token": "", "error": ""}
ENGINE_CELL = (
    "from sqlalchemy import create_engine\n"
    "from urllib.parse import quote_plus\n"
    "import os\n"
    "\n"
    "# Connection supplied by AI in the Box: the same PG* settings as the Connect page (TCP, with password).\n"
    "conn = create_engine(\n"
    "    \"postgresql+psycopg2://%s:%s@%s:%s/%s\" % (\n"
    "        quote_plus(os.environ.get(\"PGUSER\", \"postgres\")), quote_plus(os.environ.get(\"PGPASSWORD\", \"\")),\n"
    "        os.environ.get(\"PGHOST\", \"127.0.0.1\"), os.environ.get(\"PGPORT\", \"5432\"), os.environ.get(\"PGDATABASE\", \"postgres\")),\n"
    "    pool_pre_ping=True)\n"
    "\n"
    "%reload_ext sql\n"
    "%sql conn\n"
    "%config SqlMagic.named_parameters = \"enabled\"\n")


def prepare_notebooks():
    """Copy the demo notebooks to notebooks-run/ (never overwriting your edits) with the database connection cell
    pointed at this app's connection instead of a local Unix socket as the OS user."""
    os.makedirs(JUP_DIR, exist_ok=True)
    for f in sorted(os.listdir(NOTEBOOKS)):   # helper modules the Governance notebooks import (lab.py) sit next to them
        if f.endswith(".py") and not os.path.exists(os.path.join(JUP_DIR, f)):
            shutil.copy(os.path.join(NOTEBOOKS, f), os.path.join(JUP_DIR, f))
    for f in nb_files():
        dst = os.path.join(JUP_DIR, f)
        if os.path.exists(dst):
            continue
        nb = load_nb(f)
        for c in nb.get("cells", []):
            text = src(c)
            if c.get("cell_type") == "code" and "create_engine(" in text:
                tail = text.split("%config SqlMagic.named_parameters", 1)
                extra = tail[1].split("\n", 1)[1] if len(tail) > 1 and "\n" in tail[1] else ""
                c["source"] = (ENGINE_CELL + extra).splitlines(keepends=True)
                break
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(nb, fh, indent=1)


def jupyter_worker():
    import socket
    import sys
    import time
    import importlib.util
    try:
        with open(JUP_LOG, "wb") as log:
            if importlib.util.find_spec("jupyterlab") is None or importlib.util.find_spec("sql") is None:
                JUP["state"] = "installing"
                rc = subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + JUP_PKGS, stdout=log, stderr=subprocess.STDOUT).returncode
                if rc != 0:
                    raise RuntimeError("pip could not install JupyterLab and its packages (see jupyter.log).")
            JUP["state"] = "starting"
            prepare_notebooks()
            ip = "0.0.0.0" if EXPOSED else "127.0.0.1"
            cmd = [sys.executable, "-m", "jupyterlab", "--no-browser", "--ServerApp.ip=" + ip, "--port=%d" % JUP_PORT,
                   "--ServerApp.port_retries=0", "--ServerApp.token=" + JUP["token"], "--notebook-dir=" + JUP_DIR]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                cmd.append("--allow-root")
            JUP["proc"] = subprocess.Popen(cmd, cwd=JUP_DIR, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            for _ in range(120):
                if JUP["proc"].poll() is not None:
                    raise RuntimeError("JupyterLab exited at start-up. Is port %d already in use? See jupyter.log." % JUP_PORT)
                try:
                    socket.create_connection(("127.0.0.1", JUP_PORT), timeout=1).close()
                    JUP["state"] = "running"
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("JupyterLab did not start within 2 minutes (see jupyter.log).")
            JUP["proc"].wait()
        if JUP["state"] == "running":
            JUP["state"] = "stopped"
    except Exception as e:  # noqa: BLE001
        JUP["state"], JUP["error"] = "failed", str(e)


def jupyter_stop():
    p = JUP.get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    JUP["state"] = "stopped"


import atexit  # noqa: E402
atexit.register(jupyter_stop)


@app.get("/api/jupyter")
def jupyter_info():
    tail = ""
    try:
        tail = open(JUP_LOG, encoding="utf-8", errors="replace").read()[-3000:]
    except OSError:
        pass
    running = JUP["state"] == "running"
    return jsonify({"state": JUP["state"], "port": JUP_PORT, "token": JUP["token"] if running else "",
                    "error": JUP["error"], "log": tail if JUP["state"] in ("installing", "starting", "failed") else ""})


@app.post("/api/jupyter")
def jupyter_act():
    action = str((request.get_json(silent=True) or {}).get("action") or "")
    if action == "stop":
        jupyter_stop()
        return jsonify({"ok": True})
    if action != "start":
        return jsonify({"ok": False, "error": "Unknown action."}), 400
    if JUP["state"] in ("installing", "starting", "running"):
        return jsonify({"ok": True})
    JUP.update({"state": "installing", "error": "", "token": secrets.token_urlsafe(18)})
    threading.Thread(target=jupyter_worker, daemon=True).start()
    return jsonify({"ok": True})



def showcase_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/" % SHOWCASE_PORT, timeout=1)
        return True
    except urllib.error.HTTPError as e:
        return e.code == 401   # network mode: the showcase is up but asks for the password
    except Exception:
        return False


def nb_files():
    return sorted(f for f in os.listdir(NOTEBOOKS) if f.endswith(".ipynb"))


def load_nb(name):
    with open(os.path.join(NOTEBOOKS, name), encoding="utf-8") as fh:
        return json.load(fh)


def src(cell):
    s = cell.get("source", "")
    return "".join(s) if isinstance(s, list) else s


@app.get("/api/notebooks")
def notebooks():
    items = []
    for f in nb_files():
        nb = load_nb(f)
        cells = nb.get("cells", [])
        title = next((re.sub(r"^#+\s*", "", src(c).splitlines()[0]) for c in cells
                      if c.get("cell_type") == "markdown" and src(c).strip()), f)
        items.append({"file": f, "title": title, "cells": len(cells),
                      "code_cells": sum(1 for c in cells if c.get("cell_type") == "code")})
    return jsonify(items)


@app.get("/api/notebooks/<name>")
def notebook(name):
    if name not in nb_files():
        abort(404)
    cells = [{"type": c.get("cell_type"), "source": src(c)} for c in load_nb(name).get("cells", [])]
    return jsonify({"file": name, "cells": cells})


@app.get("/notebooks/<name>")
def notebook_download(name):
    if name not in nb_files():
        abort(404)
    return send_from_directory(NOTEBOOKS, name, as_attachment=True)


extend_path()
load_env_file()
envfile.apply(os.path.join(BASE, ".env"))   # .env wins over env.sh

# The AIDB Introspector lives at /introspect/ (same origin, same password, no extra port).
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402
import introspect_mount  # noqa: E402
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/introspect": introspect_mount.build_app(EXPOSED, PASSWORD, AUTH_ON)})

if __name__ == "__main__":
    if EXPOSED and NO_AUTH:
        print("AI in the Box listening on %s:%d with NO PASSWORD (AIBOX_NO_AUTH). Anyone who can reach this port can use it: testing only." % (HOST, PORT))
    elif EXPOSED:
        print("AI in the Box listening on %s:%d. Sign in with user: aibox   password: %s" % (HOST, PORT, PASSWORD))
        print("Open the ports in your firewall / EC2 security group, and prefer HTTPS or an SSH tunnel on untrusted networks.")
    else:
        print("AI in the Box  ->  http://127.0.0.1:%d" % PORT)
    if os.environ.get("AIBOX_JUPYTER_AUTOSTART", "") not in ("", "0"):   # the all-in-one Docker image starts JupyterLab with the app
        JUP.update({"state": "installing", "error": "", "token": os.environ.get("AIBOX_JUPYTER_TOKEN") or secrets.token_urlsafe(18)})
        threading.Thread(target=jupyter_worker, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False)
