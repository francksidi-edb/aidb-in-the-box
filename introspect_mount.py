"""Mounts the AIDB Introspector (introspector/aidb_introspect_server.py, unmodified) inside AI in the Box at /introspect/.
Same origin, same password, no second port. Only its loopback-only guard is replaced (by this app's sign-in or host
check), and its saved-connection lookup returns this app's PG* settings so the page connects without retyping them."""
import importlib.util
import os

from flask import jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))


def build_app(access_denied):
    path = os.path.join(HERE, "introspector", "aidb_introspect_server.py")
    spec = importlib.util.spec_from_file_location("aidb_introspect_server", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    def guard():
        denied = access_denied()
        if denied:
            return denied
        if request.method == "POST" and request.headers.get("X-Requested-By") != "aidb-introspect":
            return jsonify({"ok": False, "error": "Missing X-Requested-By header."}), 403
        if request.method == "GET" and request.path == "/api/local-config":
            cfg = {"host": os.environ.get("PGHOST", ""), "port": os.environ.get("PGPORT", ""),
                   "dbname": os.environ.get("PGDATABASE", ""), "user": os.environ.get("PGUSER", ""),
                   "password": os.environ.get("PGPASSWORD", "")}
            return jsonify({"ok": True, "config": {k: v for k, v in cfg.items() if v}, "from": "AI in the Box"})

    funcs = m.app.before_request_funcs.get(None, [])
    m.app.before_request_funcs[None] = [f for f in funcs if f.__name__ != "guard_local_api"] + [guard]
    return m.app
