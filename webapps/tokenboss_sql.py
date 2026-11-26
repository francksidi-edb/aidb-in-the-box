"""SQL and constants for Token Boss."""
import json
import os


# Estimated input tokens per task above which an agent is flagged (adjustable in the Agents tab)
HIGH_TOKENS_DEFAULT = int(os.getenv("MONITOR_HIGH_TOKENS", "5000"))

DELETED_LABEL = "(deleted)"


CREATE_VIEW_SQL = """
CREATE OR REPLACE VIEW monitor.task_stats AS
SELECT l.task_id,
       l.conversation_id,
       a.name AS agent_name,
       MIN(l.created_at) AS started_at,
       MAX(COALESCE(l.completed_at, l.created_at)) - MIN(l.created_at) AS duration,
       SUM(CASE WHEN l.action_type = 'model_request' THEN 1 ELSE 0 END) AS rounds,
       SUM(CASE WHEN l.action_type = 'tool_call' THEN 1 ELSE 0 END) AS tool_calls,
       SUM(CASE WHEN l.action_type = 'tool_response' AND l.status = 'ERROR' THEN 1 ELSE 0 END) AS tool_errors,
       SUM(CASE WHEN l.action_type = 'compaction' THEN 1 ELSE 0 END) AS compactions,
       SUM(CASE WHEN l.action_type = 'compaction' AND l.status = 'ERROR' THEN 1 ELSE 0 END) AS compaction_errors,
       SUM(CASE WHEN l.action_type IN ('model_request', 'compaction')
                THEN LENGTH(CAST(l.request_payload AS TEXT)) ELSE 0 END) / 4 AS est_input_tokens,
       SUM(CASE WHEN l.action_type IN ('model_response', 'compaction')
                THEN LENGTH(CAST(l.response_payload AS TEXT))
                WHEN l.action_type = 'tool_call'
                THEN LENGTH(CAST(l.request_payload AS TEXT)) ELSE 0 END) / 4 AS est_output_tokens
FROM aidb_internal.action_log l
LEFT JOIN aidb.agents a ON a.id = l.agent_id
GROUP BY l.task_id, l.conversation_id, l.agent_id, a.name
"""

AGENT_FILTER = ("(({col} IS NOT NULL AND (:pat IS NULL OR {col} ILIKE :pat)) "
                "OR (:show_deleted AND {col} IS NULL))")



def wildcard_to_ilike(pattern: str | None) -> str | None:
    """* (or %) matches any text, ? matches one character. No wildcard means 'contains'."""
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    has_wildcard = any(ch in pattern for ch in "*?%")
    out = []
    for ch in pattern:
        if ch in "*%":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in "_\\":
            out.append("\\" + ch)  # a literal underscore or backslash
        else:
            out.append(ch)
    s = "".join(out)
    return s if has_wildcard else f"%{s}%"

SCORECARD_SQL = """
WITH agent_scorecard AS (
    SELECT agent_name,
           COUNT(*)                AS tasks,
           ROUND(AVG(rounds), 1)   AS avg_rounds,
           SUM(tool_errors)        AS tool_errors,
           SUM(compactions)        AS compactions,
           SUM(compaction_errors)  AS compaction_errors,
           CAST(SUM(est_input_tokens) / COUNT(*) AS INTEGER) AS est_tokens_per_task,
           MAX(duration)           AS slowest,
           TRIM(
             CASE WHEN SUM(tool_errors) > 0 THEN 'tool errors; ' ELSE '' END ||
             CASE WHEN SUM(compactions) > 0 THEN 'compaction; ' ELSE '' END ||
             CASE WHEN SUM(compaction_errors) > 0 THEN 'compaction failures; ' ELSE '' END ||
             CASE WHEN SUM(est_input_tokens) / COUNT(*) > :high_tokens THEN 'high tokens per task; ' ELSE '' END
           ) AS flags
    FROM {source}
    WHERE {agent_filter}
    GROUP BY agent_name
)
SELECT * FROM (
    SELECT * FROM agent_scorecard
    {no_tasks}
) s
ORDER BY LENGTH(flags) DESC, est_tokens_per_task DESC
"""

