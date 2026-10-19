# Evidence 7: Lakebase operational serving

_Real cell-by-cell output from notebook `11_lakebase_serving`, run as a Databricks job on
e2-dogfood (2026-10-19 21:12 UTC). Lakebase project `projects/tiffin-preflight`, branch `production`,
Postgres schema `tiffin`._

**Why Postgres and not the warehouse.** The gold Delta tables answer *"how much are we
wasting?"*, which is an analytical scan over 1.3M rows. The app answers a different question for
a tool-server owner: *"what should **my team** fix first?"* That is a point lookup by `owner_team` at
interactive latency, run every time someone opens the page. Wrong shape for a warehouse scan,
right shape for an indexed Postgres read. So the ranked queue is pushed from Delta into Lakebase
and the app reads it from there.

Two tables:
- **`tiffin.fix_queue`.** read path, pushed from `main.tiffin_agent_waste.fix_queue` (Delta gold)
- **`tiffin.guard_decisions`.** write path, appended by the live agent at dispatch time


### Cell 1

```python
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
```

**Output**
```
workspace : https://e2-dogfood.staging.cloud.databricks.com
pg user   : sonakshi.pandey@databricks.com
pg host   : ep-delicate-grass-y1r5bmqd.database.us-west-2.staging.cloud.databricks.com
```

### Cell 2

```python
r = requests.post(f"{HOST}/api/2.0/postgres/credentials",
                  headers={"Authorization": f"Bearer {TOKEN}"},
                  json={"endpoint": ENDPOINT}, timeout=60)
r.raise_for_status()
cred = r.json()
PGPASS = cred["token"]
print("credential acquired, expires:", cred["expire_time"])
print("token length:", len(PGPASS))
```

**Output**
```
credential acquired, expires: 2026-10-19T22:10:52Z
token length: 2594
```

### Cell 3

```python
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
```

**Output**
```
driver: psycopg2
connected -> PostgreSQL 17.11 (11592b3) on aarch64-unknown-linux-gnu, compiled by gcc (Ubuntu
```

### Cell 4

```python
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
```

**Output**
```
schema + 2 tables + 2 indexes ready
```

### Cell 5

```python
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
```

**Output**
```
read 9 rows from Delta gold
inserted 9 rows into Lakebase
```

### Cell 6

```python
cur.execute(f"""
  SELECT fix_rank, owner_team, tool_server || '.' || tool_name AS tool,
         waste_usd_per_year, failed_calls, failure_rate_pct, waste_usd_per_failed_call
  FROM {SCHEMA}.fix_queue ORDER BY fix_rank""")
hdr = ["rank","owner_team","tool","waste_usd_yr","failed","fail_%","$/failed_call"]
print(" | ".join(f"{h:>17}" for h in hdr))
print("-"*130)
for row in cur.fetchall():
    print(" | ".join(f"{str(v):>17}" for v in row))
```

**Output**
```
rank |        owner_team |              tool |      waste_usd_yr |            failed |            fail_% |     $/failed_call
----------------------------------------------------------------------------------------------------------------------------------
                1 | logistics-platform | delivery-tracking-api.list_deliveries |          33992.15 |              3844 |             10.94 |            2.1804
                2 | payments-platform | payment-gateway.create_refund |          28693.43 |              3010 |             10.65 |            2.3505
                3 |   orders-platform | order-history-api.list_orders |          25790.13 |              5959 |             17.49 |            1.0672
                4 |     data-platform | analytics-aggregator.batch_query |          10186.26 |              1942 |              1.68 |            1.2933
                5 |   growth-platform | promo-engine.create_promo |           5040.09 |              4454 |             22.57 |            0.2790
                6 | logistics-platform | geolocation-service.distance_matrix |           4226.23 |               955 |              4.23 |            1.0912
                7 | merchant-platform | merchant-catalog-api.get_merchant |            803.26 |              1655 |             10.02 |            0.1197
                8 | merchant-platform | menu-service.upsert_menu_item |            577.89 |              1155 |             10.11 |            0.1234
                9 | merchant-platform | merchant-catalog-api.update_merchant |            109.02 |              1167 |             30.16 |            0.0230
```

### Cell 7

```python
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
```

**Output**
```
owner_team = logistics-platform
  rank 1 | list_deliveries | $ 33992.15 /yr | $ 2.1804 per failed call
  rank 6 | distance_matrix | $ 4226.23 /yr | $ 1.0912 per failed call

total: 9 queue entries, $109418.46/yr of addressable waste
waste per failed call varies 102x across tools -- which is why the queue is ranked by money, not by error count
```

### Cell 8

```python
cur.execute(f"SELECT count(*) FROM {SCHEMA}.guard_decisions")
print("guard_decisions rows (populated live by the app):", cur.fetchone()[0])
cur.close(); conn.close()
print("\nLakebase operational serving ready.")
```

**Output**
```
guard_decisions rows (populated live by the app): 0

Lakebase operational serving ready.
```
