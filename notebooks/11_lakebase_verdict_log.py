# Databricks notebook source
# MAGIC %md
# MAGIC # 11: Lakebase verdict log
# MAGIC
# MAGIC The guard decides something about every tool call and then throws that decision away.
# MAGIC This table keeps it.
# MAGIC
# MAGIC **Why Postgres and not Delta.** One row per guarded tool call, roughly 14,500 a day
# MAGIC across the fleet, written one at a time at dispatch and read back while being written.
# MAGIC Single-row inserts at that rate are the shape a warehouse handles worst and an OLTP
# MAGIC store handles best.
# MAGIC
# MAGIC **Why not just turn on inference tables.** Model Serving can log every request and
# MAGIC response automatically, and for the model's *recommendation* that would be the better
# MAGIC answer. Three things it cannot see, and they are the reason this table exists:
# MAGIC
# MAGIC 1. **What the wrapper did.** `coerce` is a recommendation. Whether `apply_coercion()`
# MAGIC    actually rewrote the argument, and to what value, is known only to the caller.
# MAGIC 2. **The outcome.** Inference logging stops at the model's response. Whether the tool
# MAGIC    call then succeeded is the label you need to tell a good coercion from a bad one.
# MAGIC 3. **Fail-open events.** When the endpoint is unreachable there is no request to log,
# MAGIC    so the incidents most worth auditing are exactly the ones that leave no trace.

# COMMAND ----------

import requests

PROJECT  = "tiffin-preflight"
ENDPOINT = f"projects/{PROJECT}/branches/production/endpoints/primary"
PGHOST   = "ep-delicate-grass-y1r5bmqd.database.us-west-2.staging.cloud.databricks.com"
PGDB     = "databricks_postgres"
SCHEMA   = "tiffin"
APP_SP   = "097a6d13-a890-42a0-9374-2c0cdf02eeb9"   # tiffin-preflight-demo

ctx   = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST  = ctx.apiUrl().get()
TOKEN = ctx.apiToken().get()
PGUSER = spark.sql("SELECT current_user()").collect()[0][0]
print("workspace:", HOST)
print("pg user  :", PGUSER)

# COMMAND ----------

# MAGIC %md ## 1. Mint a short-lived Lakebase credential

# COMMAND ----------

r = requests.post(f"{HOST}/api/2.0/postgres/credentials",
                  headers={"Authorization": f"Bearer {TOKEN}"},
                  json={"endpoint": ENDPOINT}, timeout=60)
r.raise_for_status()
cred = r.json()
PGPASS = cred["token"]
print("credential acquired, expires:", cred["expire_time"])

# COMMAND ----------

# MAGIC %md ## 2. Connect

# COMMAND ----------