# Agents that were changed and have had no tasks since still get a row (with zero tasks).
NO_TASKS_ROWS = """UNION ALL
    SELECT c.agent_name, 0::bigint, NULL::numeric, 0::bigint, 0::bigint, 0::bigint, 0, NULL::interval, ''::text
    FROM monitor.agent_last_change c
    JOIN aidb.agents a ON a.name = c.agent_name
    WHERE {agent_filter_c}
      AND NOT EXISTS (SELECT NULL FROM agent_scorecard s WHERE s.agent_name = c.agent_name)""".format(
    agent_filter_c=AGENT_FILTER.format(col="c.agent_name"))


def scorecard_sql(since_change: bool) -> str:
    return SCORECARD_SQL.format(
        source="monitor.recent_task_stats" if since_change else "monitor.task_stats",
        no_tasks=NO_TASKS_ROWS if since_change else "",
        agent_filter=AGENT_FILTER.format(col="agent_name"))

PERFORMANCE_SQL = """
WITH numbered AS (
    SELECT agent_name, changed_at, reason, old_config,
           ROW_NUMBER() OVER (PARTITION BY agent_name ORDER BY changed_at, history_id) AS n
    FROM monitor.agent_config_history
),
versions AS (
    -- Every change or undo starts a new version of that agent (version 1 is the original configuration)
    SELECT agent_name, changed_at, reason, n + 1 AS version FROM numbered
),
tasks AS (
    SELECT t.agent_name, t.started_at, t.rounds, t.est_input_tokens, t.est_output_tokens,
           COALESCE((SELECT MAX(v.version) FROM versions v
                      WHERE v.agent_name = t.agent_name AND v.changed_at <= t.started_at), 1) AS version
    FROM monitor.task_stats t
    WHERE t.agent_name IS NOT NULL
),
per_version AS (
    SELECT agent_name, version,
           COUNT(*)                                         AS tasks,
           ROUND(AVG(est_input_tokens))                     AS avg_input_tokens,
           ROUND(AVG(est_output_tokens))                    AS avg_output_tokens,
           ROUND(AVG(est_input_tokens + est_output_tokens)) AS avg_total_tokens,
           ROUND(AVG(rounds), 1)                            AS avg_rounds
    FROM tasks
    GROUP BY agent_name, version
)
SELECT p.agent_name,
       'v' || p.version AS version,
       p.tasks, p.avg_input_tokens, p.avg_output_tokens, p.avg_total_tokens, p.avg_rounds,
       ROUND(100.0 * (p.avg_total_tokens - LAG(p.avg_total_tokens) OVER w)
             / NULLIF(LAG(p.avg_total_tokens) OVER w, 0), 1) AS pct_change,
       v.reason AS change_reason,
       {cost_cols}
FROM per_version p
LEFT JOIN versions v ON v.agent_name = p.agent_name AND v.version = p.version
{cost_joins}
WINDOW w AS (PARTITION BY p.agent_name ORDER BY p.version)
ORDER BY p.agent_name, p.version
"""
# Cost: the model an agent used in each version comes from its saved configuration (the current one for the
# latest version), the real model id from aidb.models, and the price from monitor.model_prices.
COST_COLS = """COALESCE(cfg.old_config->>'model', a.model) AS model_name,
       mi.model_id,
       ROUND((p.avg_input_tokens * mp.input_per_mtok + p.avg_output_tokens * mp.output_per_mtok) / 1000000.0, 5) AS avg_cost"""
COST_JOINS = """LEFT JOIN numbered cfg ON cfg.agent_name = p.agent_name AND cfg.n = p.version
LEFT JOIN aidb.agents a ON a.name = p.agent_name
LEFT JOIN monitor.model_ids mi ON mi.model_name = COALESCE(cfg.old_config->>'model', a.model)
LEFT JOIN monitor.model_prices mp ON mp.model_id = mi.model_id"""
NO_COST_COLS = "NULL::text AS model_name, NULL::text AS model_id, NULL::numeric AS avg_cost"


