# Databricks notebook source
# MAGIC %md
# MAGIC # 11 — Lakebase operational serving: the fix queue
# MAGIC
# MAGIC The analytics layer (Delta gold) answers *"how much are we wasting?"*. That is a scan.
# MAGIC An app that a tool-server owner opens every morning asks a different question —
# MAGIC *"what should **my team** fix first?"* — and that is a point lookup, by owner, at
# MAGIC interactive latency. Wrong shape for a warehouse, right shape for Postgres.
# MAGIC
# MAGIC So the ranked fix queue is pushed from `main.tiffin_agent_waste.fix_queue` (Delta)
# MAGIC into Lakebase Postgres, and the Databricks App reads it from there.
# MAGIC
# MAGIC We also create `guard_decisions`, the write path: every verdict the live guard returns
# MAGIC is appended here as an operational audit trail.

# COMMAND ----------

import json, requests, pandas as pd

PROJECT   = "tiffin-preflight"
ENDPOINT  = f"projects/{PROJECT}/branches/production/endpoints/primary"
PGHOST    = "ep-delicate-grass-y1r5bmqd.database.us-west-2.staging.cloud.databricks.com"
PGDB      = "databricks_postgres"
SCHEMA    = "tiffin"

ctx   = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST  = ctx.apiUrl().get()
TOKEN = ctx.apiToken().get()
PGUSER = spark.sql("SELECT current_user()").collect()[0][0]
print("workspace :", HOST)
print("pg user   :", PGUSER)
print("pg host   :", PGHOST)

# COMMAND ----------

# MAGIC %md ## 1. Mint a short-lived Lakebase OAuth credential

# COMMAND ----------

r = requests.post(f"{HOST}/api/2.0/postgres/credentials",
                  headers={"Authorization": f"Bearer {TOKEN}"},
                  json={"endpoint": ENDPOINT}, timeout=60)
r.raise_for_status()
cred = r.json()
PGPASS = cred["token"]
print("credential acquired, expires:", cred["expire_time"])
print("token length:", len(PGPASS))

# COMMAND ----------

# MAGIC %md ## 2. Connect to Lakebase Postgres

# COMMAND ----------

try:
    import psycopg2
    from psycopg2.extras import execute_values
    drv = "psycopg2"
except ImportError:
    import psycopg as psycopg2            # psycopg 3 fallback
    from psycopg.rows import tuple_row
    drv = "psycopg3"
print("driver:", drv)

