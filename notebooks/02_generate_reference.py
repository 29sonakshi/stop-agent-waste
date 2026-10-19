# Databricks notebook source
# DBTITLE 1,02_generate_reference: the dimension exports
# 02_generate_reference.py
#
# WHAT THIS IS
#   Not everything comes from the Gateway. These six datasets represent exports from
#   systems that really do hand you files, which is why bronze ingests them with
#   read_files rather than streaming off a Delta table:
#
#     tool_catalog         <- MCP `tools/list` introspection dump (the DECLARED schema)
#     agent_catalog        <- Unity Catalog metadata + service-catalog tags
#     bug_registry         <- Jira export: human-readable text for families already triaged
#     model_pricing        <- system.billing.list_prices joined to the negotiated rate card
#     agent_tool_affinity  <- the generator's own call-distribution config, made queryable
#     deploy_events        <- CI/CD + git history export
#
#   tool_catalog is the important one: `expected_args` is what the server DECLARES.
#   The pipeline compares arguments actually sent against it, so the coercion suggestion
#   is derived from the server's own schema rather than guessed.
#
#   NOTE: bug_registry carries NO dollar figure. Cost is computed by the pipeline from
#   trace data, so "where does that number come from?" has a real answer.
#
# ALL DATA IS SYNTHETIC.

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# DBTITLE 1,Build the six dimension frames
import json, random, uuid
from datetime import timedelta
import pandas as pd

random.seed(SEED)

# ---- 1. tool_catalog: tool grain, server properties joined in -------------------------
tool_rows = []
for server, tool, desc, expected_args, sla in TOOLS:
    s = SERVER_BY_NAME[server]
    tool_rows.append(dict(
        tool_server=server, tool_name=tool, description=desc,
        expected_args_json=json.dumps(expected_args),   # the DECLARED MCP inputSchema
        n_params=len(expected_args),
        sla_p99_ms=sla,
        owner_team=s["owner_team"],
        doc_quality=s["doc_quality"],
        has_examples=s["has_examples"],
        has_input_coercion=s["has_input_coercion"],
        server_version=s["version"],
        server_released=s["released"],
    ))
tool_catalog = pd.DataFrame(tool_rows)

# ---- 2. agent_catalog -----------------------------------------------------------------
agent_rows = []
for a in AGENTS:
    servers = sorted({s for _a, s, _t, _w in AGENT_TOOL_AFFINITY if _a == a["agent_id"]})
    agent_rows.append(dict(
        agent_id=a["agent_id"], display_name=a["display"],
        model=a["model"], endpoint_name=a["endpoint"],
        owner_team=a["owner_team"], domain=a["domain"],
        tool_servers=",".join(servers),
        avg_daily_sessions=a["avg_daily_sessions"],
        monthly_budget_usd=a["budget_usd"],
        launched_date=a["launched"], agent_version=a["version"],
        is_live_demo_agent=(a["agent_id"] == LIVE_AGENT),
    ))
agent_catalog = pd.DataFrame(agent_rows)

# ---- 3. bug_registry: descriptive only, no costs --------------------------------------
bug_catalog = pd.DataFrame([dict(
    bug_id=b["bug_id"], bug_name=b["name"],
    tool_server=b["server"], tool_name=b["tool"], affected_param=b["param"],
    mismatch_type=b["mismatch_type"], is_silent=b["is_silent"],
    agent_ids=",".join(b["agent_ids"]),
    introduced_date=b["introduced"], fixed_date=b["fixed"],
    frequency_pct=b["frequency_pct"],
    suggested_coercion=b["suggested_coercion"],
) for b in BUG_FAMILIES])

# ---- 4. model_pricing -----------------------------------------------------------------
pricing = pd.DataFrame([dict(
    model_id=m["model_id"], endpoint_name=m["endpoint"], provider=m["provider"],
    input_cost_per_1k=m["in_per_1k"], output_cost_per_1k=m["out_per_1k"],
    context_window=m["context"], effective_date=PRICING_EFFECTIVE,
) for m in MODEL_PRICING])

# ---- 5. agent_tool_affinity -----------------------------------------------------------
affinity = pd.DataFrame([dict(agent_id=a, tool_server=s, tool_name=t, call_weight=w)
                         for a, s, t, w in AGENT_TOOL_AFFINITY])

# ---- 6. deploy_events: CI/CD history --------------------------------------------------
# Every bug is traceable to the deploy that introduced it, which is what lets the
# dashboard annotate "waste on this server jumped the day v2.4.0 shipped".
ENGINEERS = ["j.chen@tiffin.example", "p.okafor@tiffin.example", "m.silva@tiffin.example",
             "a.novak@tiffin.example", "r.iyer@tiffin.example", "t.bergman@tiffin.example"]

def _bump(v, kind):
    maj, mnr, pat = (int(x) for x in v.split("."))
    return f"{maj}.{mnr+1}.0" if kind == "release" else f"{maj}.{mnr}.{pat+1}"