def performance_sql(with_cost: bool) -> str:
    return PERFORMANCE_SQL.format(cost_cols=COST_COLS if with_cost else NO_COST_COLS,
                                  cost_joins=COST_JOINS if with_cost else "")

MODEL_NAME = "claude_model_agent_monitor"
REVIEWER_NAME = "agent_reviewer"
VIEW_NAMES = ["monitor.agent_config_history", "monitor.agent_reviews", "monitor.model_prices",
              "monitor.model_ids", "monitor.task_stats",
              "monitor.agent_last_change", "monitor.recent_task_stats", "monitor.agent_scorecard",
              "monitor.error_summary", "monitor.repeated_calls", "monitor.tool_usage"]
REVIEW_TOOL_NAMES = ["review_scorecard", "review_errors", "review_retries",
                     "review_tool_usage", "review_agent_config", "review_tool_definition"]
MODIFY_TOOL_NAMES = ["save_agent_snapshot", "apply_agent_change", "revert_agent"]
MODIFIER_NAME = "agent_modifier"

SETUP_MODEL_SQL = """
DO $do$
DECLARE
    v_name text := 'claude_model_agent_monitor';
BEGIN
    IF EXISTS (SELECT NULL FROM aidb.models WHERE name = v_name) THEN
        PERFORM aidb.delete_model(v_name);
    END IF;

    PERFORM aidb.create_model(
        name        => v_name,
        provider    => 'anthropic_messages',
        config      => aidb.anthropic_messages_config(model => 'claude-sonnet-5'),
        credentials => CAST(:creds AS JSONB),
        replace_credentials => true
    );
END
$do$;
"""

REVIEWS_TABLE_SQL = """CREATE TABLE IF NOT EXISTS monitor.agent_reviews (
    review_id      BIGSERIAL PRIMARY KEY,
    agent_name     TEXT NOT NULL,
    reviewed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    recommendation TEXT,
    edited_text    TEXT,
    applied        BOOLEAN NOT NULL DEFAULT false,
    applied_at     TIMESTAMPTZ,
    modifier_reply TEXT
)"""

# One statement per entry (the SQL text contains semicolons inside strings, so it is not split on ";")
SETUP_OBJECT_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS monitor",
    """CREATE TABLE IF NOT EXISTS monitor.agent_config_history (
    history_id   BIGSERIAL PRIMARY KEY,
    agent_name   TEXT NOT NULL,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    action       TEXT NOT NULL,
    old_config   JSONB NOT NULL,
    new_config   JSONB,
    reason       TEXT
)""",
    REVIEWS_TABLE_SQL,
    """CREATE TABLE IF NOT EXISTS monitor.model_prices (
    model_id         TEXT PRIMARY KEY,
    input_per_mtok   NUMERIC NOT NULL,
    output_per_mtok  NUMERIC NOT NULL,
    source           TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """INSERT INTO monitor.model_prices (model_id, input_per_mtok, output_per_mtok, source)
VALUES ('claude-sonnet-5', 2, 10, 'Anthropic pricing page, Nov 2026')
ON CONFLICT (model_id) DO NOTHING""",
    """CREATE OR REPLACE VIEW monitor.model_ids AS
SELECT m.name AS model_name, m.provider,
       CASE WHEN o ~ '^config=[{].*[}]$' THEN CAST(SUBSTRING(o FROM 8) AS JSONB)->>'model' END AS model_id
FROM aidb.models m
CROSS JOIN LATERAL UNNEST(m.options) AS o
WHERE o LIKE 'config=%'""",
    CREATE_VIEW_SQL.strip(),
    """CREATE OR REPLACE VIEW monitor.agent_last_change AS
SELECT agent_name, MAX(changed_at) AS changed_at
FROM monitor.agent_config_history
GROUP BY agent_name""",
    """CREATE OR REPLACE VIEW monitor.recent_task_stats AS
