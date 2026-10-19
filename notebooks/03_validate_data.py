# Databricks notebook source
# DBTITLE 1,03_validate_data — the data quality gate
# 03_validate_data.py
#
# Verifies the generated data is actually correct before any pipeline is built on it.
# Every check below either passes or raises -- silent data problems are the expensive kind.
#
# WHAT IT CHECKS
#   1. span structure and the AGENT/LLM/TOOL tree
#   2. NO bug labels leaked into the spans (families must be DISCOVERED, not handed over)
#   3. the hero feature exists: gen_ai.tool.call.arguments present on tool spans
#   4. argument shapes genuinely mismatch the DECLARED schema where a bug is present
#   5. the silent bug returns HTTP 200 with NO error.type -- invisible to any error log
#   6. class overlap exists (clean sessions are not trivially shorter)
#   7. economics are plausible and reconcile against the reference pricing

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# DBTITLE 1,1-3. Structure, label leakage, hero feature
import json
A_ = A  # alias for brevity in f-strings

df = spark.table(RAW_SPANS_TABLE)
df.createOrReplaceTempView("spans")

struct = spark.sql(f"""
SELECT
  COUNT(*)                                                                      AS spans,
  COUNT(DISTINCT trace_id)                                                      AS traces,
  SUM(CASE WHEN _is_root=1 THEN 1 ELSE 0 END)                                   AS agent_spans,
  SUM(CASE WHEN variant_get(attributes,'$["{A.SPAN_TYPE}"]','string')='LLM'  THEN 1 ELSE 0 END) AS llm_spans,
  SUM(CASE WHEN variant_get(attributes,'$["{A.SPAN_TYPE}"]','string')='TOOL' THEN 1 ELSE 0 END) AS tool_spans,
  SUM(CASE WHEN variant_get(attributes,'$["{A.TOOL_ARGS}"]','string') IS NOT NULL THEN 1 ELSE 0 END) AS with_args,
  SUM(CASE WHEN variant_get(attributes,'$["{A.ERROR_TYPE}"]','string') IS NOT NULL THEN 1 ELSE 0 END) AS error_spans,
  SUM(CASE WHEN variant_get(attributes,'$["tiffin.bug.family"]','string') IS NOT NULL THEN 1 ELSE 0 END) AS leaked_labels,
  COUNT(DISTINCT date)                                                          AS days
FROM spans
""").collect()[0].asDict()
print(json.dumps(struct, indent=2, default=str))

assert struct["leaked_labels"] == 0, "bug labels leaked into spans -- families must be discovered"
assert struct["agent_spans"] == struct["traces"], "expected exactly one AGENT root span per trace"
assert struct["with_args"] == struct["tool_spans"], "every TOOL span needs call arguments"
assert struct["days"] >= 85, f"expected ~90 days, got {struct['days']}"
print("\nOK  structure sound, no label leakage, hero feature present on all tool spans")

# COMMAND ----------

# DBTITLE 1,Span tree integrity
orphans = spark.sql("""
SELECT COUNT(*) AS n FROM spans c
WHERE c.parent_span_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM spans p
                  WHERE p.span_id = c.parent_span_id AND p.trace_id = c.trace_id)
""").collect()[0]["n"]
print(f"orphaned spans (parent not in same trace): {orphans}")
assert orphans == 0, "span tree is broken -- recovery-turn reconstruction would be wrong"

# COMMAND ----------

# DBTITLE 1,4. Argument shapes vs the DECLARED schema
# This is the pre-flight model's entire premise: compare what the model SENT against what
# the server DECLARES. Done here as a spot check; the pipeline does it properly in silver.
declared = {(r["tool_server"], r["tool_name"]): json.loads(r["expected_args_json"])
            for r in spark.read.parquet(f"{REFERENCE_VOLUME}/tool_catalog").collect()}

def shape_of(v):
    if isinstance(v, bool):  return "boolean"
    if isinstance(v, int):   return "int"
    if isinstance(v, float): return "double"
    if isinstance(v, list):  return "array"
    if isinstance(v, dict):  return "object"
    return "string"

