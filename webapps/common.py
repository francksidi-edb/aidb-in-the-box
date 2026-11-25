"""Shared helpers: database engine, JSON responses, and background jobs for slow steps."""
import datetime
import decimal
import functools
import json
import os
import threading
import time
import uuid

import pandas as pd
from flask import Blueprint, Response
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

bp = Blueprint("jobs", __name__)
_engine = None


def get_engine():
    """One pooled connection source. Uses the PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE variables
    that docker/entrypoint.sh sets. AUTOCOMMIT so long aidb calls are not wrapped in one transaction."""
    global _engine
    if _engine is None:
        host = os.getenv("PGHOST") or None
        _engine = create_engine(
            URL.create("postgresql+psycopg2", username=os.getenv("PGUSER", "postgres"),
                       password=os.getenv("PGPASSWORD") or None, host=host,
                       port=int(os.getenv("PGPORT", "5432")) if host else None,
                       database=os.getenv("PGDATABASE", "aidb")),
            pool_pre_ping=True, isolation_level="AUTOCOMMIT")
    return _engine


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
_lock = threading.Lock()


def start_job(fn, *args):
    job = Job()
    with _lock:
        for k in [k for k, j in JOBS.items() if j.done and time.time() - j.t1 > 3600]:
            del JOBS[k]
        JOBS[job.id] = job

    def run():
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
