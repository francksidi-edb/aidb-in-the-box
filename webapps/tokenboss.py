"""Token Boss: find AIDB agents that waste tokens, get recommendations from a reviewer agent, apply them with a
modifier agent, and undo them. The SQL lives in tokenboss_sql.py."""
import difflib
import json
import time

import pandas as pd
from flask import Blueprint, request
from sqlalchemy import text

from .common import (api, db_error, db_role, execute, get_engine, query, records, relation_exists, reply,
                     start_job)
from .tokenboss_sql import *  # noqa: F401,F403  (SQL text and constants)
from .tokenboss_sql import (AGENT_FILTER, CREATE_VIEW_SQL, VIEWS_COMMENT_SQL, VIEWS_VERSION, script, DELETED_LABEL, GROUP_TITLES, HIGH_TOKENS_DEFAULT,  # noqa: F401
                            MODEL_NAME, MODIFIER_NAME, MODIFY_TOOL_NAMES, REVIEW_TOOL_NAMES, REVIEWER_NAME,
                            REVIEWS_TABLE_SQL, SETUP_MODEL_SQL, SETUP_OBJECT_STATEMENTS, STEP_BY_KEY, STEPS,
                            USED_MODELS_SQL, VIEW_NAMES, fetch_litellm_prices, modifier_prompt, performance_sql,
                            reviewer_prompt, scorecard_sql, wildcard_to_ilike)

bp = Blueprint("tokenboss", __name__, url_prefix="/api/tokenboss")
db_role(bp, "monitor")  # reads every user's agent activity (docker/roles.sql)


def numbers(df, ints, floats=None):
    """SUM and AVG come back as Decimal; turn them into real numbers."""
    for c in ints:
        df[c] = pd.to_numeric(df[c]).astype("int64")
    for c in floats or []:
        df[c] = pd.to_numeric(df[c]).astype("float64")
    return df


def fmt_time(t):
    return pd.Timestamp(t).strftime("%b %d %H:%M")


VIEWS_STATE_SQL = """SELECT obj_description(to_regclass('monitor.task_stats'), 'pg_class') AS version,
                           to_regclass('monitor.task_stats') IS NOT NULL AS present"""


def create_objects(always=False):
    """Create Token Boss's schema, tables and views (setup step 2), or with always=False only when monitor.task_stats is
    missing or from another Token Boss version (VIEWS_VERSION): older views show only estimated tokens or, from before
    monitor_feed (docker/roles.sql), only Token Boss's own actions. Without task_stats only it is created. One
    transaction, under an advisory lock, so the first requests after an upgrade do not replace the views at once."""
    with get_engine().connect() as c:
        c.execute(text("SELECT pg_advisory_lock(hashtext('tokenboss-views'))"))
        try:
            state = c.execute(text(VIEWS_STATE_SQL)).mappings().first()
            if not always and state["version"] == VIEWS_VERSION:
                return
            statements = (SETUP_OBJECT_STATEMENTS if always or state["present"]
                          else ["CREATE SCHEMA IF NOT EXISTS monitor", CREATE_VIEW_SQL, VIEWS_COMMENT_SQL])
            c.execute(text(script(statements)))  # one query string: one transaction
        finally:
            c.execute(text("SELECT pg_advisory_unlock(hashtext('tokenboss-views'))"))


def ensure_view_if_needed():
    if query(VIEWS_STATE_SQL).iloc[0]["version"] != VIEWS_VERSION:
        create_objects()


def monitor_state():
    """(ready, connection ok, connection error). Ready = the log, agents table and task view exist."""
    try:
        ensure_view_if_needed()
        feed = query("SELECT to_regprocedure('monitor_feed.action_log()') IS NOT NULL AS ok").iloc[0]["ok"]
        return bool(feed) and all(relation_exists(n) for n in ("aidb.agents", "monitor.task_stats")), True, None
    except Exception as e:  # noqa: BLE001
        return False, False, db_error(e)


# ---------------------------------------------------------------- setup helpers
def names_present(sql, names, params=None):
    """Which of `names` the status query finds; empty if the query cannot run (missing table)."""
    try:
        return set(query(sql, {"n": names, **(params or {})}).iloc[:, 0])
    except Exception:  # noqa: BLE001
        return set()


