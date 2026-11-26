-- guard_runtime.sql  (run as postgres, in database "aidb"; safe to run again)
-- The governance layer used by the notebooks: policy tables, an append-only event log, PII masking,
-- personas with row- and column-level security, and the roles behind two purposes.
-- It does not touch the base sample tables (support.articles / customers / tickets).

CREATE SCHEMA IF NOT EXISTS guard;
GRANT USAGE ON SCHEMA guard TO aidb_users, aidb_governance;

-- ── policy switches (C-34, C-37) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guard.settings (
  name text PRIMARY KEY, enabled boolean NOT NULL, value numeric, note text,
  updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO guard.settings(name, enabled, value, note) VALUES
  ('app_guardrails',   true, NULL, 'master switch: every app-layer control below'),
  ('prompt_screening', true, NULL, 'block or flag injection, jailbreak, off-topic and forbidden requests'),
  ('intent_monitor',   true, 0.55, 'flag prompts that drift from the agent''s intent (value = minimum similarity)'),
  ('pii_redaction',    true, NULL, 'mask PII before the model and before the user'),
  ('approvals',        true, NULL, 'hold write requests until a human approves')
ON CONFLICT (name) DO NOTHING;
GRANT SELECT ON guard.settings TO aidb_users, aidb_governance;
GRANT UPDATE (enabled, value, updated_at) ON guard.settings TO aidb_governance;

-- ── rules (C-30) ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guard.rules (
  id serial PRIMARY KEY, category text NOT NULL, pattern text NOT NULL,
  action text NOT NULL CHECK (action IN ('block','approve','flag')),
  enabled boolean NOT NULL DEFAULT true, note text,
  UNIQUE (category, pattern));
INSERT INTO guard.rules(category, pattern, action, note) VALUES
  ('prompt_injection', 'ignore (all |any |your |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)', 'block', 'tries to override the agent''s instructions'),
  ('prompt_injection', 'disregard .{0,30}(instructions|rules|polic)',                                              'block', 'tries to override the agent''s instructions'),
  ('prompt_injection', '(reveal|show|print|repeat|leak).{0,30}(system prompt|your instructions|hidden instructions)', 'block', 'tries to read the system prompt'),
  ('jailbreak',        '\mDAN\M',                                                                                  'block', 'known jailbreak persona'),
  ('jailbreak',        'developer mode|jailbreak',                                                                 'block', 'asks the model to drop its rules'),
  ('jailbreak',        'pretend (you are|to be) .{0,60}(no|without|free of) (rules|restrictions|limits|filters)',  'block', 'asks the model to drop its rules'),
  ('forbidden_action', 'dump (all|every|the whole|the entire|full).{0,25}(customers?|tickets?|contacts?|tables?|database)', 'block', 'bulk exfiltration'),
  ('forbidden_action', '\m(drop|truncate)\s+(table|schema|database)',                                              'block', 'destructive DDL'),
  ('forbidden_action', '\mdelete\s+from\M',                                                                         'block', 'destructive DML'),
  ('forbidden_action', '(pg_shadow|pg_authid|information_schema)',                                                  'block', 'catalog probing'),
  ('write_request',    '\m(assign|reassign|close|reopen|escalate)\M.{0,30}\mticket',                               'approve', 'changes data: a human must approve'),
  ('off_topic',        '\m(poem|joke|recipe|lyrics|horoscope|limerick)\M',                                          'flag', 'not a support desk question'),
  ('off_topic',        '(write|compose) (me )?(a|an) (story|essay|song)',                                           'flag', 'not a support desk question')
ON CONFLICT (category, pattern) DO NOTHING;
GRANT SELECT ON guard.rules TO aidb_users, aidb_governance;
GRANT INSERT, UPDATE, DELETE ON guard.rules TO aidb_governance;
GRANT USAGE ON SEQUENCE guard.rules_id_seq TO aidb_governance;

-- every rule that matches, most severe first
CREATE OR REPLACE FUNCTION guard.screen_prompt(p_prompt text)
RETURNS TABLE(action text, category text, pattern text, note text) LANGUAGE sql STABLE AS $$
  SELECT r.action, r.category, r.pattern, r.note FROM guard.rules r
  WHERE r.enabled AND p_prompt ~* r.pattern
  ORDER BY CASE r.action WHEN 'block' THEN 1 WHEN 'approve' THEN 2 ELSE 3 END, r.id
$$;

-- ── intent monitoring (C-35) ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS guard.agent_intents (
  agent text NOT NULL, exemplar text NOT NULL, embedding vector(384) NOT NULL, PRIMARY KEY (agent, exemplar));
GRANT SELECT ON guard.agent_intents TO aidb_users, aidb_governance;
GRANT INSERT, DELETE ON guard.agent_intents TO aidb_governance;

CREATE OR REPLACE FUNCTION guard.register_intent(p_agent text, p_exemplars text[]) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE e text; n int := 0;
BEGIN
  DELETE FROM guard.agent_intents WHERE agent = p_agent;
  FOREACH e IN ARRAY p_exemplars LOOP
    INSERT INTO guard.agent_intents VALUES (p_agent, e, aidb.encode_text('bge-small-en-v1.5-f16', e)::vector);
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- highest cosine similarity between the prompt and any of the agent's example requests
CREATE OR REPLACE FUNCTION guard.intent_score(p_agent text, p_prompt text) RETURNS numeric
LANGUAGE sql STABLE AS $$
  SELECT round(max(1 - (i.embedding <=> aidb.encode_text('bge-small-en-v1.5-f16', p_prompt)::vector))::numeric, 3)
  FROM guard.agent_intents i WHERE i.agent = p_agent
$$;

-- ── PII (C-36) ────────────────────────────────────────────────────────────────
-- phone: international (+31 6 12345678), national with a leading 0 (020 7946 0958) or North American (415-555-0100),
-- so dates and IDs such as 2026-11-26 or INV-2026-10428 are not taken for phone numbers
CREATE OR REPLACE FUNCTION guard.pii_types(t text) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$
  SELECT coalesce(array_remove(ARRAY[
    CASE WHEN t ~* '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}' THEN 'email' END,
    CASE WHEN t ~ '\m\d{3}-\d{2}-\d{4}\M' THEN 'ssn' END,
    CASE WHEN t ~ '\m(?:\d[ -]?){13,16}\M' THEN 'card' END,
    CASE WHEN regexp_replace(regexp_replace(t, '\m(?:\d[ -]?){13,16}\M', '', 'g'), '\m\d{3}-\d{2}-\d{4}\M', '', 'g')
              ~ '\+\d[\d ().-]{7,}\d|\(\d{3}\) ?\d{3}[ .-]\d{4}\M|\m\d{3}[ .-]\d{3}[ .-]\d{4}\M|\m0\d([ -]?\d){8,10}\M' THEN 'phone' END], NULL), ARRAY[]::text[])
$$;

CREATE OR REPLACE FUNCTION guard.mask_pii(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT regexp_replace(
           regexp_replace(
             regexp_replace(
               regexp_replace(t, '[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', '[EMAIL]', 'gi'),
               '\m\d{3}-\d{2}-\d{4}\M', '[SSN]', 'g'),
             '\m(?:\d[ -]?){13,16}\M', '[CARD]', 'g'),
           '\+\d[\d ().-]{7,}\d|\(\d{3}\) ?\d{3}[ .-]\d{4}\M|\m\d{3}[ .-]\d{3}[ .-]\d{4}\M|\m0\d([ -]?\d){8,10}\M', '[PHONE]', 'g')
$$;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA guard TO aidb_users, aidb_governance;

-- ── append-only event log (C-31, C-33, C-35, C-37) ────────────────────────────
CREATE TABLE IF NOT EXISTS guard.events (
  id bigserial PRIMARY KEY,
  at timestamptz NOT NULL DEFAULT clock_timestamp(),
  actor text NOT NULL DEFAULT session_user,
  agent text, model text, model_version text,
  prompt text, prompt_sent text,
  decision text NOT NULL,           -- allowed | blocked | flagged | pending_approval | approved | rejected
  policy text,                      -- which rule or monitor fired
  intent_score numeric, pii_found text[], guardrails_on boolean,
  conversation_id text, reply text, sources jsonb, detail jsonb);
CREATE OR REPLACE FUNCTION guard.append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'guard.events is append-only: % is not allowed', TG_OP; END $$;
DROP TRIGGER IF EXISTS events_append_only ON guard.events;
CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON guard.events FOR EACH ROW EXECUTE FUNCTION guard.append_only();
DROP TRIGGER IF EXISTS events_no_truncate ON guard.events;
CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON guard.events FOR EACH STATEMENT EXECUTE FUNCTION guard.append_only();
REVOKE ALL ON guard.events FROM PUBLIC;
REVOKE INSERT ON guard.events FROM aidb_users, aidb_governance;   -- earlier versions granted it
REVOKE USAGE ON SEQUENCE guard.events_id_seq FROM aidb_users, aidb_governance;
GRANT SELECT ON guard.events TO aidb_users, aidb_governance;

-- the only way to write an event: the actor is always the logged-in role, so nobody can log in someone else's name,
-- and only the governance team can resolve an approval
CREATE OR REPLACE FUNCTION guard.log_event(
  p_decision text, p_agent text DEFAULT NULL, p_model text DEFAULT NULL, p_model_version text DEFAULT NULL,
  p_prompt text DEFAULT NULL, p_prompt_sent text DEFAULT NULL, p_policy text DEFAULT NULL,
  p_intent_score numeric DEFAULT NULL, p_pii_found text[] DEFAULT NULL, p_guardrails_on boolean DEFAULT NULL,
  p_conversation_id text DEFAULT NULL, p_reply text DEFAULT NULL, p_sources jsonb DEFAULT NULL, p_detail jsonb DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_id bigint;
BEGIN
  IF p_decision IN ('approved', 'rejected') AND NOT pg_has_role(session_user, 'aidb_governance', 'MEMBER') THEN
    RAISE EXCEPTION 'only members of aidb_governance can log the decision %', p_decision;
  END IF;
  INSERT INTO guard.events (actor, agent, model, model_version, prompt, prompt_sent, decision, policy, intent_score,
                            pii_found, guardrails_on, conversation_id, reply, sources, detail)
  VALUES (session_user, p_agent, p_model, p_model_version, p_prompt, p_prompt_sent, p_decision, p_policy, p_intent_score,
          p_pii_found, p_guardrails_on, p_conversation_id, p_reply, p_sources, p_detail)
  RETURNING id INTO v_id;
  RETURN v_id;
END $$;
REVOKE ALL ON FUNCTION guard.log_event FROM PUBLIC;
GRANT EXECUTE ON FUNCTION guard.log_event TO aidb_users, aidb_governance;

-- who, what prompt, which model, which data, which policy, what decision (C-31)
CREATE OR REPLACE VIEW guard.audit AS
SELECT e.id, e.at, e.actor, e.agent, e.model, e.prompt, e.decision, e.policy, e.intent_score, e.pii_found,
       e.conversation_id, e.sources,
       (SELECT jsonb_object_agg(s.decision_status, s.n) FROM
          (SELECT t.decision_status, count(*) n FROM aidb_otel.traces t
           WHERE t.gen_ai_conversation_id = e.conversation_id AND t.decision_status IS NOT NULL
           GROUP BY 1) s) AS db_decisions
FROM guard.events e;
GRANT SELECT ON guard.audit TO aidb_governance;

-- counts for the policy dashboard (C-37)
CREATE OR REPLACE VIEW guard.policy_counts AS
SELECT date_trunc('day', at)::date AS day,
       count(*)                                              AS requests,
       count(*) FILTER (WHERE decision = 'blocked')          AS blocked,
       count(*) FILTER (WHERE decision = 'flagged')          AS flagged,
       count(*) FILTER (WHERE decision = 'pending_approval') AS approvals_requested,
       count(*) FILTER (WHERE decision = 'approved')         AS approved,
       count(*) FILTER (WHERE decision = 'rejected')         AS rejected,
       count(*) FILTER (WHERE cardinality(coalesce(pii_found, '{}')) > 0) AS redactions
FROM guard.events GROUP BY 1;
GRANT SELECT ON guard.policy_counts TO aidb_governance;

-- ── roles behind the two purposes (C-34) ──────────────────────────────────────
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'guard_ro') THEN CREATE ROLE guard_ro NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'guard_rw') THEN CREATE ROLE guard_rw NOLOGIN; END IF;
END $$;
GRANT aidb_users TO guard_ro, guard_rw;
GRANT guard_ro, guard_rw TO demo_user;

-- ── contacts: sample data that contains PII (C-36) ────────────────────────────
CREATE TABLE IF NOT EXISTS support.contacts (
  id serial PRIMARY KEY, customer_id int REFERENCES support.customers(id),
  name text NOT NULL, email text NOT NULL, phone text NOT NULL);
TRUNCATE support.contacts RESTART IDENTITY;
INSERT INTO support.contacts(customer_id, name, email, phone)
SELECT c.id, (ARRAY['Anna Visser','Tom Becker','Lina Haas','Omar Reyes','Sara Lindqvist','Ravi Patel','Chloe Martin','Jonas Weber'])[1 + (c.id % 8)],
       lower(replace((ARRAY['Anna Visser','Tom Becker','Lina Haas','Omar Reyes','Sara Lindqvist','Ravi Patel','Chloe Martin','Jonas Weber'])[1 + (c.id % 8)], ' ', '.')) || '@' ||
         lower(regexp_replace(c.name, '[^A-Za-z]', '', 'g')) || '.example',
       '+31 6 ' || lpad((12345600 + c.id * 111)::text, 8, '0')
FROM support.customers c;

-- ── personas with row- and column-level security (C-32) ───────────────────────
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'persona_analyst') THEN CREATE ROLE persona_analyst LOGIN PASSWORD 'demo'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'persona_admin')   THEN CREATE ROLE persona_admin   LOGIN PASSWORD 'demo'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'persona_auditor') THEN CREATE ROLE persona_auditor LOGIN PASSWORD 'demo'; END IF;
END $$;
GRANT aidb_users TO persona_analyst, persona_admin, persona_auditor;

