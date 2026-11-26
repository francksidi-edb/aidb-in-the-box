-- bootstrap.sql  (run by entrypoint.sh as postgres, in database "aidb", on first start)
-- Installs the extensions, creates the roles (roles.sql) and the sample "Orbit Cloud support desk" data, and opens
-- the AIDB egress allowlist to Anthropic and Hugging Face.
-- The governance objects (guard schema, tools, agents, purposes) are created by lab.install() in the notebooks.

CREATE EXTENSION IF NOT EXISTS vector CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_tokenizer;
CREATE EXTENSION IF NOT EXISTS vchord_bm25;
CREATE EXTENSION IF NOT EXISTS pgfs;
CREATE EXTENSION IF NOT EXISTS aidb CASCADE;

-- Persist: record governance decisions as spans in aidb_otel.traces
ALTER DATABASE aidb SET aidb.otel_client = 'database';

-- ── people ────────────────────────────────────────────────────────────────────
\i /roles.sql

-- ── sample data ───────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS support AUTHORIZATION demo_user;
SET search_path = support, public;

CREATE TABLE IF NOT EXISTS support.articles (
  id        int PRIMARY KEY,
  title     text NOT NULL,
  category  text NOT NULL,
  body      text NOT NULL
);

CREATE TABLE IF NOT EXISTS support.customers (
  id     int PRIMARY KEY,
  name   text NOT NULL,
  tier   text NOT NULL,
  region text NOT NULL
);

CREATE TABLE IF NOT EXISTS support.tickets (
  id          int PRIMARY KEY,
  customer_id int REFERENCES support.customers(id),
  subject     text NOT NULL,
  status      text NOT NULL DEFAULT 'open',
  priority    text NOT NULL DEFAULT 'normal',
  assignee    text,
  opened_at   timestamptz NOT NULL DEFAULT now()
);

TRUNCATE support.articles, support.tickets, support.customers CASCADE;

INSERT INTO support.articles(id, title, category, body) VALUES
(1, 'Disk full on the WAL volume', 'operations',
 'When the WAL volume fills up Postgres stops accepting writes and may shut down. First find out what is holding WAL back: an inactive replication slot is the usual cause. Check pg_replication_slots for slots with active = false and a large restart_lsn lag. If the consumer is gone for good, drop the slot with pg_drop_replication_slot(). Then run a CHECKPOINT and confirm that old segments in pg_wal are recycled. Never delete files from pg_wal by hand: that corrupts recovery. As a safety net set max_slot_wal_keep_size so that a stuck slot can no longer fill the disk.'),
(2, 'Restoring a database to a point in time (PITR)', 'backup',
 'Point-in-time recovery needs a base backup plus the archived WAL. Stop the broken instance, restore the base backup into an empty data directory and set recovery_target_time in postgresql.conf. Create an empty recovery.signal file and start Postgres. It replays WAL up to the target time and then pauses. Check the data, then run pg_wal_replay_resume() to end recovery and promote. With Barman the whole procedure is a single barman recover command with the --target-time option.'),
(3, 'Connection pooling with PgBouncer', 'performance',
 'Too many connections to Postgres cost memory and context switches. Put PgBouncer in front of the database in transaction pooling mode and keep default_pool_size close to the number of CPU cores of the database server. Applications that rely on session state such as temporary tables, advisory locks or SET commands must use session pooling instead. Watch the cl_waiting column of SHOW POOLS: if clients wait, the pool is too small or queries are too slow.'),
(4, 'Tuning autovacuum for a busy table', 'performance',
 'A table with heavy updates can bloat when autovacuum cannot keep up. Lower autovacuum_vacuum_scale_factor for that table to 0.02 and raise autovacuum_vacuum_cost_limit so the worker is throttled less. Look at n_dead_tup in pg_stat_user_tables to see the effect. For emergency cleanup run VACUUM (VERBOSE, ANALYZE) manually. Avoid VACUUM FULL on a production table because it takes an exclusive lock for the whole duration.'),