import psycopg2
conn = psycopg2.connect(host=PGHOST, dbname=PGDB, user=PGUSER,
                        password=PGPASS, sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT version()")
print("connected ->", cur.fetchone()[0][:70])

# COMMAND ----------

# MAGIC %md ## 3. The table
# MAGIC
# MAGIC `action` carries `coerce`, `allow` or `guard_error`. The last one is a fail-open: the
# MAGIC guard was unreachable and the call was let through, which is the row that has no
# MAGIC equivalent anywhere else.

# COMMAND ----------

cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

# An earlier iteration of this project created a guard_decisions table with a narrower
# schema, plus a fix_queue table that was removed from the design. Report what is there
# and what it holds before replacing anything, so nothing is dropped silently.
cur.execute("""
  SELECT table_name FROM information_schema.tables
  WHERE table_schema = %s ORDER BY table_name""", (SCHEMA,))
existing = [r[0] for r in cur.fetchall()]
print("tables already in schema:", existing or "(none)")
for t in existing:
    cur.execute(f"SELECT count(*) FROM {SCHEMA}.{t}")
    print(f"   {t:<18} {cur.fetchone()[0]} row(s)")

for stale in ("guard_decisions", "fix_queue"):
    if stale in existing:
        cur.execute(f"DROP TABLE {SCHEMA}.{stale}")
        print(f"dropped stale table {stale}")

cur.execute(f"""
CREATE TABLE {SCHEMA}.guard_decisions (
  id                bigserial PRIMARY KEY,
  decided_at        timestamptz NOT NULL DEFAULT now(),
  run_id            text,
  agent_id          text        NOT NULL,
  tool_server       text,
  tool_name         text        NOT NULL,
  turn_index        int,
  action            text        NOT NULL,
  failure_prob      numeric(6,4),
  mismatch_class    text,
  mismatched_param  text,
  guard_latency_ms  int,
  coercion_applied  boolean     NOT NULL DEFAULT false,
  args_before       jsonb,
  args_after        jsonb,
  tool_ok           boolean,
  guard_error       text
)""")
for ddl in [
  f"CREATE INDEX IF NOT EXISTS gd_time_idx  ON {SCHEMA}.guard_decisions (decided_at DESC)",
  f"CREATE INDEX IF NOT EXISTS gd_run_idx   ON {SCHEMA}.guard_decisions (run_id)",
  f"CREATE INDEX IF NOT EXISTS gd_tool_idx  ON {SCHEMA}.guard_decisions (tool_name, action)",
]:
    cur.execute(ddl)
print("table + 3 indexes ready")

# COMMAND ----------

# MAGIC %md ## 4. Grant the app's service principal write access
# MAGIC The schema is owned by the human who created it. The app runs as its own service
# MAGIC principal, which holds CAN_CONNECT_AND_CREATE on the database but no rights on a
# MAGIC schema it did not create, so the owner grants explicitly.

# COMMAND ----------

for s in [
  f'GRANT USAGE ON SCHEMA {SCHEMA} TO "{APP_SP}"',
  f'GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.guard_decisions TO "{APP_SP}"',
  f'GRANT USAGE, SELECT ON SEQUENCE {SCHEMA}.guard_decisions_id_seq TO "{APP_SP}"',
]:
    cur.execute(s)
    print("OK  ", s)

# COMMAND ----------

cur.execute("""
  SELECT table_name, grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privs
  FROM information_schema.role_table_grants
  WHERE table_schema = %s AND grantee = %s
  GROUP BY table_name, grantee""", (SCHEMA, APP_SP))
for t, g, p in cur.fetchall():
    print(f"{t:<18} {g}  ->  {p}")

# COMMAND ----------

# MAGIC %md ## 5. Prove the two-phase write works
# MAGIC The verdict is known before the tool runs; the outcome only afterwards. So the caller
# MAGIC inserts the decision, keeps the id, and updates the same row once the tool returns.

# COMMAND ----------

cur.execute(f"""
  INSERT INTO {SCHEMA}.guard_decisions
    (run_id, agent_id, tool_server, tool_name, turn_index, action, failure_prob,
     mismatch_class, mismatched_param, guard_latency_ms, coercion_applied,
     args_before, args_after)
  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
  RETURNING id""",
  ("setup-selftest", "delivery-routing-agent", "delivery-tracking-api", "list_deliveries",
   1, "coerce", 0.8919, "token_version", "page_token", 337, True,
   '{"courier_id":"CR-4172","page_token":"v2:eyJvZmZzZXQiOjI1fQ=="}',
   '{"courier_id":"CR-4172","page_token":"v1-eyJvZmZzZXQiOjI1"}'))
rid = cur.fetchone()[0]
print("phase 1, inserted decision id:", rid)

cur.execute(f"UPDATE {SCHEMA}.guard_decisions SET tool_ok = %s WHERE id = %s", (True, rid))
print("phase 2, outcome recorded")

cur.execute(f"""SELECT id, action, failure_prob, coercion_applied, tool_ok,
                       args_before->>'page_token' AS sent,
                       args_after->>'page_token'  AS actually_sent
                FROM {SCHEMA}.guard_decisions WHERE id = %s""", (rid,))
row = cur.fetchone()
print("\nread back:")
for k, v in zip(["id","action","failure_prob","coercion_applied","tool_ok",
                 "token the agent proposed","token actually sent"], row):
    print(f"  {k:<26} {v}")

cur.execute(f"DELETE FROM {SCHEMA}.guard_decisions WHERE run_id = 'setup-selftest'")
print("\nself-test row removed; table is empty and ready for the live app")
cur.close(); conn.close()