SELECT t.*
FROM monitor.task_stats t
LEFT JOIN monitor.agent_last_change c ON c.agent_name = t.agent_name
WHERE c.changed_at IS NULL OR t.started_at >= c.changed_at""",
    """CREATE OR REPLACE VIEW monitor.agent_scorecard AS
SELECT agent_name,
       COUNT(*)                AS tasks,
       ROUND(AVG(rounds), 1)   AS avg_rounds,
       SUM(tool_errors)        AS tool_errors,
       SUM(compactions)        AS compactions,
       SUM(compaction_errors)  AS compaction_errors,
       CAST(SUM(est_input_tokens) / COUNT(*) AS INTEGER) AS est_tokens_per_task,
       MAX(duration)           AS slowest,
       TRIM(
         CASE WHEN SUM(tool_errors) > 0 THEN 'tool errors; ' ELSE '' END ||
         CASE WHEN SUM(compactions) > 0 THEN 'compaction; ' ELSE '' END ||
         CASE WHEN SUM(compaction_errors) > 0 THEN 'compaction failures; ' ELSE '' END ||
         CASE WHEN SUM(est_input_tokens) / COUNT(*) > 5000 THEN 'high tokens per task; ' ELSE '' END
       ) AS flags
FROM monitor.recent_task_stats
WHERE agent_name IS NOT NULL
GROUP BY agent_name""",
    """CREATE OR REPLACE VIEW monitor.error_summary AS
SELECT agent_name, action_type, error_type, COUNT(*) AS n, MAX(created_at) AS last_seen
FROM (
  SELECT agent_name, action_type, created_at,
         CASE
           WHEN POSITION(' [decision_id' IN message) > 0
             THEN SUBSTRING(message FROM 1 FOR POSITION(' [decision_id' IN message) - 1)
           WHEN POSITION(': category=' IN message) > 0
             THEN SUBSTRING(message FROM 1 FOR POSITION(': category=' IN message) - 1)
           ELSE message
         END AS error_type
  FROM (
    SELECT a.name AS agent_name, l.action_type, l.created_at,
           l.response_payload->>'error' AS message
    FROM aidb_internal.action_log l
    JOIN aidb.agents a ON a.id = l.agent_id
    LEFT JOIN monitor.agent_last_change lc ON lc.agent_name = a.name
    WHERE l.status = 'ERROR' AND (lc.changed_at IS NULL OR l.created_at >= lc.changed_at)
  ) m
) e
GROUP BY agent_name, action_type, error_type""",
    """CREATE OR REPLACE VIEW monitor.repeated_calls AS
SELECT t.agent_name, l.task_id,
       l.request_payload->>'tool_name' AS tool_name,
       CAST(l.request_payload->'arguments' AS TEXT) AS arguments,
       COUNT(*) AS times
FROM aidb_internal.action_log l
JOIN monitor.recent_task_stats t ON t.task_id = l.task_id
WHERE l.action_type = 'tool_call' AND t.agent_name IS NOT NULL
GROUP BY t.agent_name, l.task_id, l.request_payload->>'tool_name', CAST(l.request_payload->'arguments' AS TEXT)
HAVING COUNT(*) > 1""",
    """CREATE OR REPLACE VIEW monitor.tool_usage AS
SELECT a.name AS agent_name,
       c.request_payload->>'tool_name' AS tool_name,
       COUNT(*) AS calls,
       SUM(CASE WHEN r.status = 'ERROR' THEN 1 ELSE 0 END) AS errors,
       CAST(AVG(LENGTH(CAST(r.response_payload AS TEXT))) / 4 AS INTEGER) AS avg_response_tokens,
       CAST(MAX(LENGTH(CAST(r.response_payload AS TEXT))) / 4 AS INTEGER) AS max_response_tokens
FROM aidb_internal.action_log c
JOIN aidb.agents a ON a.id = c.agent_id
LEFT JOIN monitor.agent_last_change lc ON lc.agent_name = a.name
LEFT JOIN aidb_internal.action_log r
       ON r.task_id = c.task_id
      AND r.action_type = 'tool_response'
      AND r.response_payload->>'call_id' = c.request_payload->>'call_id'