(5, 'Failover runbook for a streaming replica', 'high-availability',
 'If the primary is lost, promote the most advanced standby. Compare the replay LSN of every standby with pg_last_wal_replay_lsn() and pick the highest. Promote it with pg_ctl promote or SELECT pg_promote(). Repoint the application through the virtual IP or the connection pooler, then rebuild the old primary as a new standby with pg_rewind. With EDB Failover Manager or Patroni this sequence is automated, but the manual runbook is still needed for the day the automation itself fails.'),
(6, 'Renewing the server TLS certificate', 'security',
 'Postgres reads the server certificate at start and on reload. Place the new certificate and key in the data directory, make sure the key has permission 0600 and is owned by the postgres user, and run SELECT pg_reload_conf(). Verify with openssl s_client -connect host:5432 -starttls postgres. Clients that use sslmode=verify-full need the new CA if the issuer changed, so roll the CA bundle out first.'),
(7, 'Rotating a database password without downtime', 'security',
 'Create the new password with ALTER ROLE app_user PASSWORD and store it in your secret manager. Applications that read the secret at connection time pick it up on the next connection. Long-lived pools must be recycled, so trigger a rolling restart or a pool refresh. Use scram-sha-256 and not md5. Finally check pg_stat_activity to confirm that no session still uses the retired credentials.'),
(8, 'Triage of a slow query', 'performance',
 'Start with EXPLAIN (ANALYZE, BUFFERS) on the exact statement with realistic parameters. Look for sequential scans on big tables, row estimates that are far from reality and sorts that spill to disk. Run ANALYZE if the statistics are stale. A missing index is the most common fix: create it with CREATE INDEX CONCURRENTLY to avoid blocking writes. pg_stat_statements tells you which statements consume the most total time, which is a better target than the single slowest query.'),
(9, 'Resolving logical replication conflicts', 'replication',
 'A subscriber stops with a duplicate key or missing row error when the same data is changed on both sides. Read the subscriber log to find the failing transaction and its LSN. Either fix the data on the subscriber or skip the transaction with ALTER SUBSCRIPTION ... SKIP (lsn = ...). Skipping loses that change, so only do it after you have reconciled the rows. For active-active setups use EDB Postgres Distributed which has built-in conflict detection and resolution.'),
(10, 'Upgrading to a new major Postgres version', 'operations',
 'Use pg_upgrade with the --link option for a fast in-place upgrade: it needs minutes and not hours, even for terabytes. Always run pg_upgrade --check first. After the upgrade run the generated analyze script, because optimizer statistics are not carried over. Keep the old cluster until the application is verified. Extensions must be installed for the new version before you start, including pgvector.'),
(11, 'Tuning pgvector indexes for semantic search', 'ai',
 'pgvector offers HNSW and IVFFlat indexes. HNSW gives better recall and does not need training data, at the cost of build time and memory. Raise hnsw.ef_search at query time to trade speed for recall. IVFFlat needs the table to be filled before the index is built and a probes setting at query time. Match the distance operator of the index (cosine, L2, inner product) to the operator you use in queries or the index will not be used.'),
(12, 'Monitoring checklist for a production cluster', 'operations',
 'Alert on replication lag in bytes and seconds, on the oldest transaction age to catch wraparound risk, on the number of connections against max_connections, on free disk for data and WAL separately, and on failed archive_command attempts in pg_stat_archiver. Track cache hit ratio and checkpoint frequency as trend metrics, not as alerts. Test your alerts: a monitoring rule that never fired may never work.');

INSERT INTO support.customers(id, name, tier, region) VALUES
(1, 'Aurora Bank',        'platinum', 'EMEA'),
(2, 'Brightside Retail',  'gold',     'NA'),
(3, 'Cobalt Logistics',   'gold',     'EMEA'),
(4, 'Delta Health',       'platinum', 'NA'),
(5, 'Evergreen Telecom',  'silver',   'APAC'),
(6, 'Fjord Energy',       'silver',   'EMEA'),
(7, 'Granite Insurance',  'gold',     'NA'),
(8, 'Helios Media',       'bronze',   'APAC');