def safe_relation_exists(name):
    try:
        return relation_exists(name)
    except Exception:  # noqa: BLE001
        return False


def present_now():
    return {
        "model": names_present("SELECT name FROM aidb.models WHERE name = ANY(:n)", [MODEL_NAME]),
        "objects": {n for n in VIEW_NAMES if safe_relation_exists(n)},
        # modify tools from an older Token Boss wrote aidb.agents directly: they count as missing until step 4 runs
        "tools": names_present("SELECT name FROM aidb.sql_tool_registry WHERE name = ANY(:n) "
                               "AND (NOT name = ANY(:m) OR query_text LIKE '%aidb.update_agent%')",
                               REVIEW_TOOL_NAMES + MODIFY_TOOL_NAMES, {"m": ["apply_agent_change", "revert_agent"]}),
        "agents": names_present("SELECT name FROM aidb.agents WHERE name = ANY(:n)", [REVIEWER_NAME, MODIFIER_NAME]),
    }


def step_done(step, have):
    return all(n in have[step["group"]] for n in step["names"])


def run_step_sql(step, api_key=None):
    if step["key"] == "model":
        execute(SETUP_MODEL_SQL, {"creds": json.dumps({"api_key": api_key})})
    elif step["key"] == "objects":
        create_objects(always=True)
    else:
        execute(step["sql"])


def error_text(e, secret):
    """Only the database's own message (never the SQL parameters), with the API key masked."""
    message = db_error(e)
    return message.replace(secret, "****") if secret else message


@bp.get("/state")
def state():
    ready, conn_ok, conn_error = monitor_state()
    have = present_now() if conn_ok else {"model": set(), "objects": set(), "tools": set(), "agents": set()}
    steps = []
    for n, s in enumerate(STEPS, start=1):
        steps.append({"n": n, "key": s["key"], "title": s["title"], "done": step_done(s, have),
                      "missing": [STEP_BY_KEY[k]["title"] for k in s["needs"] if not step_done(STEP_BY_KEY[k], have)],
                      "sql": s["sql"].strip()})
    groups = []
    for group, title in GROUP_TITLES:
        names = [n for s in STEPS for n in s["names"] if s["group"] == group]
        groups.append({"title": title, "got": sum(n in have[group] for n in names), "total": len(names),
                       "chips": [{"name": n.replace("monitor.", ""), "ok": n in have[group]} for n in names]})
    done = sum(s["done"] for s in steps)
    return reply({"ok": True, "ready": ready, "conn_ok": conn_ok, "conn_error": conn_error, "steps": steps,
                  "groups": groups, "done": done, "total": len(STEPS), "setup_complete": conn_ok and done == len(STEPS),
                  "reviewer_ok": REVIEWER_NAME in have["agents"], "modifier_ok": MODIFIER_NAME in have["agents"],
                  "high_default": HIGH_TOKENS_DEFAULT})


@bp.post("/setup/step")
def setup_step():
    b = request.get_json()
    step, key = STEP_BY_KEY[b["key"]], (b.get("api_key") or "").strip() or None
    if step["key"] == "model" and not key:
        return reply({"ok": False, "error": "Enter your Claude API key first."})
    try:
        run_step_sql(step, key)
    except Exception as e:  # noqa: BLE001
        return reply({"ok": False, "error": f"Failed: {error_text(e, key)}"})
    return reply({"ok": True, "message": f"Step {STEPS.index(step) + 1} done: {step['title']}."})


def setup_all_job(job, api_key):
    """Run every step, 1 to 6, in order. Each step deletes and recreates what it created before,
    then checks that the objects exist."""
    ok = True
    for n, step in enumerate(STEPS, start=1):
        job.progress = (n - 1) / len(STEPS)
        job.say(f"Step {n} of {len(STEPS)}: {step['title']}…")
        started = time.perf_counter()
        try:
            run_step_sql(step, api_key)
        except Exception as e:  # noqa: BLE001
            job.say(f"❌ Step {n}: {step['title']}: {error_text(e, api_key)}")
            return {"ok": False, "message": f"Stopped at step {n}."}
        found = step_done(step, present_now())
        ok = ok and found
        job.say(f"{'✅' if found else '⚠️'} Step {n}: {step['title']} ({time.perf_counter() - started:.2f}s)"
                + ("" if found else " ran, but the objects were not found afterwards"))
    job.progress = 1.0
    return {"ok": ok, "message": f"Ran all {len(STEPS)} steps."}


