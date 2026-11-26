#!/usr/bin/env python3
"""
box_server.py - local backend for "AI in the Box".

Serves the catalog UI, the bundled demo notebooks, the Inspect pages, the demo apps (webapps/) and a read-only
status check of the Postgres it runs next to. The status check and Inspect go through `psql` and the apps use
SQLAlchemy, both with the usual PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD variables (set by docker/entrypoint.sh).

Binds to 127.0.0.1 only by default. In the container it listens on all addresses (AIBOX_HOST=0.0.0.0) with
no password (AIBOX_NO_AUTH=1) and docker-compose.yml publishes the port on localhost only. Otherwise set
AIBOX_PASSWORD (user "aibox") to require a sign-in. Without a password only requests for localhost / 127.0.0.1
(plus AIBOX_ALLOWED_HOSTS, comma-separated) are served, so a DNS-rebinding page cannot reach the app.
"""
import base64
import glob
import hmac
import secrets
import json
import os
import re
import shutil
import subprocess
import urllib.parse

from flask import Flask, Response, abort, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))

STATIC = os.path.join(BASE, "static")
NOTEBOOKS = os.path.join(BASE, "notebooks")
PORT = int(os.environ.get("AIBOX_PORT", "8650"))

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # PDF uploads for Customs Document Validation


LOCAL = ("127.0.0.1", "localhost", "::1")
HOST = os.environ.get("AIBOX_HOST", "127.0.0.1")
EXPOSED = HOST not in ("127.0.0.1", "localhost", "::1")
NO_AUTH = os.environ.get("AIBOX_NO_AUTH", "") not in ("", "0")   # testing only: network mode without a password
AUTH_ON = EXPOSED and not NO_AUTH
PASSWORD = os.environ.get("AIBOX_PASSWORD", "") or (secrets.token_urlsafe(12) if AUTH_ON else "")
ALLOWED_HOSTS = set(LOCAL) | {h.strip().lower() for h in os.environ.get("AIBOX_ALLOWED_HOSTS", "").split(",") if h.strip()}


def basic_ok():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user, "aibox") and hmac.compare_digest(pw, PASSWORD)


def access_denied():
    """The sign-in or host check shared with the Introspector mount. Returns an error response, or None."""
    if AUTH_ON:
        # Network mode: a password replaces the host rule (this app can install software and save database passwords).
        if not basic_ok():
            return Response("Sign in with user aibox.", 401, {"WWW-Authenticate": 'Basic realm="AI in the Box"'})
    elif urllib.parse.urlsplit("//" + (request.host or "")).hostname not in ALLOWED_HOSTS:
        # No password (also with AIBOX_NO_AUTH): only known host names, so DNS rebinding cannot reach this server.
        return jsonify({"ok": False, "error": "Forbidden host."}), 403
    return None


@app.before_request
def only_local():
    denied = access_denied()
    if denied:
        return denied
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


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(STATIC, "favicon.ico")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name)


@app.get("/api/jupyter")
def jupyter():
    # The token docker/entrypoint.sh gives Jupyter, for the "Open in Jupyter" links.
    return jsonify({"token": os.environ.get("JUPYTER_TOKEN", "")})


@app.get("/api/status")
def status():
    conn = {k: os.environ.get(k, "") or ("(socket)" if k == "PGHOST" else "")
            for k in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER")}
    out = {"psql": shutil.which("psql") is not None, "connection": conn,
           "introspector": True}
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

# The demo apps (Converse, Customs Document Validation, Token Boss): JSON APIs under /api/<app>/.
import webapps  # noqa: E402
webapps.register(app)

# The AIDB Introspector lives at /introspect/ (same origin, same password, no extra port).
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402
import introspect_mount  # noqa: E402
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/introspect": introspect_mount.build_app(access_denied)})

if __name__ == "__main__":
    if EXPOSED and NO_AUTH:
        print("AI in the Box listening on %s:%d with NO PASSWORD (AIBOX_NO_AUTH): testing only. Only these host names are "
              "served (add more with AIBOX_ALLOWED_HOSTS): %s" % (HOST, PORT, ", ".join(sorted(ALLOWED_HOSTS))))
    elif EXPOSED:
        print("AI in the Box listening on %s:%d. Sign in with user: aibox   password: %s" % (HOST, PORT, PASSWORD))
        print("Open the ports in your firewall / EC2 security group, and prefer HTTPS or an SSH tunnel on untrusted networks.")
    else:
        print("AI in the Box  ->  http://127.0.0.1:%d" % PORT)
    app.run(host=HOST, port=PORT, debug=False)