DROP TABLE IF EXISTS support.tickets_rls;
CREATE TABLE support.tickets_rls AS
SELECT t.id, t.customer_id, c.name AS customer, c.tier, c.region, t.subject, t.status, t.priority, t.assignee,
       'Call ' || k.name || ' on ' || k.phone || ' or write to ' || k.email AS internal_note
FROM support.tickets t JOIN support.customers c ON c.id = t.customer_id
LEFT JOIN LATERAL (SELECT * FROM support.contacts k WHERE k.customer_id = c.id ORDER BY k.id LIMIT 1) k ON true;
ALTER TABLE support.tickets_rls ADD PRIMARY KEY (id);
ALTER TABLE support.tickets_rls OWNER TO demo_user;
ALTER TABLE support.tickets_rls ENABLE ROW LEVEL SECURITY;
ALTER TABLE support.tickets_rls FORCE ROW LEVEL SECURITY;
CREATE POLICY see_all_admin   ON support.tickets_rls FOR SELECT TO persona_admin   USING (true);
CREATE POLICY see_all_auditor ON support.tickets_rls FOR SELECT TO persona_auditor USING (true);
CREATE POLICY see_emea        ON support.tickets_rls FOR SELECT TO persona_analyst USING (region = 'EMEA');
CREATE POLICY owner_all       ON support.tickets_rls TO demo_user USING (true) WITH CHECK (true);
GRANT USAGE ON SCHEMA support TO persona_analyst, persona_admin, persona_auditor, guard_ro, guard_rw;
GRANT SELECT ON support.tickets_rls TO persona_admin;
GRANT SELECT (id, customer_id, customer, tier, region, subject, status, priority, assignee)
  ON support.tickets_rls TO persona_analyst, persona_auditor;

-- ── data access for the two purposes ──────────────────────────────────────────
GRANT SELECT ON support.articles, support.customers, support.tickets, support.contacts TO guard_ro, guard_rw;
GRANT UPDATE ON support.tickets TO guard_rw;
GRANT SELECT ON support.contacts TO demo_user;
