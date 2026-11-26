-- roles.sql  (run by entrypoint.sh as postgres, in database "aidb", on every start; also part of bootstrap.sql)
-- The demo roles. Every statement can run again, so volumes created before a role existed get it on the next start.
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

-- Row-level security shows an aidb_users member only the agent actions it started itself
-- (aidb_internal.action_log). Token Boss reads every user's through this view instead: it belongs to postgres, so it
-- reads the whole table, and demo_monitor may only SELECT from it. Only the columns Token Boss reads, so an AIDB upgrade
-- that changes others is not blocked by this view.
CREATE SCHEMA IF NOT EXISTS monitor_feed AUTHORIZATION postgres;
CREATE OR REPLACE VIEW monitor_feed.action_log AS
SELECT task_id, conversation_id, agent_id, action_type, status, request_payload, response_payload, created_at,
       completed_at
FROM aidb_internal.action_log;
GRANT USAGE ON SCHEMA monitor_feed TO demo_monitor;
GRANT SELECT ON monitor_feed.action_log TO demo_monitor;

-- pipelines and knowledge bases create their tables in schema public (PG15+ no longer allows CREATE there by default)
GRANT USAGE, CREATE ON SCHEMA public TO demo_user;

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