WHERE c.action_type = 'tool_call' AND (lc.changed_at IS NULL OR c.created_at >= lc.changed_at)
GROUP BY a.name, c.request_payload->>'tool_name'""",
]

SETUP_TOOLS_SQL = """
DO $do$
DECLARE
    v_name text := 'review_scorecard';
BEGIN
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Health summary per agent, counting only tasks since the agent was last changed: tasks, average rounds, tool errors, compactions, compaction failures, estimated input tokens per task, slowest task, and a flags column that names the problems. Start here to find which agents need attention.',
      sql_statement => 'SELECT * FROM monitor.agent_scorecard WHERE agent_name ILIKE ${agent_pattern} ORDER BY LENGTH(flags) DESC, est_tokens_per_task DESC LIMIT 25',
      params => aidb.tool_params(
        aidb.tool_param('agent_pattern', 'TEXT', 'An agent name, or a pattern with % as a wildcard. Use % for all agents.')
      )
    );

    v_name := 'review_errors';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Errors grouped by agent, action type and error message, with a count (n) and when each was last seen.',
      sql_statement => 'SELECT * FROM monitor.error_summary WHERE agent_name ILIKE ${agent_pattern} ORDER BY n DESC LIMIT 25',
      params => aidb.tool_params(
        aidb.tool_param('agent_pattern', 'TEXT', 'An agent name, or a pattern with % as a wildcard. Use % for all agents.')
      )
    );

    v_name := 'review_retries';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Tool calls that were repeated with identical arguments inside one task. This usually means the first call failed or returned nothing useful.',
      sql_statement => 'SELECT agent_name, task_id, tool_name, SUBSTRING(arguments FROM 1 FOR 300) AS arguments, times FROM monitor.repeated_calls WHERE agent_name ILIKE ${agent_pattern} ORDER BY times DESC LIMIT 25',
      params => aidb.tool_params(
        aidb.tool_param('agent_pattern', 'TEXT', 'An agent name, or a pattern with % as a wildcard. Use % for all agents.')
      )
    );

    v_name := 'review_tool_usage';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'For each agent and tool: number of calls, number of errors, and the average and maximum response size in estimated tokens. Large responses or a high error rate point to a tool to fix or remove.',
      sql_statement => 'SELECT * FROM monitor.tool_usage WHERE agent_name ILIKE ${agent_pattern} ORDER BY avg_response_tokens DESC NULLS LAST LIMIT 25',
      params => aidb.tool_params(
        aidb.tool_param('agent_pattern', 'TEXT', 'An agent name, or a pattern with % as a wildcard. Use % for all agents.')
      )
    );

    v_name := 'review_agent_config';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'The full configuration of one agent from aidb.agents, including its instructions, tools and budgets.',
      sql_statement => 'SELECT * FROM aidb.agents WHERE name = ${agent_name}',
      params => aidb.tool_params(
        aidb.tool_param('agent_name', 'TEXT', 'The exact name of the agent.')
      )
    );

    v_name := 'review_tool_definition';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'The definition of one tool from aidb.tools, including its description and parameters.',
      sql_statement => 'SELECT * FROM aidb.tools WHERE name = ${tool_name}',
      params => aidb.tool_params(
        aidb.tool_param('tool_name', 'TEXT', 'The exact name of the tool.')
      )
    );
END
$do$;
"""

def agent_sql(name: str, instructions: str, tools: list) -> str:
    """SQL that deletes the agent if it exists, creates it, then checks the stored tools."""
    tool_list = ", ".join(f"'{t}'" for t in tools)
    return f"""
DO $do$
DECLARE
    v_name text := '{name}';
    v_model text := '{MODEL_NAME}';
    v_tools text[] := ARRAY[{tool_list}];
    v_instructions text := $inst$
{instructions}$inst$;
    v_err text;
    v_have text[];
