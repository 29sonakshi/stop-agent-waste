# Databricks notebook source
# Tests the SHIPPING agent module end to end against the live endpoints, with the
# Lakebase verdict log attached, then reads the trail back out of Postgres.
#
# This imports the same agent.py the deployed app imports, so what is tested is what
# ships. The only difference is the Postgres identity: this notebook connects as the
# human running it, the app connects as its own service principal.
import os, sys, json

sys.path.insert(0, "/Workspace/Users/sonakshi.pandey@databricks.com/fe-bar-sonakshi/demo_app")

# lakebase.py resolves the Postgres user from the environment. In the app that is the
# service principal; in a notebook it is the human, so set it before importing.
os.environ["PGUSER"] = spark.sql("SELECT current_user()").collect()[0][0]

import agent
import lakebase

results = {}
for guard in (False, True):
    events = []
    # The logger is only attached when the guard is on. With the guard off there are no
    # verdicts to record, which is the point of the comparison.
    try:
        summary = agent.run_agent(agent.DEFAULT_REQUEST, guard_on=guard,
                                  emit=events.append,
                                  logger=lakebase if guard else None)
    except Exception as e:
        results[f"guard_{guard}"] = {"ERROR": f"{type(e).__name__}: {e}"}
        continue
    # compact trace: what the agent proposed, what the guard said, what happened
    trace = []
    for ev in events:
        t = ev["type"]
        if t == "turn":      trace.append(f"--- turn {ev['turn']}")
        elif t == "proposed":trace.append(f"  propose {ev['tool']} {json.dumps(ev['args'])[:110]}")
        elif t == "guard":
            p = ev["pred"]
            trace.append(f"  GUARD {p.get('action')} p={p.get('failure_probability')} cls={p.get('mismatch_class')}")
        elif t == "coerced": trace.append(f"  COERCED {ev['note']}")
        elif t == "guard_error": trace.append(f"  GUARD-ERROR {ev['error'][:80]}")
        elif t == "result":  trace.append(f"  {'ok' if ev['ok'] else 'FAIL'} {json.dumps(ev['result'])[:110]}")
        elif t == "final":   trace.append(f"  FINAL: {ev['text'][:300]}")
    results[f"guard_{guard}"] = {"summary": summary, "trace": trace}

print(json.dumps(results, indent=2, default=str))

# COMMAND ----------

# MAGIC %md ## The verdict log, read back from Lakebase
# MAGIC Every decision the guarded run just made, retrieved by `run_id`. This is a point
# MAGIC lookup on an indexed column, which is the access pattern the app makes on every page
# MAGIC view and the reason this lives in Postgres rather than Delta.

# COMMAND ----------

run_id = (results.get("guard_True", {}).get("summary") or {}).get("run_id")
print("run_id:", run_id)

rows = lakebase.run_summary(run_id) if run_id else []
hdr = ["turn", "tool", "verdict", "p(fail)", "rewrote?", "tool ok?", "before", "after"]
print("\n" + " | ".join(f"{h:>16}" for h in hdr))
print("-" * 145)
for r in rows:
    print(" | ".join(f"{str(v):>16}" for v in [
        r["turn_index"], r["tool_name"], r["action"], r["failure_prob"],
        r["coercion_applied"], r["tool_ok"],
        str(r["before_value"])[:16], str(r["after_value"])[:16]]))
print(f"\n{len(rows)} decision(s) recorded for this run")

# COMMAND ----------

# MAGIC %md ## What this captures that an inference log cannot
# MAGIC The model's own request/response could be captured automatically by Model Serving.
# MAGIC These three columns could not be: whether the wrapper really rewrote the argument,
# MAGIC whether the tool call then succeeded, and any fail-open where no request was sent.

# COMMAND ----------

import psycopg2.extras
conn = lakebase.connect()
with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
    c.execute("""
      SELECT action,
             count(*)                                             AS decisions,
             sum(CASE WHEN coercion_applied THEN 1 ELSE 0 END)     AS args_rewritten,
             sum(CASE WHEN tool_ok THEN 1 ELSE 0 END)              AS tool_succeeded,
             sum(CASE WHEN tool_ok IS FALSE THEN 1 ELSE 0 END)     AS tool_failed,
             sum(CASE WHEN guard_error IS NOT NULL THEN 1 ELSE 0 END) AS fail_opens,
             round(avg(guard_latency_ms))                          AS avg_guard_ms
      FROM tiffin.guard_decisions
      GROUP BY action ORDER BY decisions DESC""")
    agg = c.fetchall()
for row in agg:
    print(dict(row))

with conn.cursor() as c:
    c.execute("SELECT count(*) FROM tiffin.guard_decisions")
    print("\ntotal rows in tiffin.guard_decisions:", c.fetchone()[0])

dbutils.notebook.exit(json.dumps(
    {"ab": results, "verdict_log": rows, "aggregate": [dict(r) for r in agg]},
    default=str)[:14000])
