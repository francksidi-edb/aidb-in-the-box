"""Helpers shared by the governance notebooks.

    import lab
    lab.install()                       # once: guard schema, sample PII, tools, agents, purposes
    lab.sql("SELECT ...")               # run SQL and show a table
    lab.ask("guard_reader", "...")      # the guarded request path (screen, mask, run, log)

Connects to the aidb database on localhost:5432 (PGHOST / PGPORT override). The roles and passwords
are the demo ones created by docker/bootstrap.sql.
"""
import html
import json
import os
import re
from pathlib import Path

import psycopg2

HERE = Path(__file__).resolve().parent
HOST = os.environ.get("PGHOST", "localhost")
PORT = int(os.environ.get("PGPORT", "5432"))
MODEL = os.environ.get("GUARD_MODEL", "desk_llm")          # the language model each notebook registers (its "Register the model" cell)

LOGINS = {
    "demo_user": ("demo_user", "demo"),             # application user
    "demo_governor": ("demo_governor", "demo"),     # governance team
    "admin": ("postgres", "postgres"),              # the DBA
    "persona_analyst": ("persona_analyst", "demo"),
    "persona_admin": ("persona_admin", "demo"),
    "persona_auditor": ("persona_auditor", "demo"),
}


def connect(role="demo_user"):
    user, pw = LOGINS[role]
    c = psycopg2.connect(host=HOST, port=PORT, dbname="aidb", user=user, password=pw, connect_timeout=5)
    c.autocommit = True
    return c


class Result(tuple):
    """(rows, cols); stays quiet when it is the last value of a notebook cell."""
    def _ipython_display_(self):
        pass


class Answer(dict):
    """The outcome of ask(); stays quiet when it is the last value of a notebook cell."""
    def _ipython_display_(self):
        pass


# ───────────────────────────── running and showing SQL ─────────────────────────
def _fmt(v, width):
    s = "" if v is None else (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v))
    return s if len(s) <= width else s[: width - 1] + "…"


