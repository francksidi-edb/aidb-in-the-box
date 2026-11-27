"""EDB Agent Converse: chat with an AIDB agent. The conversation list lives in the browser."""
import os

from flask import Blueprint, request
from sqlalchemy import text

from .common import api, get_engine, reply

bp = Blueprint("converse", __name__, url_prefix="/api/converse")

# Only agents the app's database user may act for: another role's agents usually depend on that role's private
# model credentials and tables. Override if your aidb version stores agents elsewhere.
AGENT_LIST_SQL = os.getenv("AGENT_LIST_SQL", "SELECT a.name FROM aidb.agents a "
                                             "JOIN pg_roles r ON r.rolname = a.owner_role "  # skips dropped roles
                                             "WHERE pg_has_role(current_user, r.oid, 'MEMBER') ORDER BY a.name")


@bp.get("/agents")
def agents():
    try:
        with get_engine().connect() as c:
            names = [r[0] for r in c.execute(text(AGENT_LIST_SQL))]
        return reply({"ok": True, "agents": names})
    except Exception as e:  # noqa: BLE001
        return reply({"ok": True, "agents": [], "error": str(getattr(e, "orig", e))[:300]})


@bp.post("/ask")
@api
def ask():
    b = request.get_json()
    agent, message, cid, debug = b["agent"], b["message"], b.get("conversation_id"), bool(b.get("debug"))
    if cid:
        sql = "SELECT a.* FROM aidb.agent_converse(:a, :m, conversation_id => :c, debug => :d) AS a"
        params = {"a": agent, "m": message, "c": cid, "d": debug}
    else:
        sql = "SELECT a.* FROM aidb.agent_converse(:a, :m, debug => :d) AS a"
        params = {"a": agent, "m": message, "d": debug}
    with get_engine().begin() as c:
        row = c.execute(text(sql), params).mappings().first()
    if row is None:
        return reply({"ok": True, "answer": "(no response)", "conversation_id": cid, "extra": {}})
    row = dict(row)
    new_cid = row.get("conversation_id") or cid
    answer = row.get("message")
    if isinstance(answer, dict):
        answer = answer.get("content") or str(answer)
    if not answer and row.get("error"):
        answer = f"⚠️ The agent returned an error: {row['error']}"
    extra = {k: v for k, v in row.items() if k not in ("message", "conversation_id")}
    return reply({"ok": True, "answer": str(answer), "conversation_id": str(new_cid) if new_cid else None,
                  "extra": extra if debug else None})