deploys = []
bug_by_server_date = {(b["server"], b["introduced"]): b for b in BUG_FAMILIES}

for s in TOOL_SERVERS:
    version = s["version"]
    d = WINDOW_START
    while d <= WINDOW_END:
        d += timedelta(days=random.randint(4, 12))
        if d > WINDOW_END:
            break
        kind = random.choices(["release", "hotfix", "config_change", "rollback"],
                              weights=[0.45, 0.30, 0.20, 0.05])[0]
        prev, version = version, _bump(version, kind)
        deploys.append(dict(
            deploy_id=f"dep-{uuid.uuid4().hex[:10]}",
            tool_server=s["server"], deploy_date=d,
            version_from=prev, version_to=version, deploy_type=kind,
            deployed_by=random.choice(ENGINEERS),
            introduced_bug=None, fixed_bug=None,
            changelog=random.choice([
                "Dependency bumps and logging cleanup",
                "Tighten request validation",
                "Performance: reduce p99 on list endpoints",
                "Refactor serialization layer",
                "Add retry on upstream timeout",
                "Update response schema",
            ]),
        ))

# Pin each bug to a deliberate deploy on its introduction date -- the smoking gun.
for b in BUG_FAMILIES:
    s = SERVER_BY_NAME[b["server"]]
    deploys.append(dict(
        deploy_id=f"dep-{uuid.uuid4().hex[:10]}",
        tool_server=b["server"], deploy_date=b["introduced"],
        version_from=_bump(s["version"], "hotfix"), version_to=s["version"],
        deploy_type="release", deployed_by=random.choice(ENGINEERS),
        introduced_bug=b["bug_id"], fixed_bug=None,
        changelog=f"Update {b['tool']} request handling ({b['param']})",
    ))

deploy_events = pd.DataFrame(deploys).sort_values(["deploy_date", "tool_server"])

for name, df in [("tool_catalog", tool_catalog), ("agent_catalog", agent_catalog),
                 ("bug_registry", bug_catalog), ("model_pricing", pricing),
                 ("agent_tool_affinity", affinity), ("deploy_events", deploy_events)]:
    print(f"  {name:22} {len(df):>4} rows   {list(df.columns)[:4]}...")

# COMMAND ----------

# DBTITLE 1,Write Parquet to the reference Volume
# One directory per dataset so bronze can read_files each with a stable glob.
#
# Columns that are entirely NULL (e.g. fixed_date while no bug has been fixed yet) are
# inferred by Spark as VOID, which Parquet cannot write. Cast them to the type they are
# MEANT to be so the schema stays correct and useful once values do arrive.
from pyspark.sql import types as T

NULL_COL_TYPES = {
    "bug_registry":  {"fixed_date": "date"},
    "deploy_events": {"introduced_bug": "string", "fixed_bug": "string"},
}


def _write(name, pdf):
    sdf = spark.createDataFrame(pdf)
    overrides = NULL_COL_TYPES.get(name, {})
    exprs = []
    for f in sdf.schema.fields:
        if isinstance(f.dataType, T.NullType):
            t = overrides.get(f.name, "string")
            exprs.append(f"CAST(NULL AS {t}) AS `{f.name}`")
        else:
            exprs.append(f"`{f.name}`")
    sdf = sdf.selectExpr(*exprs)
    path = f"{REFERENCE_VOLUME}/{name}"
    sdf.coalesce(1).write.mode("overwrite").parquet(path)
    print(f"  wrote {len(pdf):>4} rows -> {path}")
    return len(pdf)


written = {}
for name, pdf in [("tool_catalog", tool_catalog), ("agent_catalog", agent_catalog),
                  ("bug_registry", bug_catalog), ("model_pricing", pricing),
                  ("agent_tool_affinity", affinity), ("deploy_events", deploy_events)]:
    written[name] = _write(name, pdf)

# COMMAND ----------

# DBTITLE 1,Verify readable + the declared-schema spot check
import json as _json
checks = {}
for name in written:
    checks[name] = spark.read.parquet(f"{REFERENCE_VOLUME}/{name}").count()

# The feature the whole coercion mechanism depends on: a tool's DECLARED argument schema.
probe = (spark.read.parquet(f"{REFERENCE_VOLUME}/tool_catalog")
              .where("tool_server='merchant-catalog-api' AND tool_name='get_merchant'")
              .collect()[0])
print(f"declared schema for merchant-catalog-api.get_merchant: {probe['expected_args_json']}")
print(f"  doc_quality={probe['doc_quality']}  coercion={probe['has_input_coercion']}")

out = dict(rows_written=written, rows_read_back=checks,
           reference_volume=REFERENCE_VOLUME,
           bugs_with_introducing_deploy=int(
               deploy_events["introduced_bug"].notna().sum()))
print(_json.dumps(out, indent=2))
dbutils.notebook.exit(_json.dumps(out, default=str))