def show(rows, cols, width=90):
    """Render rows as an HTML table in Jupyter, or plain text elsewhere."""
    try:
        from IPython.display import HTML, display
        head = "".join(f"<th style='text-align:left'>{html.escape(c)}</th>" for c in cols)
        body = "".join("<tr>" + "".join(f"<td style='text-align:left;white-space:pre-wrap'>{html.escape(_fmt(v, width))}</td>"
                                        for v in r) + "</tr>" for r in rows)
        display(HTML(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"))
    except ImportError:
        print(" | ".join(cols))
        for r in rows:
            print(" | ".join(_fmt(v, width) for v in r))


def sql(query, params=None, role="demo_user", quiet=False, width=90):
    """Run SQL as a lab role. Shows the result set and returns (rows, cols)."""
    notices = []
    c = connect(role)
    try:
        with c.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if cur.description else []
            cols = [d[0] for d in cur.description] if cur.description else []
        notices = [n.strip() for n in c.notices]
    finally:
        c.close()
    if not quiet and cols:
        show(rows, cols, width)
    elif not quiet:
        print("OK")
    return Result((rows, cols))


def try_sql(query, params=None, role="demo_user", width=90):
    """Like sql(), but prints the database error instead of raising (for 'this should fail' cells)."""
    try:
        return sql(query, params, role, width=width)
    except psycopg2.Error as e:
        print("ERROR:", re.sub(r"^ERROR:\s*", "", (e.pgerror or str(e)).strip().splitlines()[0]))
        return None


def scalar(query, params=None, role="demo_user"):
    rows, _ = sql(query, params, role, quiet=True)
    return rows[0][0] if rows else None


def show_function(name, role="admin"):
    """Print the source of a function in the guard schema."""
    src = scalar("SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                 "WHERE n.nspname = 'guard' AND p.proname = %s", (name,), role)
    print(src)


# ───────────────────────────── switches ────────────────────────────────────────
def switch(name=None, enabled=None, value=None):
    """Change a policy switch (as the governance team). Without changes, show all the switches."""
    if enabled is not None or value is not None:
        sql("UPDATE guard.settings SET enabled = coalesce(%s, enabled), value = coalesce(%s, value), updated_at = now() "
            "WHERE name = %s", (enabled, value, name), role="demo_governor", quiet=True)
        return None
    return sql("SELECT name, enabled, value, note FROM guard.settings ORDER BY name", role="demo_governor", width=80)


def settings():
    rows, _ = sql("SELECT name, enabled, value FROM guard.settings", role="demo_governor", quiet=True)
    return {n: (e, v) for n, e, v in rows}


# ───────────────────────────── install ─────────────────────────────────────────
INTENT = [
    "Which tickets are open for this customer?",
    "List the critical tickets and who they are assigned to",
    "How do I fix a full WAL volume or replica lag?",
    "Who is the contact for Aurora Bank?",
    "Look up a runbook for a Postgres problem",
    "Assign this ticket to an engineer",
    "How many tickets does each engineer have?",
]

TOOLS = {
    "g_list_tickets": (
        "List support tickets with customer name, filtered by status, priority and assignee. Use any for a filter the user did not ask for.",
        "SELECT t.id, c.name AS customer, c.tier, t.subject, t.status, t.priority, t.assignee\n"
        "FROM support.tickets t JOIN support.customers c ON c.id = t.customer_id\n"
        "WHERE (${status} = 'any' OR t.status = ${status}) AND (${priority} = 'any' OR t.priority = ${priority})\n"
        "  AND (${assignee} = 'any' OR t.assignee = lower(${assignee})) ORDER BY t.id LIMIT 10",
        [("status", "TEXT", "Exactly one value: open, in_progress, resolved, closed or any"),
         ("priority", "TEXT", "Exactly one value: critical, high, normal, low or any"),
         ("assignee", "TEXT", "Engineer name in lower case, for example maria, or any")], True),
    "g_list_contacts": (
        "List the customer contacts (name, email, phone) of one customer, found by a part of its name.",
        "SELECT k.id, c.name AS customer, k.name AS contact, k.email, k.phone\n"
        "FROM support.contacts k JOIN support.customers c ON c.id = k.customer_id\n"
        "WHERE c.name ILIKE '%' || ${customer} || '%' ORDER BY k.id LIMIT 5",
        [("customer", "TEXT", "Part of the customer name, for example Aurora")], True),
    "g_search_articles": (
        "Search the support runbooks by one keyword. Returns the article id, title and the start of the text.",
        "SELECT a.id, a.title, left(a.body, 400) AS excerpt FROM support.articles a\n"
        "WHERE a.title ILIKE '%' || ${keyword} || '%' OR a.body ILIKE '%' || ${keyword} || '%' ORDER BY a.id LIMIT 3",
        [("keyword", "TEXT", "One keyword, for example WAL or replica")], True),
    "g_assign_ticket": (
        "Assign a ticket to an engineer. Writes to the database.",
        "UPDATE support.tickets SET assignee = lower(${assignee}) WHERE id = ${id} RETURNING id, assignee",
        [("id", "INT", "Ticket id"), ("assignee", "TEXT", "Engineer name, for example priya")], False),
    "g_list_tickets_rls": (
        "List tickets (id, customer, region, status, priority). Use any for a status the user did not ask for.",
        "SELECT id, customer, region, status, priority FROM support.tickets_rls\n"
        "WHERE ${status} = 'any' OR status = ${status} ORDER BY id LIMIT 20",
        [("status", "TEXT", "Exactly one value: open, in_progress, resolved, closed or any")], True),
    "g_ticket_notes": (
        "Read the internal note of one ticket (contains customer contact details).",
        "SELECT id, internal_note FROM support.tickets_rls WHERE id = ${id}",
        [("id", "INT", "Ticket id")], True),
}

DESK_TOOLS = ["g_list_tickets", "g_list_contacts", "g_search_articles", "g_assign_ticket"]
AGENTS = {
    "guard_reader": ("You are the support desk assistant. Always call a tool, never guess. Report what the tool returned. Be brief.",
                     DESK_TOOLS, "guard_readonly"),
    "guard_triager": ("You are the support desk assistant. Always call a tool, never guess. Report what the tool returned. Be brief.",
                      DESK_TOOLS, "guard_triage"),
    "guard_persona": ("You answer questions about support tickets. Always call a tool, never guess. Report what the tool returned. Be brief.",
                      ["g_list_tickets_rls", "g_ticket_notes"], None),
}


def _tool_sql(name):
    desc, stmt, params, ro = TOOLS[name]
    ps = ",\n    ".join(f"aidb.tool_param('{n}', '{t}', '{d}')" for n, t, d in params)
    return (f"SELECT aidb.create_sql_tool(name => '{name}', description => $d${desc}$d$,\n"
            f"  sql_statement => $s${stmt}$s$,\n  params => aidb.tool_params({ps}),\n  read_only => {str(ro).lower()})")


def install(verbose=True):
    """Create everything the notebooks need. Safe to run again. Prints each step (verbose=False to be quiet)."""
    say = print if verbose else (lambda *a, **k: None)
    if not scalar("SELECT EXISTS (SELECT 1 FROM aidb.models WHERE name = %s)", (MODEL,)):
        raise RuntimeError(f"Model '{MODEL}' is not registered. Run the notebook's 'Register the model' cell first.")
    say(f"model {MODEL}: registered")

    say("guard schema, roles, sample data and screening functions (guard_runtime.sql): running...")
    c = connect("admin")
    with c.cursor() as cur:
        cur.execute((HERE / "guard_runtime.sql").read_text())
    c.close()
    say("  done")

    have = {r[0] for r in sql("SELECT name FROM aidb.tools", quiet=True)[0]}
    for name in TOOLS:
        if name in have:
            say(f"tool {name}: already exists")
        else:
            sql(_tool_sql(name), quiet=True)
            say(f"tool {name}: created")

    have_p = {r[0] for r in sql("SELECT name FROM aidb.purpose_registry WHERE deleted_at IS NULL", role="demo_governor", quiet=True)[0]}
    for p, role, desc in (("guard_readonly", "guard_ro", "Guarded desk: read-only"), ("guard_triage", "guard_rw", "Guarded desk: may assign tickets")):
        if p in have_p:
            say(f"purpose {p} (role {role}): already exists")
        else:
            sql("SELECT aidb.create_purpose(%s, %s, %s)", (p, role, desc), role="demo_governor", quiet=True)
            say(f"purpose {p} (role {role}): created")

    have_a = {r[0] for r in sql("SELECT name FROM aidb.agents", quiet=True)[0]}
    for name, (instr, tools, purpose) in AGENTS.items():
        if name in have_a:
            say(f"agent {name}: already exists")
            continue
        rows, _ = sql("SELECT error FROM aidb.create_agent(name => %s, instructions => %s, model => %s, tools => %s, purpose => %s)",
                      (name, instr, MODEL, tools, purpose), quiet=True)
        if rows and rows[0][0]:
            raise RuntimeError(f"create_agent {name}: {rows[0][0]}")
        say(f"agent {name}: created" + (f" (purpose {purpose})" if purpose else ""))

    for agent in ("guard_reader", "guard_triager", "guard_persona"):
        if scalar("SELECT count(*) FROM guard.agent_intents WHERE agent = %s", (agent,), "demo_governor"):
            say(f"intent examples for {agent}: already registered")
        else:
            sql("SELECT guard.register_intent(%s, %s)", (agent, INTENT), role="admin", quiet=True)
            say(f"intent examples for {agent}: registered")
    say("ready")


# ───────────────────────────── the guarded request path ────────────────────────
DEBUG_RE = re.compile(r"^\[agent debug\] (\w+) request=(.*)$", re.S)


def _json(s):
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return s


def _parse_debug(notices):
    events = []
    for n in notices:
        m = DEBUG_RE.match(re.sub(r"^\s*NOTICE:\s*", "", n).strip())
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)
        i = rest.rfind(" response=")
        req, resp = (rest[:i], rest[i + 10:]) if i >= 0 else (rest, "null")
        events.append((kind, _json(req), _json(resp)))
    return events


def _row_refs(contents):
    """Pick the row identifiers out of a tool result so the answer can cite them.

    The debug stream can cut a long result short, so fall back to scanning the text for "id" fields.
    """
    data = _json(contents) if isinstance(contents, str) else contents
    if isinstance(data, dict):
        data = data.get("rows") or data.get("result") or [data]
    refs = []
    if isinstance(data, list):
        for r in data:
            if isinstance(r, dict):
                ref = {k: r[k] for k in ("id", "title") if k in r}
                if ref:
                    refs.append(ref)
    elif isinstance(contents, str):
        seen = []
        for m in re.finditer(r'"id"\s*:\s*(\d+)', contents.replace('\\"', '"')):
            if int(m.group(1)) not in seen:
                seen.append(int(m.group(1)))
        refs = [{"id": i} for i in seen]
    return refs


def lineage(events):
    """Which tools ran, with which arguments, and which rows came back."""
    out, pending = [], None
    for kind, req, resp in events:
        if kind == "tool_call" and isinstance(req, dict):
            pending = {"tool": req.get("tool_name"), "arguments": req.get("arguments")}
        elif kind == "tool_response" and pending:
            if isinstance(resp, dict):
                error, contents = resp.get("error"), resp.get("contents")
            else:                                  # a long result can arrive cut off: not valid JSON any more
                error, contents = None, resp
            pending["error"] = error
            pending["rows"] = _row_refs(contents) if not error else []
            out.append(pending)
            pending = None
    return out


def _model_info(agent):
    row = sql("SELECT a.model, m.provider, m.options::text FROM aidb.agents a LEFT JOIN aidb.models m ON m.name = a.model "
              "WHERE a.name = %s", (agent,), quiet=True)[0]
    if not row:
        return None, None
    name, provider, opts = row[0]
    mv = re.search(r'model\\*"\s*:\s*\\*"([^"\\]+)', opts or "")
    return name, f"{provider}:{mv.group(1)}" if mv else provider


def log_event(role="demo_user", **ev):
    """Write an event through guard.log_event(), which records the logged-in role as the actor."""
    cols = ["agent", "model", "model_version", "prompt", "prompt_sent", "decision", "policy", "intent_score",
            "pii_found", "guardrails_on", "conversation_id", "reply", "sources", "detail"]
    vals = [json.dumps(ev.get(c)) if c in ("sources", "detail") and ev.get(c) is not None else ev.get(c) for c in cols]
    q = f"SELECT guard.log_event({', '.join(f'p_{c} => %s' for c in cols)})"
    return scalar(q, vals, role)


def ask(agent, prompt, role="demo_user", approved=False, conversation_id=None, verbose=True):
    """Send a prompt through the guarded path: screen -> intent check -> mask PII -> agent -> mask reply -> log.

    The app-layer steps run only while the 'app_guardrails' switch is on.
    The database layer (purposes, roles, row-level security) always runs.
    """
    cfg = settings()
    on = cfg["app_guardrails"][0]
    use = lambda name: on and cfg[name][0]          # noqa: E731
    model, model_version = _model_info(agent)
    decision, policy, score, pii = "allowed", None, None, []
    prompt_sent = prompt
    if use("pii_redaction"):                       # mask first: the audit log must never keep the raw PII either
        pii = scalar("SELECT guard.pii_types(%s)", (prompt,), role) or []
        if pii:
            prompt_sent = scalar("SELECT guard.mask_pii(%s)", (prompt,), role)
    base = dict(agent=agent, model=model, model_version=model_version, prompt=prompt_sent, guardrails_on=on)

    if use("prompt_screening"):
        hits, _ = sql("SELECT action, category, note FROM guard.screen_prompt(%s)", (prompt,), role, quiet=True)
        if hits:
            action, category, note = hits[0]
            policy = f"{category}: {note}"
            if action == "block":
                eid = log_event(role, **base, decision="blocked", policy=policy, pii_found=pii or None)
                return _result(verbose, agent, prompt_sent, "blocked", policy, None, None, pii, eid)
            if action == "approve" and use("approvals") and not approved:
                eid = log_event(role, **base, decision="pending_approval", policy=policy, pii_found=pii or None)
                return _result(verbose, agent, prompt_sent, "pending_approval", policy, None, None, pii, eid)
            if action == "flag":
                decision = "flagged"
    if use("intent_monitor"):
        score = scalar("SELECT guard.intent_score(%s, %s)", (agent, prompt_sent), role)
        threshold = float(cfg["intent_monitor"][1] or 0)
        if score is not None and float(score) < threshold and decision == "allowed":
            decision, policy = "flagged", f"intent_drift: similarity {score} < {threshold}"

    c = connect(role)
    try:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = '600s'")
            cur.execute("SELECT message, conversation_id, error FROM aidb.agent_converse("
                        "agent_name => %s, prompt => %s, conversation_id => %s, debug => true)",
                        (agent, prompt_sent, conversation_id))
            message, conv, error = cur.fetchone()
        events = _parse_debug(c.notices)
    finally:
        c.close()

    reply = message if message is not None else (error or "")
    reply_pii = []
    if use("pii_redaction"):
        reply_pii = scalar("SELECT guard.pii_types(%s)", (reply,), role) or []
        if reply_pii:
            reply = scalar("SELECT guard.mask_pii(%s)", (reply,), role)
    pii = sorted(set(pii) | set(reply_pii))
    sources = lineage(events)
    eid = log_event(role, **base, decision=decision, policy=policy, intent_score=score, pii_found=pii or None,
                    conversation_id=conv, reply=reply,
                    sources=sources, detail={"error": error} if error else None)
    return _result(verbose, agent, prompt_sent, decision, policy, reply, score, pii, eid, sources, conv)


def _result(verbose, agent, prompt, decision, policy, reply, score, pii, eid, sources=None, conv=None):
    res = Answer(agent=agent, prompt=prompt, decision=decision, policy=policy, reply=reply, intent_score=score,
               pii_found=pii, event_id=eid, sources=sources or [], conversation_id=conv)
    if verbose:
        print(f"[{decision.upper()}] {agent} <- {prompt!r}")
        if policy:
            print(f"  policy : {policy}")
        if score is not None:
            print(f"  intent : similarity {score}")
        if pii:
            print(f"  pii    : {', '.join(pii)} (masked)")
        if reply is not None:
            print(f"  answer : {reply.strip()[:600]}")
        for s in res["sources"]:
            print(f"  source : {s['tool']}({json.dumps(s['arguments'])}) -> {json.dumps(s['rows'])[:200]}")
    return res


def approve(event_id, approver="demo_governor", note=None):
    """Resolve a pending approval: log the decision, then run the held request."""
    row = sql("SELECT agent, prompt, actor FROM guard.events WHERE id = %s AND decision = 'pending_approval'",
              (event_id,), approver, quiet=True)[0]
    if not row:
        raise ValueError(f"event {event_id} is not a pending approval")
    agent, prompt, requester = row[0]
    log_event(approver, agent=agent, prompt=prompt, decision="approved", policy=f"approved by {approver}",
              guardrails_on=True, detail={"approval_of": event_id, "note": note})
    return ask(agent, prompt, role=requester if requester in LOGINS else "demo_user", approved=True)


def reject(event_id, approver="demo_governor", note=None):
    row = sql("SELECT agent, prompt FROM guard.events WHERE id = %s AND decision = 'pending_approval'",
              (event_id,), approver, quiet=True)[0]
    if not row:
        raise ValueError(f"event {event_id} is not a pending approval")
    log_event(approver, agent=row[0][0], prompt=row[0][1], decision="rejected", policy=f"rejected by {approver}",
              guardrails_on=True, detail={"approval_of": event_id, "note": note})
    print(f"event {event_id} rejected")


def reset_demo_data():
    """Put the sample data back (tickets assigned during demos)."""
    sql("UPDATE support.tickets SET assignee = NULL WHERE id = 1004", role="admin", quiet=True)
