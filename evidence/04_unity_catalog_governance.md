# Evidence 4 — Unity Catalog governance

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood) — 2026-10-19 21:08 UTC. Every table below is real query output, not illustrative._

One governance plane over raw spans, pipeline tables, the model, and the serving endpoint.

### Table lineage captured by UC (pipeline provenance)

```sql
SELECT source_table_full_name, target_table_full_name, count(*) AS events
FROM system.access.table_lineage
WHERE target_table_schema_name = 'tiffin_agent_waste'
  AND source_table_full_name IS NOT NULL
  AND event_date >= current_date() - INTERVAL 30 DAYS
GROUP BY 1,2 ORDER BY 1,2 LIMIT 30
```

**QUERY FAILED:** `{"error_code": "BAD_REQUEST", "message": "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter with name `target_table_schema_name` cannot be resolved. Did you mean one of the following? [`target_table_schema`, `target_table_full_name`, `target_table_name`, `target_table_cat`

### Billing attribution — what this solution costs to run

Cost is itself governed and queryable — the same system tables that prove the pipeline ran also price it.

```sql
SELECT billing_origin_product, sku_name,
       round(sum(usage_quantity),2) AS dbus,
       count(DISTINCT usage_date) AS days
FROM system.billing.usage
WHERE usage_date >= current_date() - INTERVAL 30 DAYS
  AND (usage_metadata.endpoint_name = 'tiffin-tool-call-risk'
    OR usage_metadata.app_name LIKE '%tiffin%'
    OR usage_metadata.dlt_pipeline_id = '9453c79d-19c2-4168-93c0-778d8aa19fd5')
GROUP BY 1,2 ORDER BY dbus DESC
```

| billing_origin_product | sku_name | dbus | days |
|---|---|---|---|
| MODEL_SERVING | ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_US_WEST_OREGON | 484.86 | 7 |
| APPS | ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE_US_WEST_OREGON | 34.25 | 4 |
| DLT | ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_WEST_OREGON | 2.73 | 1 |

_3 row(s)_

### Table lineage captured by Unity Catalog

UC records the pipeline's provenance automatically — no manual documentation.

```sql
SELECT source_table_full_name, target_table_full_name, count(*) AS lineage_events FROM system.access.table_lineage WHERE target_table_full_name LIKE 'main.tiffin_agent_waste.%' AND source_table_full_name IS NOT NULL AND event_date >= current_date() - INTERVAL 30 DAYS GROUP BY 1,2 ORDER BY 1,2 LIMIT 40
```

| source_table_full_name | target_table_full_name | lineage_events |
|---|---|---|
| main.tiffin_agent_waste.bronze_agent_catalog | main.tiffin_agent_waste.agent_waste_gold | 5 |
| main.tiffin_agent_waste.bronze_model_pricing | main.tiffin_agent_waste.llm_turns_silver | 5 |
| main.tiffin_agent_waste.bronze_spans | main.tiffin_agent_waste.llm_turns_silver | 5 |
| main.tiffin_agent_waste.bronze_spans | main.tiffin_agent_waste.tool_calls_silver | 5 |
| main.tiffin_agent_waste.bronze_tool_catalog | main.tiffin_agent_waste.tool_calls_silver | 5 |
| main.tiffin_agent_waste.llm_turns_silver | main.tiffin_agent_waste.call_risk_features_gold | 5 |
| main.tiffin_agent_waste.llm_turns_silver | main.tiffin_agent_waste.sessions_silver | 5 |
| main.tiffin_agent_waste.raw_otel_spans | main.tiffin_agent_waste._shred_a | 1 |
| main.tiffin_agent_waste.raw_otel_spans | main.tiffin_agent_waste._shred_b | 1 |
| main.tiffin_agent_waste.raw_otel_spans | main.tiffin_agent_waste._shred_c | 1 |
| main.tiffin_agent_waste.raw_otel_spans | main.tiffin_agent_waste._shred_test | 1 |
| main.tiffin_agent_waste.raw_otel_spans | main.tiffin_agent_waste.bronze_spans | 10 |
| main.tiffin_agent_waste.sessions_silver | main.tiffin_agent_waste.agent_waste_gold | 5 |
| main.tiffin_agent_waste.sessions_silver | main.tiffin_agent_waste.tool_waste_gold | 5 |
| main.tiffin_agent_waste.tool_calls_silver | main.tiffin_agent_waste.call_risk_features_gold | 5 |
| main.tiffin_agent_waste.tool_calls_silver | main.tiffin_agent_waste.fix_queue | 1 |
| main.tiffin_agent_waste.tool_calls_silver | main.tiffin_agent_waste.sessions_silver | 5 |
| main.tiffin_agent_waste.tool_calls_silver | main.tiffin_agent_waste.tool_error_events_gold | 5 |
| main.tiffin_agent_waste.tool_calls_silver | main.tiffin_agent_waste.tool_recovery_silver | 5 |
| main.tiffin_agent_waste.tool_recovery_silver | main.tiffin_agent_waste.call_risk_features_gold | 5 |
| main.tiffin_agent_waste.tool_recovery_silver | main.tiffin_agent_waste.sessions_silver | 5 |
| main.tiffin_agent_waste.tool_recovery_silver | main.tiffin_agent_waste.tool_waste_gold | 5 |
| main.tiffin_agent_waste.tool_waste_gold | main.tiffin_agent_waste.fix_queue | 1 |

_23 lineage edge(s)_