@bp.post("/setup/all")
def setup_all():
    key = (request.get_json().get("api_key") or "").strip()
    if not key:
        return reply({"ok": False, "error": "Enter your Claude API key. Run all recreates the model."})
    return reply({"ok": True, "job": start_job(setup_all_job, key).id})


def token_estimates(source, where, params=None):
    """How many of the tasks in `source` matching `where` fall back to estimated token counts (characters / 4)
    because AIDB has no usable provider-reported usage for them, and why. The pages show it as a warning."""
    df = query(f"SELECT token_note, COUNT(*) AS tasks FROM {source} WHERE {where} GROUP BY token_note", params)
    df["tasks"] = pd.to_numeric(df["tasks"]).astype("int64")
    est = df[df["token_note"].notna()]
    # What Token Boss's own session sees: why AIDB may not be storing spans it can read
    telemetry = query("""SELECT current_setting('aidb.otel_client', true) AS otel_client,
                                current_setting('aidb.otel_client_storage_format', true) AS storage_format""")
    return {"tasks": int(df["tasks"].sum()), "estimated": int(est["tasks"].sum()),
            "reasons": [{"note": r.token_note, "tasks": int(r.tasks)} for r in est.itertuples()],
            **telemetry.iloc[0].to_dict()}


# ---------------------------------------------------------------- scorecard
def modified_labels():
    """agent name -> text for the Modified column (changes recorded in the history)."""
    try:
        df = query("SELECT agent_name, COUNT(*) AS changes, MAX(changed_at) AS last_changed "
                   "FROM monitor.agent_config_history GROUP BY agent_name")
    except Exception:  # noqa: BLE001
        return {}
    return {r.agent_name: f"✏️ {int(r.changes)} change{'s' if int(r.changes) != 1 else ''}, "
                          f"last {fmt_time(r.last_changed)}" for r in df.itertuples()}


@bp.get("/scorecard")
@api
def scorecard():
    ready, conn_ok, conn_error = monitor_state()
    if not ready:
        return reply({"ok": True, "ready": False, "conn_error": conn_error})
    a = request.args
    params = {"pat": wildcard_to_ilike(a.get("search")), "show_deleted": a.get("show_deleted") == "1"}
    views_ok = relation_exists("monitor.recent_task_stats") and relation_exists("monitor.agent_last_change")
    since = bool(views_ok and a.get("since", "1") == "1")
    high = int(a.get("high") or HIGH_TOKENS_DEFAULT)
    df = query(scorecard_sql(since), {**params, "high_tokens": high})
    df = numbers(df, ["tasks", "tool_errors", "compactions", "compaction_errors", "tokens_per_task",
                      "estimated_tasks"], ["avg_rounds"])
    df["avg_rounds"] = df["avg_rounds"].fillna(0)  # an agent with no tasks since its change
    df["slowest_s"] = pd.to_timedelta(df.pop("slowest")).dt.total_seconds().round(1).fillna(0)
    df["flags"] = df["flags"].fillna("").str.rstrip("; ")
    df["agent_name"] = df["agent_name"].fillna(DELETED_LABEL)
    labels = modified_labels()
    df["can_review"] = df["agent_name"] != DELETED_LABEL
    df["modified"] = [labels.get(x) for x in df["agent_name"]]
    have = present_now()
    tokens = token_estimates("monitor.recent_task_stats" if since else "monitor.task_stats",
                             AGENT_FILTER.format(col="agent_name"), params)
    return reply({"ok": True, "ready": True, "views_ok": views_ok, "since": since, "rows": records(df),
                  "flagged": int((df["flags"] != "").sum()), "tokens": tokens,
                  "reviewer_ok": REVIEWER_NAME in have["agents"], "modifier_ok": MODIFIER_NAME in have["agents"]})


@bp.post("/review")
def review():
    agent = request.get_json()["agent"]
    if REVIEWER_NAME not in names_present("SELECT name FROM aidb.agents WHERE name = ANY(:n)", [REVIEWER_NAME]):
        return reply({"ok": False, "error": f"The reviewer agent {REVIEWER_NAME} does not exist yet. "
                                            "Run the steps on the Setup tab first."})
    return reply({"ok": True, "job": start_job(lambda job: run_agent(REVIEWER_NAME, reviewer_prompt(agent))).id})


