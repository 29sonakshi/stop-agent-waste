# Databricks notebook source
# MAGIC %md
# MAGIC # 13: Verify Lakebase state

# COMMAND ----------

import requests, psycopg2
ENDPOINT="projects/tiffin-preflight/branches/production/endpoints/primary"
PGHOST="ep-delicate-grass-y1r5bmqd.database.us-west-2.staging.cloud.databricks.com"
ctx=dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST=ctx.apiUrl().get(); TOKEN=ctx.apiToken().get()
PGUSER=spark.sql("SELECT current_user()").collect()[0][0]
tok=requests.post(f"{HOST}/api/2.0/postgres/credentials",
    headers={"Authorization":f"Bearer {TOKEN}"},json={"endpoint":ENDPOINT},timeout=60).json()["token"]
conn=psycopg2.connect(host=PGHOST,dbname="databricks_postgres",user=PGUSER,password=tok,sslmode="require")
conn.autocommit=True; cur=conn.cursor()

cur.execute("""SELECT table_name,
  (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
   table_schema, table_name), false, true, '')))[1]::text::bigint AS row_count
  FROM information_schema.tables WHERE table_schema='tiffin' ORDER BY table_name""")
print(f"{'table':<20} {'rows':>8}")
print("-"*30)
for t,n in cur.fetchall(): print(f"{t:<20} {n:>8}")

cur.execute("""SELECT indexname FROM pg_indexes WHERE schemaname='tiffin' ORDER BY 1""")
print("\nindexes:", [r[0] for r in cur.fetchall()])

cur.execute("""SELECT table_name, grantee, string_agg(privilege_type,', ' ORDER BY privilege_type)
  FROM information_schema.role_table_grants WHERE table_schema='tiffin'
  AND grantee='097a6d13-a890-42a0-9374-2c0cdf02eeb9' GROUP BY 1,2 ORDER BY 1""")
print("\napp service-principal grants:")
for t,g,p in cur.fetchall(): print(f"  {t:<20} {p}")
cur.close(); conn.close()
