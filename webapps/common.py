"""Shared helpers: database engine, JSON responses, and background jobs for slow steps."""
import contextvars
import datetime
import decimal
import functools
import json
import os
import threading
import time
import uuid

import pandas as pd
from flask import Blueprint, Response, request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

bp = Blueprint("jobs", __name__)
_engines = {}
_lock = threading.Lock()

# Which database user a request uses: "app" (AIBOX_DB_USER, demo_user in the container) unless its blueprint
# picked another with db_role(), such as "monitor" for Token Boss (AIBOX_MONITOR_USER, demo_monitor).
DB_ROLE = contextvars.ContextVar("db_role", default="app")
ROLE_ENV = {"app": "AIBOX_DB", "monitor": "AIBOX_MONITOR"}
_blueprint_roles = {}


def db_role(blueprint, role):
    """Run the requests of `blueprint` (and the jobs they start) as database role `role`."""
    if role not in ROLE_ENV:
        raise ValueError(f"Unknown database role {role!r}; known: {', '.join(ROLE_ENV)}")
    _blueprint_roles[blueprint.name] = role


@bp.before_app_request
def _pick_db_role():
    DB_ROLE.set(_blueprint_roles.get(request.blueprint, "app"))


def get_engine():
    """One pooled connection source per role. Uses PGHOST / PGPORT / PGDATABASE, and <prefix>_USER /
    <prefix>_PASSWORD for the role (PGUSER / PGPASSWORD when <prefix>_USER is not set), which docker/entrypoint.sh
    sets. AUTOCOMMIT so long aidb calls are not wrapped in one transaction."""
    role = DB_ROLE.get()
    with _lock:
        if role not in _engines:
            prefix, host = ROLE_ENV[role], os.getenv("PGHOST") or None
            if os.getenv(f"{prefix}_USER"):
                user, password = os.getenv(f"{prefix}_USER"), os.getenv(f"{prefix}_PASSWORD")
            else:
                user, password = os.getenv("PGUSER", "postgres"), os.getenv("PGPASSWORD")
            _engines[role] = create_engine(
                URL.create("postgresql+psycopg2", username=user, password=password or None, host=host,
                           port=int(os.getenv("PGPORT", "5432")) if host else None,
                           database=os.getenv("PGDATABASE", "aidb")),
                pool_pre_ping=True, isolation_level="AUTOCOMMIT")
        return _engines[role]


def query(sql, params=None):
    with get_engine().connect() as c:
        return pd.read_sql(text(sql), c, params=params or {})


def execute(sql, params=None):
    with get_engine().begin() as c:
        return c.execute(text(sql), params or {})


def relation_exists(name):
    with get_engine().connect() as c:
        return c.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar()


def db_error(e):
    return str(getattr(e, "orig", e))


def _default(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.datetime, datetime.date, pd.Timestamp)):
        return o.isoformat()
    if isinstance(o, (datetime.timedelta, pd.Timedelta)):
        return o.total_seconds()
    if isinstance(o, uuid.UUID):
        return str(o)
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def reply(obj, status=200):
    return Response(json.dumps(obj, default=_default), status=status, mimetype="application/json")


def records(df):
    """DataFrame -> list of dicts with NaN/NaT turned into None."""
    return json.loads(json.dumps(df.astype(object).where(pd.notna(df), None).to_dict("records"), default=_default))


def table(df):
    return {"columns": list(df.columns), "rows": records(df)}


def api(fn):
    """Return {"ok": false, "error": ...} instead of a stack trace when a handler fails."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            return reply({"ok": False, "error": db_error(e)}, 500)
    return wrapper


# ---- background jobs: the page starts one, then polls /api/jobs/<id> ----
class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.lines = []
        self.progress = None   # 0..1 or None
        self.done = False
        self.result = None
        self.error = None
        self.t0 = time.time()
        self.t1 = None

    def say(self, line):
        self.lines.append(line)


JOBS = {}
RUNNING = {}   # key -> the job started with that key


def start_job(fn, *args, key=None):
    """Run fn(job, *args) in a thread, as the database role of the request that started it. With a key, only one
    such job runs at a time: while it runs, starting another returns the running job instead (for example a
    second browser tab pressing the same button)."""
    with _lock:
        if key and key in RUNNING and not RUNNING[key].done:
            return RUNNING[key]
        job = Job()
        for k in [k for k, j in JOBS.items() if j.done and time.time() - j.t1 > 3600]:
            del JOBS[k]
        JOBS[job.id] = job
        if key:
            RUNNING[key] = job

    role = DB_ROLE.get()

    def run():
        DB_ROLE.set(role)  # the thread starts with a fresh context
        try:
            job.result = fn(job, *args)
        except Exception as e:  # noqa: BLE001
            job.error = db_error(e)
        finally:
            job.t1 = time.time()
            job.done = True
    threading.Thread(target=run, daemon=True).start()
    return job


@bp.get("/api/jobs/<jid>")
def job_status(jid):
    j = JOBS.get(jid)
    if j is None:
        return reply({"ok": False, "error": "Unknown job."}, 404)
    return reply({"ok": True, "done": j.done, "lines": j.lines, "progress": j.progress, "result": j.result,
                  "error": j.error, "elapsed": round((j.t1 or time.time()) - j.t0)})