def converse(agent, prompt):
    """Ask an AIDB agent and return (message, error, conversation_id)."""
    with get_engine().begin() as c:
        df = pd.read_sql(text("SELECT * FROM aidb.agent_converse(CAST(:agent AS text), CAST(:q AS text))"),
                         c, params={"agent": agent, "q": prompt})
    row = df.iloc[0]
    return (row["message"] or ""), (row["error"] or None), row["conversation_id"]


def run_agent(agent, prompt):
    try:
        message, error, conv = converse(agent, prompt)
        return {"text": message, "error": error, "conv": str(conv) if conv is not None else None}
    except Exception as e:  # noqa: BLE001
        return {"text": "", "error": db_error(e), "conv": None}


def record_applied_review(agent, original, final_text, reply_text):
    """Save a recommendation once it has been sent to the modifier agent. Never breaks the page."""
    try:
        execute("CREATE SCHEMA IF NOT EXISTS monitor")
        execute(REVIEWS_TABLE_SQL)
        execute("INSERT INTO monitor.agent_reviews "
                "(agent_name, recommendation, edited_text, applied, applied_at, modifier_reply) "
                "VALUES (:a, :orig, CASE WHEN :final <> :orig THEN :final ELSE NULL END, true, now(), :reply)",
                {"a": agent, "orig": original, "final": final_text, "reply": reply_text})
    except Exception:  # noqa: BLE001
        pass


def apply_job(job, agent, final_text, original):
    r = run_agent(MODIFIER_NAME, modifier_prompt(agent, final_text))
    if not r["error"]:
        record_applied_review(agent, original, final_text, r["text"])
    return {"message": r["text"], "error": r["error"]}


@bp.post("/apply")
def apply():
    b = request.get_json()
    if MODIFIER_NAME not in names_present("SELECT name FROM aidb.agents WHERE name = ANY(:n)", [MODIFIER_NAME]):
        return reply({"ok": False, "error": f"The modifier agent {MODIFIER_NAME} does not exist yet."})
    # One modifier run per agent at a time: its edits are computed from the instructions it read
    args = (b["agent"], b["text"], b.get("original") or b["text"])
    job = start_job(apply_job, *args, key=f"tokenboss-apply:{b['agent']}")
    if job.args != args:
        return reply({"ok": False, "error": f"Another recommendation for {b['agent']} is still being applied. "
                                            "Try again when it has finished."})
    return reply({"ok": True, "job": job.id})


# ---------------------------------------------------------------- performance
@bp.get("/performance")
@api
def performance():
    ready, _, conn_error = monitor_state()
    if not ready:
        return reply({"ok": True, "ready": False})
    include_single = request.args.get("include_single") == "1"
    costs_ok = relation_exists("monitor.model_prices")
    try:
        perf = query(performance_sql(costs_ok))
    except Exception as e:  # noqa: BLE001
        msg = ("The history table does not exist yet. Run the steps on the Setup tab."
               if "agent_config_history" in str(e) else f"Could not load the performance data: {db_error(e)}")
        return reply({"ok": True, "ready": True, "info": msg})
    perf = numbers(perf, ["tasks", "estimated_tasks", "avg_input_tokens", "avg_output_tokens", "avg_total_tokens"],
                   ["avg_rounds"])
    perf["pct_change"] = pd.to_numeric(perf["pct_change"])
    perf["avg_cost"] = pd.to_numeric(perf["avg_cost"])
    if not include_single:
        perf = perf[perf.groupby("agent_name")["version"].transform("count") > 1]
    unpriced = sorted({str(m) for m in perf.loc[perf["avg_cost"].isna() & perf["model_id"].notna(),
                                                "model_id"].dropna()}) if costs_ok else []
    tokens = token_estimates("monitor.task_stats", "agent_name = ANY(:agents)",
                             {"agents": sorted(perf["agent_name"].unique())})
    return reply({"ok": True, "ready": True, "costs_ok": costs_ok, "unpriced": unpriced, "rows": records(perf),
                  "agents": int(perf["agent_name"].nunique()), "tokens": tokens})