BEGIN
    IF EXISTS (SELECT NULL FROM aidb.agents WHERE name = v_name) THEN
        SELECT error INTO v_err FROM aidb.delete_agent(v_name, force => true);
        IF v_err IS NOT NULL THEN
            RAISE EXCEPTION 'delete_agent returned an error: %', v_err;
        END IF;
        IF EXISTS (SELECT NULL FROM aidb.agents WHERE name = v_name) THEN
            RAISE EXCEPTION 'delete_agent returned no error, but agent % still exists', v_name;
        END IF;
    END IF;

    SELECT error INTO v_err FROM aidb.create_agent(
        name => v_name, model => v_model, instructions => v_instructions, tools => v_tools);
    IF v_err IS NOT NULL THEN
        RAISE EXCEPTION 'create_agent failed: %', v_err;
    END IF;

    SELECT tools INTO v_have FROM aidb.agents WHERE name = v_name;
    IF NOT (v_tools <@ COALESCE(v_have, ARRAY[]::text[])) THEN
        RAISE EXCEPTION 'agent % is missing tools: %', v_name,
            (SELECT string_agg(t, ', ') FROM unnest(v_tools) t WHERE NOT (t = ANY (COALESCE(v_have, ARRAY[]::text[]))));
    END IF;
END
$do$;
"""


SETUP_AGENT_SQL = agent_sql(
    'agent_reviewer',
    "You review AIDB agents and recommend changes. You only read data and recommend; you never change anything.\n\nThe modifier agent can make only two kinds of change: edit an agent\'s instructions (add, replace or delete a sentence) and change its list of tools (add or remove a tool). Every recommendation must be one of these. Never recommend changing budgets, max_iterations, timeouts, the model, tool definitions, tool descriptions, permissions or grants, or anything else; they cannot be applied.\n\nAll the data you read covers only the tasks since each agent\'s last change, so it shows how the agent behaves with its current configuration. An agent that has no rows has had no tasks since it was last changed: say there is nothing to review yet.\n\nSteps:\n1. Call review_scorecard with agent_pattern \'%\'. Only investigate agents with something in the flags column.\n2. For each flagged agent, call review_errors, review_retries and review_tool_usage for that agent, and review_agent_config for its configuration.\n3. For any tool that errors, returns large responses, or is repeated, call review_tool_definition.\n4. Do not call the same tool twice with the same arguments.\n\nWhat to look for:\n- Tool errors: the model uses a tool wrongly or the tool is not suited to the job. Recommend an instruction that explains how to use it, or removing the tool from the agent\'s list.\n- Retries: the same call repeated with the same arguments. Recommend an instruction telling the agent to stop or change approach after a failure.\n- High tokens or compaction: large tool responses or too many rounds. Recommend tighter instructions, an instruction to request less data, or fewer tools.\n- Tools that are configured but never called, or whose description does not match the agent\'s job: recommend removing them from the list.\n\nIf the agent\'s purpose or instructions show that the failures are intended (for example it demonstrates budgets, limits or errors), recommend no change and say it is working as intended.\n\nBase every recommendation on numbers you retrieved. Give the agent name, the evidence, and one specific change. Do not recommend anything for an agent with no flags.\n",
    ['review_scorecard', 'review_errors', 'review_retries', 'review_tool_usage', 'review_agent_config', 'review_tool_definition'])


SETUP_MODIFY_TOOLS_SQL = """
DO $do$
DECLARE
    v_name text := 'save_agent_snapshot';