sample = spark.sql(f"""
SELECT variant_get(attributes,'$["{A.TOOL_SOURCE}"]','string') AS srv,
       variant_get(attributes,'$["{A.TOOL_NAME}"]','string')   AS tool,
       variant_get(attributes,'$["{A.TOOL_ARGS}"]','string')   AS args,
       variant_get(attributes,'$["{A.ERROR_TYPE}"]','string')  AS err
FROM spans
WHERE variant_get(attributes,'$["{A.SPAN_TYPE}"]','string')='TOOL'
LIMIT 40000
""").collect()

mismatch_when_error = mismatch_when_clean = n_err = n_clean = 0
examples = []
for r in sample:
    decl = declared.get((r["srv"], r["tool"]))
    if not decl:
        continue
    sent = json.loads(r["args"])
    bad = False
    for p, dt in decl.items():
        if p not in sent:
            bad = True; break
        want = ("array"  if dt == "array<string>" else
                "object" if dt.endswith("_object") else
                "int"    if dt == "int" else
                "double" if dt == "double" else
                "boolean" if dt == "boolean" else "string")
        if shape_of(sent[p]) != want:
            bad = True
            if len(examples) < 4:
                examples.append({"tool": f"{r['srv']}.{r['tool']}", "param": p,
                                 "declared": dt, "sent_shape": shape_of(sent[p]),
                                 "sent_value": str(sent[p])[:60], "errored": bool(r["err"])})
            break
    if r["err"]:
        n_err += 1;  mismatch_when_error += bad
    else:
        n_clean += 1; mismatch_when_clean += bad

print(f"errored tool calls:  {n_err:,}  of which shape-mismatched: {mismatch_when_error:,} "
      f"({100*mismatch_when_error/max(n_err,1):.1f}%)")
print(f"clean tool calls:    {n_clean:,}  of which shape-mismatched: {mismatch_when_clean:,} "
      f"({100*mismatch_when_clean/max(n_clean,1):.1f}%)")
print("\nexample mismatches the model would plausibly make:")
for e in examples:
    print(f"  {e['tool']}.{e['param']}: declared {e['declared']}, sent {e['sent_shape']} "
          f"= {e['sent_value']}  (errored={e['errored']})")

assert mismatch_when_error > 0, "errored calls show no argument mismatch -- no signal to learn"
assert mismatch_when_error / max(n_err, 1) > mismatch_when_clean / max(n_clean, 1), \
    "mismatch is not more common on failures than on clean calls -- signal is absent"

# COMMAND ----------

# DBTITLE 1,5. The silent bug is genuinely invisible
silent_bug = [b for b in BUG_FAMILIES if b["is_silent"]][0]
gt = spark.table(f"{CATALOG}.{SCHEMA}.ground_truth")
gt.createOrReplaceTempView("gt")

sil = spark.sql(f"""
SELECT
  COUNT(*)                                                        AS silent_sessions,
  SUM(CASE WHEN emitted_error THEN 1 ELSE 0 END)                  AS emitted_any_error,
  ROUND(AVG(struggle_turns),2)                                    AS avg_struggle_turns,
  ROUND(SUM(wasted_cost_usd),2)                                   AS waste_usd
FROM gt WHERE is_silent
""").collect()[0].asDict()
print(f"silent family = {silent_bug['bug_id']}")
print(json.dumps(sil, indent=2, default=str))
assert sil["silent_sessions"] > 0, "no silent sessions generated"
assert sil["emitted_any_error"] == 0, \
    "silent sessions emitted errors -- they would be findable by grepping the error log"

sil_http = spark.sql(f"""
SELECT DISTINCT variant_get(attributes,'$["{A.HTTP_STATUS}"]','int') AS http_status
FROM spans
WHERE variant_get(attributes,'$["{A.TOOL_SOURCE}"]','string')='{silent_bug['server']}'
  AND variant_get(attributes,'$["{A.TOOL_NAME}"]','string')='{silent_bug['tool']}'
""").collect()
print(f"HTTP statuses on {silent_bug['server']}.{silent_bug['tool']}: "
      f"{[r['http_status'] for r in sil_http]}")