# ---------------------------------------------------------------- history
def cfg_instructions(cfg):
    return (cfg or {}).get("instructions") or ""


def cfg_tools(cfg):
    tools = (cfg or {}).get("tools")
    return list(tools) if isinstance(tools, list) else []


def current_config(agent):
    df = query("SELECT to_jsonb(a) AS cfg FROM aidb.agents a WHERE a.name = :a", {"a": agent})
    return df.iloc[0]["cfg"] if len(df) else None


def restore_config(agent, target, reason):
    """Put an agent's instructions and tools back to `target`. The current configuration is saved to the
    history first, in the same transaction, so the restore can itself be undone."""
    engine = get_engine().execution_options(isolation_level="READ COMMITTED")
    with engine.begin() as c:
        current = c.execute(text("SELECT to_jsonb(a) FROM aidb.agents a WHERE a.name = :a"), {"a": agent}).scalar()
        if current is None:
            raise ValueError(f"Agent {agent} no longer exists.")
        tools = cfg_tools(target)
        history_id = c.execute(
            text("INSERT INTO monitor.agent_config_history (agent_name, action, old_config, new_config, reason) "
                 "VALUES (:a, 'revert', CAST(:cur AS jsonb), CAST(:new AS jsonb), :r) RETURNING history_id"),
            {"a": agent, "cur": json.dumps(current), "r": reason,
             "new": json.dumps({"instructions": cfg_instructions(target), "tools": tools})}).scalar()
        # Unchanged tools are left out, so a tool deleted since then does not block restoring the instructions
        error = c.execute(text("SELECT error FROM aidb.update_agent(:a, instructions => :ins, "
                               "tools => CAST(:tools AS text[]))"),
                          {"a": agent, "ins": cfg_instructions(target),
                           "tools": None if tools == cfg_tools(current) else tools}).scalar()
        if error:
            raise ValueError(error)  # rolls the history row back too
    return history_id


def before_after(tasks, start, middle, end):
    """(tasks, input tokens per task, tool errors, tasks with estimated tokens) for the tasks before and after a
    change."""
    def stats(lo, hi):
        part = tasks
        if lo is not None:
            part = part[part["started_at"] >= lo]
        if hi is not None:
            part = part[part["started_at"] < hi]
        n = len(part)
        return (n, (int(part["input_tokens"].mean()) if n else None), (int(part["tool_errors"].sum()) if n else None),
                int((~part["tokens_reported"].astype(bool)).sum()))
    return stats(start, middle), stats(middle, end)


def arrow(before, after, fmt="{:,}"):
    def show(v):
        return "–" if v is None else fmt.format(v)
    return f"{show(before)} → {show(after)}"


def change_diff(before_cfg, after_cfg):
    old_text, new_text = cfg_instructions(before_cfg), cfg_instructions(after_cfg)
    diff = None
    if old_text != new_text:
        diff = "\n".join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                              "before", "after", lineterm="", n=2)) or "(whitespace only)"
    old_tools, new_tools = cfg_tools(before_cfg), cfg_tools(after_cfg)
    return {"diff": diff, "added": [t for t in new_tools if t not in old_tools],
            "removed": [t for t in old_tools if t not in new_tools]}


def load_history(agent):
    hist = query("SELECT history_id, changed_at, action, old_config, new_config, reason "
                 "FROM monitor.agent_config_history WHERE agent_name = :a ORDER BY changed_at, history_id",
                 {"a": agent})
    times = [pd.Timestamp(t).tz_convert("UTC") for t in hist["changed_at"]]
    return hist, times