INSERT INTO support.tickets(id, customer_id, subject, status, priority, assignee, opened_at) VALUES
(1001, 1, 'WAL volume at 97% on primary, writes failing',            'open',        'critical', 'maria',  now() - interval '2 hours'),
(1002, 4, 'Need PITR to 09:15 after bad deployment',                 'in_progress', 'critical', 'jonas',  now() - interval '5 hours'),
(1003, 2, 'Checkout queries 10x slower since Monday',                'open',        'high',     'priya',  now() - interval '1 day'),
(1004, 3, 'Replica lag growing during nightly batch',                'open',        'high',     NULL,     now() - interval '1 day 3 hours'),
(1005, 5, 'Rotate application password for billing service',         'open',        'normal',   'maria',  now() - interval '2 days'),
(1006, 6, 'TLS certificate expires in 5 days',                       'open',        'high',     'jonas',  now() - interval '2 days'),
(1007, 7, 'Plan upgrade from Postgres 15 to 18',                     'in_progress', 'normal',   'priya',  now() - interval '4 days'),
(1008, 8, 'Too many connections errors in the evening',              'open',        'normal',   NULL,     now() - interval '3 days'),
(1009, 1, 'Logical subscription stopped: duplicate key violation',   'open',        'high',     'maria',  now() - interval '6 hours'),
(1010, 4, 'Table orders_events is 40 GB, mostly dead tuples',        'open',        'normal',   NULL,     now() - interval '5 days'),
(1011, 2, 'Semantic search returns poor results after reindex',      'in_progress', 'normal',   'priya',  now() - interval '1 day 8 hours'),
(1012, 3, 'Failover test: how do we choose the new primary?',        'resolved',    'low',      'jonas',  now() - interval '9 days'),
(1013, 5, 'Alerting rules for replication lag',                      'resolved',    'low',      'maria',  now() - interval '12 days'),
(1014, 6, 'Disk usage alert on data volume',                         'closed',      'normal',   'jonas',  now() - interval '20 days'),
(1015, 7, 'Autovacuum workers busy all day',                         'open',        'normal',   NULL,     now() - interval '7 hours'),
(1016, 8, 'Request: read-only user for the BI team',                 'open',        'low',      NULL,     now() - interval '10 hours');

ALTER TABLE support.articles  OWNER TO demo_user;
ALTER TABLE support.customers OWNER TO demo_user;
ALTER TABLE support.tickets   OWNER TO demo_user;

-- ── AIDB egress allowlist ─────────────────────────────────────────────────────
-- Let AIDB reach Anthropic and Hugging Face. Missing hosts are appended to an existing allowlist, never replacing it.
-- An empty allowlist is left empty (no restriction), since filling it would block every other host.
-- ALTER SYSTEM cannot run inside a DO block, so build the statement and run it with \gexec.
-- The setting takes effect when Postgres restarts at the end of the first start.
WITH cur AS (SELECT current_setting('aidb.egress_allowlist') AS v),
     want(h) AS (VALUES ('api.anthropic.com'), ('huggingface.co'), ('.huggingface.co'), ('cdn-lfs.huggingface.co'),
                        ('hf.co'), ('.hf.co'), ('cas-bridge.xethub.hf.co'), ('.xethub.hf.co')),
     missing AS (SELECT h FROM want, cur WHERE NOT (h = ANY (string_to_array(replace(cur.v, ' ', ''), ','))))
SELECT format('ALTER SYSTEM SET aidb.egress_allowlist = %L', concat_ws(', ', NULLIF(cur.v, ''), string_agg(h, ', ')))
FROM cur, missing
GROUP BY cur.v
HAVING count(*) > 0 AND cur.v <> ''
\gexec