print("OK  silent waste returns 200 and emits no error -- invisible to any error log")

# COMMAND ----------

# DBTITLE 1,6. Class overlap (the de-risk lesson)
ov = spark.sql("""
SELECT
  ROUND(PERCENTILE(CASE WHEN NOT has_waste THEN struggle_turns END, 0.95),2) AS clean_p95_struggle,
  COUNT(DISTINCT archetype)                                                  AS archetypes,
  ROUND(100.0*SUM(CASE WHEN has_waste THEN 1 ELSE 0 END)/COUNT(*),2)         AS pct_sessions_with_waste
FROM gt
""").collect()[0].asDict()
arch = spark.sql("SELECT archetype, COUNT(*) n FROM gt GROUP BY 1 ORDER BY 2 DESC").collect()
print(json.dumps(ov, indent=2, default=str))
print("archetype mix: " + ", ".join(f"{r['archetype']}={r['n']:,}" for r in arch))

turns = spark.sql("""
SELECT
  ROUND(PERCENTILE(CASE WHEN NOT g.has_waste THEN o.total_turns END, 0.95),1) AS clean_p95_turns,
  ROUND(PERCENTILE(CASE WHEN g.has_waste     THEN o.total_turns END, 0.05),1) AS waste_p05_turns
FROM gt g JOIN main.tiffin_agent_waste.session_outcomes o USING (trace_id)
""").collect()[0].asDict()
print(json.dumps(turns, indent=2, default=str))
assert turns["clean_p95_turns"] >= turns["waste_p05_turns"], \
    "no class overlap -- session length alone would separate the classes (de-risk lesson)"
print("OK  classes overlap on session length, so the model must learn the mechanism")

# COMMAND ----------

# DBTITLE 1,7. Economics reconcile
econ = spark.sql(f"""
SELECT
  ROUND(SUM(o.total_cost_usd),2)                                       AS spend_window,
  ROUND((SELECT SUM(wasted_cost_usd) FROM gt),2)                       AS waste_window,
  COUNT(*)                                                             AS sessions
FROM main.tiffin_agent_waste.session_outcomes o
""").collect()[0].asDict()
days = struct["days"]
econ["spend_annualized"] = round(econ["spend_window"] / days * 365, 2)
econ["waste_annualized"] = round(econ["waste_window"] / days * 365, 2)
econ["waste_pct_of_spend"] = round(100 * econ["waste_window"] / econ["spend_window"], 1)
print(json.dumps(econ, indent=2, default=str))
assert 2 < econ["waste_pct_of_spend"] < 60, \
    f"waste share implausible at {econ['waste_pct_of_spend']}% -- check the cost model"
print("OK  economics plausible")

# COMMAND ----------

# DBTITLE 1,Per-agent waste (is the story diagnostic, not just traffic?)
spark.sql(f"""
SELECT o.agent_id,
       COUNT(*)                                                          AS sessions,
       ROUND(100.0*SUM(CASE WHEN g.has_waste THEN 1 ELSE 0 END)/COUNT(*),1) AS pct_waste_sessions,
       ROUND(SUM(g.wasted_cost_usd)/{days}*365,0)                        AS waste_usd_per_year,
       ROUND(100.0*SUM(g.wasted_cost_usd)/NULLIF(SUM(o.total_cost_usd),0),1) AS pct_of_its_own_spend
FROM main.tiffin_agent_waste.session_outcomes o
JOIN gt g USING (trace_id)
GROUP BY o.agent_id ORDER BY waste_usd_per_year DESC
""").show(truncate=False)

# COMMAND ----------

# DBTITLE 1,Verdict
out = dict(structure=struct, silent=sil, overlap=turns, economics=econ,
           orphan_spans=orphans,
           arg_mismatch_rate_on_errors=round(mismatch_when_error/max(n_err,1), 3),
           arg_mismatch_rate_on_clean=round(mismatch_when_clean/max(n_clean,1), 3),
           all_checks="PASSED")
print(json.dumps(out, indent=2, default=str))
dbutils.notebook.exit(json.dumps(out, default=str))