@bp.get("/history")
@api
def history():
    ready, _, _ = monitor_state()
    try:
        counts = query("SELECT agent_name, COUNT(*) AS n FROM monitor.agent_config_history "
                       "GROUP BY agent_name").set_index("agent_name")["n"].astype(int).to_dict()
        available = True
    except Exception:  # noqa: BLE001
        counts, available = {}, False
    try:
        live = query("SELECT name FROM aidb.agents WHERE name NOT IN (:r, :m) ORDER BY name",
                     {"r": REVIEWER_NAME, "m": MODIFIER_NAME})["name"].tolist()
    except Exception:  # noqa: BLE001
        live = []
    choices = sorted(set(live) | set(counts))
    if not ready or not available:
        return reply({"ok": True, "available": False})
    if not choices:
        return reply({"ok": True, "available": True, "choices": []})
    labels = [{"name": a, "label": (f"{a} ({counts[a]} change{'s' if counts[a] != 1 else ''})" if a in counts
                                    else a if a in live else f"{a} (deleted)")} for a in choices]
    agent = request.args.get("agent")
    if agent not in choices:
        agent = choices[0]
    hist, times = load_history(agent)
    current = current_config(agent)
    out = {"ok": True, "available": True, "choices": labels, "agent": agent, "exists": current is not None,
           "current": None if current is None else {"tools": cfg_tools(current),
                                                    "instructions": cfg_instructions(current)},
           "changes": []}
    if not hist.empty:
        tasks = query("SELECT started_at, input_tokens, tool_errors, tokens_reported FROM monitor.task_stats "
                      "WHERE agent_name = :a", {"a": agent})
        tasks["started_at"] = pd.to_datetime(tasks["started_at"], utc=True)
        tasks["input_tokens"] = pd.to_numeric(tasks["input_tokens"])
        out["tokens"] = token_estimates("monitor.task_stats", "agent_name = :a", {"a": agent})
        tasks["tool_errors"] = pd.to_numeric(tasks["tool_errors"])
        for i in range(len(hist) - 1, -1, -1):  # newest first
            start = times[i - 1] if i > 0 else None
            end = times[i + 1] if i + 1 < len(times) else None
            (t_b, tok_b, err_b, est_b), (t_a, tok_a, err_a, est_a) = before_after(tasks, start, times[i], end)
            row = hist.iloc[i]
            out["changes"].append({"id": int(row["history_id"]), "when": fmt_time(row["changed_at"]),
                                   "action": "Undo" if row["action"] == "revert" else "Change",
                                   "reason": row["reason"] or "", "tasks": arrow(t_b, t_a),
                                   "tokens": arrow(tok_b, tok_a), "estimated_tasks": est_b + est_a,
                                   "errors": arrow(err_b, err_a)})
    try:
        r = query("SELECT reviewed_at, recommendation, edited_text, applied_at FROM monitor.agent_reviews "
                  "WHERE agent_name = :a AND applied ORDER BY reviewed_at DESC LIMIT 10", {"a": agent})
        out["reviews"] = [{"when": fmt_time(x.applied_at), "text": x.edited_text or x.recommendation,
                           "edited": bool(x.edited_text)} for x in r.itertuples()]
    except Exception:  # noqa: BLE001
        out["reviews"] = []
    return reply(out)


@bp.get("/history/change")
@api
def history_change():
    agent, hid = request.args["agent"], int(request.args["id"])
    hist, times = load_history(agent)
    current = current_config(agent)
    match = hist[hist["history_id"] == hid]
    if not len(match):
        return reply({"ok": False, "error": "Change not found."})
    i = int(match.index[0])
    row = hist.iloc[i]
    before_cfg = row["old_config"]
    after_cfg = hist.iloc[i + 1]["old_config"] if i + 1 < len(hist) else current
    out = {"ok": True, "id": hid, "when": fmt_time(row["changed_at"]), "reason": row["reason"] or "",
           "change": change_diff(before_cfg, after_cfg) if after_cfg is not None else None}
    try:
        reviews = query("SELECT recommendation, edited_text, applied_at FROM monitor.agent_reviews "
                        "WHERE agent_name = :a AND applied ORDER BY applied_at", {"a": agent})
    except Exception:  # noqa: BLE001
        reviews = pd.DataFrame()
    out["recommendation"] = None
    if len(reviews):
        applied_at = pd.to_datetime(reviews["applied_at"], utc=True)
        near = reviews[(applied_at >= times[i] - pd.Timedelta(minutes=1))
                       & (applied_at <= times[i] + pd.Timedelta(minutes=15))]
        if len(near):
            out["recommendation"] = {"text": near.iloc[0]["edited_text"] or near.iloc[0]["recommendation"],
                                     "edited": bool(near.iloc[0]["edited_text"])}
    if current is None:
        out["undo"] = "gone"
    elif (after_cfg is not None and cfg_instructions(current) == cfg_instructions(before_cfg)
          and cfg_tools(current) == cfg_tools(before_cfg)):
        out["undo"] = "already"
    else:
        out["undo"] = "can"
    return reply(out)