BEGIN
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Save the current configuration of one agent so a change can be reverted. Always call this before apply_agent_change. Returns a history_id that apply_agent_change requires. reason must say which problem the change will fix and cite the evidence. No rows means the agent was not found or may not be changed.',
      sql_statement => 'INSERT INTO monitor.agent_config_history (agent_name, action, old_config, reason)
        SELECT a.name, ''change'', to_jsonb(a), ${reason}
          FROM aidb.agents a
         WHERE a.name = ${agent_name} AND a.name NOT IN (''agent_reviewer'', ''agent_modifier'')
        RETURNING history_id, agent_name',
      params => aidb.tool_params(
        aidb.tool_param('agent_name', 'TEXT', 'The exact name of the agent about to be changed.'),
        aidb.tool_param('reason', 'TEXT', 'The problem being fixed and the evidence.')
      ),
      read_only => false
    );

    v_name := 'apply_agent_change';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Make one small edit to an agent. Requires the history_id returned by save_agent_snapshot for the same agent. To change the instructions, pass old_text, an exact short snippet copied from the current instructions, and new_text, what replaces it (an empty new_text deletes the snippet). To add a line to the end of the instructions, leave old_text empty and pass the line as new_text. To change the tools, pass new_tools as the full comma-separated list of tool names with no spaces. Leave a parameter as an empty string when it is not part of this edit. Call once per edit. No rows means nothing changed, usually because old_text was not found exactly.',
      sql_statement => 'UPDATE aidb.agents
           SET instructions = CASE
                   WHEN NULLIF(${old_text}, '''') IS NOT NULL THEN REPLACE(instructions, ${old_text}, ${new_text})
                   WHEN NULLIF(${new_text}, '''') IS NOT NULL THEN instructions || chr(10) || ${new_text}
                   ELSE instructions END,
               tools        = CASE WHEN NULLIF(${new_tools}, '''') IS NULL THEN tools
                                   ELSE string_to_array(${new_tools}, '','') END,
               updated_at   = now()
         WHERE name = ${agent_name}
           AND name NOT IN (''agent_reviewer'', ''agent_modifier'')
           AND (NULLIF(${old_text}, '''') IS NULL OR POSITION(${old_text} IN instructions) > 0)
           AND EXISTS (SELECT NULL FROM monitor.agent_config_history h
                        WHERE h.history_id = CAST(${history_id} AS BIGINT) AND h.agent_name = ${agent_name})
        RETURNING name AS agent_name',
      params => aidb.tool_params(
        aidb.tool_param('agent_name', 'TEXT', 'The exact name of the agent to change.'),
        aidb.tool_param('history_id', 'TEXT', 'The history_id number returned by save_agent_snapshot for this agent, for example 7. Never guess it; call save_agent_snapshot first.'),
        aidb.tool_param('old_text', 'TEXT', 'An exact snippet of the current instructions to replace, or an empty string to append to the end.'),
        aidb.tool_param('new_text', 'TEXT', 'The replacement text, or the line to append. An empty string deletes the snippet.'),
        aidb.tool_param('new_tools', 'TEXT', 'Full comma-separated list of tool names with no spaces, or an empty string to leave the tools unchanged.')
      ),
      read_only => false
    );

    v_name := 'revert_agent';
    IF EXISTS (SELECT NULL FROM aidb.sql_tool_registry WHERE name = v_name) THEN
        PERFORM aidb.delete_tool(v_name);
    END IF;

    PERFORM aidb.create_sql_tool(
      name => v_name,
      description => 'Undo a change by restoring the instructions and tools saved in a history row. No rows means the history_id was not found.',
      sql_statement => 'UPDATE aidb.agents a
           SET instructions = h.old_config->>''instructions'',
               tools        = CASE WHEN jsonb_typeof(h.old_config->''tools'') = ''array''
                                   THEN ARRAY(SELECT jsonb_array_elements_text(h.old_config->''tools''))
                                   ELSE NULL END,
               updated_at   = now()
          FROM monitor.agent_config_history h
         WHERE h.history_id = CAST(${history_id} AS BIGINT) AND a.name = h.agent_name
        RETURNING a.name AS agent_name',
      params => aidb.tool_params(
        aidb.tool_param('history_id', 'TEXT', 'The history_id number from monitor.agent_config_history, for example 7.')
      ),
      read_only => false
    );
END
$do$;
"""

SETUP_MODIFIER_SQL = agent_sql(
    'agent_modifier',
    'You apply recommended changes to AIDB agents. You change only the agent named in the request, and only what the recommendation says. Keep everything you write short.\n\nSteps:\n1. Call review_agent_config for the agent to read its current instructions and tools. Do not skip this step.\n2. Decide the smallest edits that implement the recommendation. Never rewrite the whole instructions. Each instruction edit replaces one short snippet, a line or two, copied exactly from the current instructions, or adds one line at the end. A tool change is the full new comma-separated list with no spaces.\n3. Call save_agent_snapshot once, with the agent name and a reason of one or two short sentences. Note the history_id it returns; you must have it before step 4, never invent one.\n4. Call apply_agent_change once per edit, with the same agent name and that history_id. Use an empty string for any parameter that is not part of that edit.\n5. Reply in three lines or fewer: the agent name, the history_id, and what changed.\n\nIf a call returns an error or no rows, report it and stop. Do not retry. Never change instructions or tools that the recommendation does not mention. Only call revert_agent when the request asks for a revert, and give it the history_id from the request.\n',
    ['review_agent_config', 'save_agent_snapshot', 'apply_agent_change', 'revert_agent'])

def script(statements: list[str]) -> str:
    return "\n\n".join(s.strip().rstrip(";") + ";" for s in statements)

STEPS = [
    dict(key="model", title="Claude model", group="model", names=[MODEL_NAME],
         sql=SETUP_MODEL_SQL, needs=[]),
    dict(key="objects", title="Schema, table and views", group="objects", names=VIEW_NAMES,
         sql=script(SETUP_OBJECT_STATEMENTS), needs=[]),
    dict(key="review_tools", title="Review tools (read the logs)", group="tools",
         names=REVIEW_TOOL_NAMES, sql=SETUP_TOOLS_SQL, needs=["objects"]),
    dict(key="modify_tools", title="Modify tools (change and revert agents)", group="tools",
         names=MODIFY_TOOL_NAMES, sql=SETUP_MODIFY_TOOLS_SQL, needs=["objects"]),
    dict(key="reviewer", title="Reviewer agent", group="agents", names=[REVIEWER_NAME],
         sql=SETUP_AGENT_SQL, needs=["model", "review_tools"]),
    dict(key="modifier", title="Modifier agent", group="agents", names=[MODIFIER_NAME],
         sql=SETUP_MODIFIER_SQL, needs=["model", "review_tools", "modify_tools"]),
]
STEP_BY_KEY = {s["key"]: s for s in STEPS}
GROUP_TITLES = [("model", "Model"), ("objects", "Schema, table, views"),
                ("tools", "Tools"), ("agents", "Agents")]


LITELLM_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
# Models the agents use now, plus the models earlier versions used (from the saved configurations)
USED_MODELS_SQL = """SELECT DISTINCT mi.model_id FROM monitor.model_ids mi
WHERE mi.model_id IS NOT NULL
  AND mi.model_name IN (SELECT model FROM aidb.agents
                        UNION SELECT old_config->>'model' FROM monitor.agent_config_history)
ORDER BY 1"""


def fetch_litellm_prices(model_ids: list[str]) -> tuple[dict, list[str]]:
    """Look the models up in LiteLLM's public price list. Returns ({model_id: (input, output)}, not_found),
    with prices in US dollars per million tokens."""
    import urllib.request
    with urllib.request.urlopen(LITELLM_PRICES_URL, timeout=30) as response:
        data = json.load(response)
    found, missing = {}, []
    for model_id in model_ids:
        entry = data.get(model_id)
        if not isinstance(entry, dict):
            entry = next((v for k, v in data.items() if k.endswith("/" + model_id) and isinstance(v, dict)), None)
        if entry and entry.get("input_cost_per_token") is not None and entry.get("output_cost_per_token") is not None:
            found[model_id] = (round(entry["input_cost_per_token"] * 1e6, 6),
                               round(entry["output_cost_per_token"] * 1e6, 6))
        else:
            missing.append(model_id)
    return found, missing

def reviewer_prompt(agent: str) -> str:
    return (f"Review only the agent named {agent}. Use '{agent}' as the agent_pattern and agent_name "
            f"when you call the review tools. Recommend specific changes to its instructions or tools, "
            f"each with the evidence. Do not change anything.")


def modifier_prompt(agent: str, recommendation: str) -> str:
    return (f"Apply this recommendation to the agent {agent}. Change only that agent. Make small edits; "
            f"do not rewrite the whole instructions.\n\n{recommendation}")