conn = psycopg2.connect(host=PGHOST, dbname=PGDB, user=PGUSER,
                        password=PGPASS, sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT version()")
print("connected ->", cur.fetchone()[0][:80])

# COMMAND ----------

# MAGIC %md ## 3. Schema + tables
# MAGIC `fix_queue` is the read path (pushed from Delta). `guard_decisions` is the write path
# MAGIC (appended live by the agent at dispatch time).

# COMMAND ----------

cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
cur.execute(f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.fix_queue (
  fix_rank                  int,
  tool_server               text NOT NULL,
  tool_name                 text NOT NULL,
  recovery_signature        text NOT NULL,
  owner_team                text,
  waste_usd_per_year        numeric(14,2),
  waste_usd_90d             numeric(14,2),
  failed_calls              bigint,
  total_calls               bigint,
  failure_rate_pct          numeric(6,2),
  waste_usd_per_failed_call numeric(12,4),
  waste_tokens_90d          bigint,
  worst_burst               bigint,
  avg_arg_shapes_tried      numeric(6,2),
  doc_quality               text,
  has_input_coercion        boolean,
  refreshed_at              timestamptz,
  PRIMARY KEY (tool_server, tool_name, recovery_signature)
)""")
cur.execute(f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.guard_decisions (
  id             bigserial PRIMARY KEY,
  decided_at     timestamptz NOT NULL DEFAULT now(),
  agent_id       text,
  tool_server    text,
  tool_name      text,
  turn_index     int,
  action         text,
  failure_prob   numeric(6,4),
  mismatch_class text,
  mismatched_param text,
  args_before    jsonb,
  args_after     jsonb
)""")
cur.execute(f"CREATE INDEX IF NOT EXISTS fix_queue_owner_idx ON {SCHEMA}.fix_queue (owner_team, fix_rank)")
cur.execute(f"CREATE INDEX IF NOT EXISTS guard_dec_time_idx  ON {SCHEMA}.guard_decisions (decided_at DESC)")
print("schema + 2 tables + 2 indexes ready")

# COMMAND ----------

# MAGIC %md ## 4. Push the Delta fix queue into Postgres

# COMMAND ----------

pdf = spark.table("main.tiffin_agent_waste.fix_queue").toPandas()
print(f"read {len(pdf)} rows from Delta gold")

cols = ["fix_rank","tool_server","tool_name","recovery_signature","owner_team",
        "waste_usd_per_year","waste_usd_90d","failed_calls","total_calls",
        "failure_rate_pct","waste_usd_per_failed_call","waste_tokens_90d",
        "worst_burst","avg_arg_shapes_tried","doc_quality","has_input_coercion",
        "refreshed_at"]
rows = [tuple(None if pd.isna(v) else v for v in rec) for rec in pdf[cols].itertuples(index=False)]

cur.execute(f"TRUNCATE {SCHEMA}.fix_queue")
ph = "(" + ",".join(["%s"]*len(cols)) + ")"
if drv == "psycopg2":
    execute_values(cur, f"INSERT INTO {SCHEMA}.fix_queue ({','.join(cols)}) VALUES %s", rows)
else:
    cur.executemany(f"INSERT INTO {SCHEMA}.fix_queue ({','.join(cols)}) VALUES {ph}", rows)
print(f"inserted {len(rows)} rows into Lakebase")

# COMMAND ----------

# MAGIC %md ## 5. Read it back out of Postgres — this is what the app will run

# COMMAND ----------

cur.execute(f"""
  SELECT fix_rank, owner_team, tool_server || '.' || tool_name AS tool,
         waste_usd_per_year, failed_calls, failure_rate_pct, waste_usd_per_failed_call
  FROM {SCHEMA}.fix_queue ORDER BY fix_rank""")
hdr = ["rank","owner_team","tool","waste_usd_yr","failed","fail_%","$/failed_call"]
print(" | ".join(f"{h:>17}" for h in hdr))
print("-"*130)
for row in cur.fetchall():
    print(" | ".join(f"{str(v):>17}" for v in row))

# COMMAND ----------

# MAGIC %md ## 6. The point lookup the app actually makes
# MAGIC One owner team opening their queue. This is the access pattern that justifies Postgres.

# COMMAND ----------

cur.execute(f"""
  SELECT fix_rank, tool_name, waste_usd_per_year, waste_usd_per_failed_call
  FROM {SCHEMA}.fix_queue WHERE owner_team = %s ORDER BY fix_rank""",
  ("logistics-platform",))
print("owner_team = logistics-platform")
for row in cur.fetchall():
    print("  rank", row[0], "|", row[1], "| $", row[2], "/yr | $", row[3], "per failed call")

cur.execute(f"SELECT count(*), round(sum(waste_usd_per_year),2) FROM {SCHEMA}.fix_queue")
n, tot = cur.fetchone()
print(f"\ntotal: {n} queue entries, ${tot}/yr of addressable waste")

cur.execute(f"""SELECT max(waste_usd_per_failed_call)/min(waste_usd_per_failed_call)
                FROM {SCHEMA}.fix_queue WHERE waste_usd_per_failed_call > 0""")
print(f"waste per failed call varies {float(cur.fetchone()[0]):.0f}x across tools "
      f"-- which is why the queue is ranked by money, not by error count")

# COMMAND ----------

cur.execute(f"SELECT count(*) FROM {SCHEMA}.guard_decisions")
print("guard_decisions rows (populated live by the app):", cur.fetchone()[0])
cur.close(); conn.close()
print("\nLakebase operational serving ready.")