@bp.post("/undo")
def undo():
    b = request.get_json()
    agent, hid = b["agent"], int(b["history_id"])
    try:
        hist, _ = load_history(agent)
        row = hist[hist["history_id"] == hid].iloc[0]
        new_id = restore_config(agent, row["old_config"], f"Undo of change {hid}")
        return reply({"ok": True, "message": f"Restored the configuration from before change {hid}. "
                                             f"The configuration it replaced is saved as change {new_id}."})
    except Exception as e:  # noqa: BLE001
        return reply({"ok": False, "error": f"Could not restore: {db_error(e)}"})


# ---------------------------------------------------------------- model prices
def save_prices(rows):
    """Replace the whole price table with `rows` (model_id, input, output, source), in one transaction."""
    engine = get_engine().execution_options(isolation_level="READ COMMITTED")
    with engine.begin() as c:
        c.execute(text("DELETE FROM monitor.model_prices"))
        for model_id, price_in, price_out, source in rows:
            c.execute(text("INSERT INTO monitor.model_prices (model_id, input_per_mtok, output_per_mtok, source) "
                           "VALUES (:m, :i, :o, :s)"),
                      {"m": model_id, "i": price_in, "o": price_out, "s": source})


def price_state():
    prices = query("SELECT model_id, input_per_mtok, output_per_mtok, source FROM monitor.model_prices ORDER BY model_id")
    prices = numbers(prices, [], ["input_per_mtok", "output_per_mtok"])
    used = list(query(USED_MODELS_SQL)["model_id"])
    return prices, used, [m for m in used if m not in set(prices["model_id"])]


@bp.get("/prices")
@api
def prices():
    if not safe_relation_exists("monitor.model_prices"):
        return reply({"ok": True, "available": False})
    p, used, unpriced = price_state()
    return reply({"ok": True, "available": True, "rows": records(p), "used": used, "unpriced": unpriced})


@bp.post("/prices/load")
def prices_load():
    replace = bool(request.get_json().get("replace"))
    try:
        p, used, unpriced = price_state()
        wanted = used if replace else unpriced
        if not wanted:
            return reply({"ok": True, "kind": "info", "message": "Every model your agents use already has a price."})
        try:
            found, missing = fetch_litellm_prices(wanted)
        except Exception as e:  # noqa: BLE001
            return reply({"ok": True, "kind": "error",
                          "message": f"Could not load the LiteLLM price list ({e}). The app needs internet access to "
                                     "GitHub for this. You can still type prices into the table."})
        merged = {r.model_id: (r.input_per_mtok, r.output_per_mtok, r.source) for r in p.itertuples(index=False)}
        for model_id, (price_in, price_out) in found.items():
            merged[model_id] = (price_in, price_out, "LiteLLM price list")
        save_prices([(m, *v) for m, v in sorted(merged.items())])
        note = f"Loaded prices for {len(found)} model(s)."
        if missing:
            note += " Not in the LiteLLM list, so add them in the table: " + ", ".join(missing) + "."
        return reply({"ok": True, "kind": "warning" if missing else "success", "message": note})
    except Exception as e:  # noqa: BLE001
        return reply({"ok": False, "error": db_error(e)})


@bp.post("/prices/save")
def prices_save():
    rows, seen = [], set()
    for r in request.get_json().get("rows", []):
        model_id = str(r.get("model_id") or "").strip()
        if not model_id:
            continue
        try:
            pin, pout = float(r.get("input_per_mtok")), float(r.get("output_per_mtok"))
        except (TypeError, ValueError):
            return reply({"ok": False, "error": "Every model needs both an input and an output price."})
        if pin < 0 or pout < 0:
            return reply({"ok": False, "error": "Prices cannot be negative."})
        if model_id in seen:
            return reply({"ok": False, "error": "Each model can appear only once."})
        seen.add(model_id)
        rows.append((model_id, pin, pout, (r.get("source") or "").strip() or "entered by hand"))
    try:
        save_prices(rows)
        return reply({"ok": True, "message": f"Saved {len(rows)} price(s)."})
    except Exception as e:  # noqa: BLE001
        return reply({"ok": False, "error": f"Could not save the prices: {db_error(e)}"})
