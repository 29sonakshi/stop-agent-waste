# Evidence 4: Unity Catalog governance

_Executed on Databricks (e2-dogfood) on 2026-10-19 22:28 UTC._

One governance plane over the raw spans, every pipeline table, the model, and the serving
endpoint. Lineage and cost are both queryable from system tables rather than documented by hand.

### Table lineage captured by Unity Catalog

UC records the pipeline's provenance automatically, with no manual documentation.

Two edges below target `main.tiffin_agent_waste.fix_queue`, a table that no longer exists. It was created and then dropped during development, and the lineage system table keeps a 30-day history, so it is still in the window. Left in rather than filtered out, because the point of lineage is that it records what actually happened.

```sql
SELECT source_table_full_name, target_table_full_name, count(*) AS lineage_events FROM system.access.table_lineage WHERE target_table_full_name LIKE 'main.tiffin_agent_waste.%' AND source_table_full_name IS NOT NULL AND target_table_full_name NOT LIKE '%__materialization%' AND event_date >= current_date() - INTERVAL 30 DAYS GROUP BY 1,2 ORDER BY 1,2 LIMIT 40
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

_23 row(s)_

### Billing attribution: what this solution costs to run

Cost is itself governed and queryable: the same system tables that prove the pipeline ran also price it.

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
| MODEL_SERVING | ENTERPRISE_SERVERLESS_REAL_TIME_INFERENCE_US_WEST_OREGON | 487.00 | 7 |
| APPS | ENTERPRISE_ALL_PURPOSE_SERVERLESS_COMPUTE_US_WEST_OREGON | 34.25 | 4 |
| DLT | ENTERPRISE_JOBS_SERVERLESS_COMPUTE_US_WEST_OREGON | 2.73 | 1 |

_3 row(s)_
