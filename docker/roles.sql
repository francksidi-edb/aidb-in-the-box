-- roles.sql  (run by entrypoint.sh as postgres, in database "aidb", on every start; also part of bootstrap.sql)
-- The demo roles, and the settings and functions they rely on. Every statement can run again, so volumes created before
-- something here was added get it on the next start.
--
-- demo_user      application user: creates and calls agents; the apps and the Agents notebooks connect as it
-- demo_governor  governance team: owns purposes, reads the audit                                (aidb_governance)
-- demo_monitor   Token Boss: reads every user's agent activity (monitor_feed) and changes demo_user's agents
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'demo_user') THEN
    CREATE ROLE demo_user LOGIN PASSWORD 'demo';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'demo_governor') THEN
    CREATE ROLE demo_governor LOGIN PASSWORD 'demo';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'demo_monitor') THEN
    CREATE ROLE demo_monitor LOGIN PASSWORD 'demo';
  END IF;
END $$;
GRANT aidb_users      TO demo_user;
GRANT aidb_governance TO demo_governor;
GRANT aidb_governance TO demo_user;  -- demo convenience: lets the UI read the audit trail as one user

-- Membership in demo_user lets Token Boss change demo_user's agents: AIDB lets only the owner role update an agent.
-- (Not BYPASSRLS: AIDB enforces that ownership with row-level security, so it would let Token Boss rewrite anyone's.)
GRANT aidb_users, aidb_governance, demo_user TO demo_monitor;

-- Workaround for AIDB bug AID-6739 (https://enterprisedb.atlassian.net/browse/AID-6739): while a semantic knowledge
-- base with auto_processing other than 'Disabled' exists, AIDB's DDL event trigger reads
-- aidb_internal.semantic_knowledge_bases on every CREATE/ALTER/DROP TABLE or VIEW and COMMENT, so these fail for every
-- role that cannot read that table.
DO $$ BEGIN
  IF to_regclass('aidb_internal.semantic_knowledge_bases') IS NOT NULL THEN  -- AIDB is not pinned: skip once it changes
    GRANT SELECT ON aidb_internal.semantic_knowledge_bases TO demo_user, demo_monitor;
  END IF;
END $$;

-- pipelines and knowledge bases create their tables in schema public (PG15+ no longer allows CREATE there by default)
GRANT USAGE, CREATE ON SCHEMA public TO demo_user;

-- aidb.run_pipeline reads pgfs.allowed_local_fs_paths, which exists only once pgfs is loaded in the session; without
-- it the pipeline sees no allowed folder. Load pgfs in every session instead of calling a pgfs function first.
ALTER DATABASE aidb SET session_preload_libraries = 'pgfs';

-- Token Boss creates schema monitor for its views and tables. Before demo_monitor existed it ran as postgres, so
-- hand over a schema created back then, with everything in it.
GRANT CREATE ON DATABASE aidb TO demo_monitor;
DO $$
DECLARE
  r record;
BEGIN
  IF NOT EXISTS (SELECT FROM pg_namespace WHERE nspname = 'monitor' AND nspowner <> 'demo_monitor'::regrole) THEN
    RETURN;
  END IF;
  ALTER SCHEMA monitor OWNER TO demo_monitor;
  FOR r IN SELECT c.oid::regclass AS rel,
                  CASE c.relkind WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END AS kind
           FROM pg_class c
           WHERE c.relnamespace = 'monitor'::regnamespace AND c.relkind IN ('r', 'v', 'p')
             AND c.relowner <> 'demo_monitor'::regrole LOOP
    EXECUTE format('ALTER %s %s OWNER TO demo_monitor', r.kind, r.rel);  -- a table's sequences follow it
  END LOOP;
END $$;

-- ── monitor_feed: AIDB data that AIDB 7.10 offers no public interface for ──────────────────────────────────────────
-- Read in this one place, by functions that belong to postgres and run as it (SECURITY DEFINER). They take no
-- arguments and only read. Functions rather than views: Postgres records no dependency on what a function body reads,
-- so these never block an AIDB upgrade that changes or drops the objects they read.
CREATE SCHEMA IF NOT EXISTS monitor_feed AUTHORIZATION postgres;
GRANT USAGE ON SCHEMA monitor_feed TO demo_user, demo_governor, demo_monitor;
-- AIDB is not pinned: if a release renames what a function reads, that function fails when called, but this script
-- (and the first start) still runs to the end.
SET check_function_bodies = off;

-- Every user's agent actions. Row-level security shows an aidb_users member only the actions it started itself
-- (aidb_internal.action_log), and no public view has tool calls, arguments or results; AIDB's docs call this table
-- the agent's history.
CREATE OR REPLACE FUNCTION monitor_feed.action_log()
RETURNS TABLE (task_id uuid, conversation_id uuid, agent_id uuid, action_type text, status text,
               request_payload jsonb, response_payload jsonb, created_at timestamptz, completed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT task_id, conversation_id, agent_id, action_type::text, status::text, request_payload, response_payload,
         created_at, completed_at
  FROM aidb_internal.action_log
$$;

-- The token usage the model provider reported for each task: AIDB records it on the task's agent_converse span
-- (gen_ai.usage.input_tokens / output_tokens in aidb_otel.traces) when aidb.otel_client = 'database'. Only AIDB's
-- own spans count, not ones sent in from outside. Values that are not whole numbers give NULL.
CREATE OR REPLACE FUNCTION monitor_feed.task_usage()
RETURNS TABLE (task_id text, input_raw text, output_raw text, input_tokens bigint, output_tokens bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT DISTINCT ON (task_id) task_id, input_raw, output_raw,
         CASE WHEN pg_input_is_valid(input_raw, 'bigint') THEN CAST(input_raw AS bigint) END,
         CASE WHEN pg_input_is_valid(output_raw, 'bigint') THEN CAST(output_raw AS bigint) END
  FROM (SELECT attrs.a->>'aidb.agent.task.id' AS task_id,
               attrs.a->>'gen_ai.usage.input_tokens' AS input_raw,
               attrs.a->>'gen_ai.usage.output_tokens' AS output_raw
        FROM aidb_otel.traces t
        CROSS JOIN LATERAL jsonb_path_query(
            t.payload_json, '$.resourceSpans[*].scopeSpans[*].spans[*] ? (@.name == "agent_converse")') s
        CROSS JOIN LATERAL (
            SELECT jsonb_object_agg(x->>'key', COALESCE(x->'value'->>'intValue', x->'value'->>'stringValue',
                                                        x->'value'->>'doubleValue'))
                       FILTER (WHERE x->>'key' IS NOT NULL) AS a
            FROM jsonb_array_elements(s->'attributes') x) attrs
        WHERE t.source = 'internal' AND t.agent_name IS NOT NULL) spans  -- agent spans only, before parsing payloads
  WHERE task_id IS NOT NULL
  ORDER BY task_id, (input_raw IS NOT NULL OR output_raw IS NOT NULL) DESC
$$;

-- The provider's model id (for example claude-sonnet-5) of each AIDB model, for Token Boss's prices and the audit
-- log's model version. AIDB 7.10 exposes a model's configuration only as a 'config=<json>' entry in
-- aidb.models.options; anything that is not valid JSON gives NULL.
CREATE OR REPLACE FUNCTION monitor_feed.model_ids()
RETURNS TABLE (model_name text, provider text, model_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
  SELECT m.name, m.provider,
         (SELECT CAST(substr(o, 8) AS jsonb)->>'model'
          FROM unnest(m.options) AS o
          WHERE o LIKE 'config=%' AND pg_input_is_valid(substr(o, 8), 'jsonb')
          LIMIT 1)
  FROM aidb.models m
$$;

RESET check_function_bodies;

REVOKE ALL ON FUNCTION monitor_feed.action_log(), monitor_feed.task_usage(), monitor_feed.model_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor_feed.action_log(), monitor_feed.task_usage() TO demo_monitor;
GRANT EXECUTE ON FUNCTION monitor_feed.model_ids() TO demo_user, demo_governor, demo_monitor;
