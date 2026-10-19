# Databricks notebook source
# MAGIC %md
# MAGIC # 12 — Grant the app's service principal access to Lakebase
# MAGIC
# MAGIC The `tiffin` schema is owned by the human who created it (me). The Databricks App runs as
# MAGIC its own service principal, which holds `CAN_CONNECT_AND_CREATE` on the database but has no
# MAGIC rights on a schema it did not create. So the owner grants explicitly:
# MAGIC `SELECT` on the read path, `INSERT` on the decision log.

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

r = requests.post(f"{HOST}/api/2.0/postgres/credentials",
                  headers={"Authorization": f"Bearer {TOKEN}"},
                  json={"endpoint": ENDPOINT}, timeout=60)
r.raise_for_status()
PGPASS = r.json()["token"]

import psycopg2
conn = psycopg2.connect(host=PGHOST, dbname=PGDB, user=PGUSER,
                        password=PGPASS, sslmode="require")
conn.autocommit = True
cur = conn.cursor()
print("connected as", PGUSER)

# COMMAND ----------

stmts = [
  f'GRANT USAGE ON SCHEMA {SCHEMA} TO "{APP_SP}"',
  f'GRANT SELECT ON {SCHEMA}.fix_queue TO "{APP_SP}"',
  f'GRANT SELECT, INSERT ON {SCHEMA}.guard_decisions TO "{APP_SP}"',
  f'GRANT USAGE, SELECT ON SEQUENCE {SCHEMA}.guard_decisions_id_seq TO "{APP_SP}"',
  f'ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO "{APP_SP}"',
]
for s in stmts:
    cur.execute(s)
    print("OK  ", s)

# COMMAND ----------

# MAGIC %md ## Verify the grants actually landed

# COMMAND ----------

cur.execute("""
  SELECT table_name, grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privs
  FROM information_schema.role_table_grants
  WHERE table_schema = %s AND grantee = %s
  GROUP BY table_name, grantee ORDER BY table_name""", (SCHEMA, APP_SP))
rows = cur.fetchall()
print(f"{'table':<20} {'grantee':<40} privileges")
print("-"*95)
for t, g, p in rows:
    print(f"{t:<20} {g:<40} {p}")
print(f"\n{len(rows)} table grant(s) in place for the app service principal")
cur.close(); conn.close()
