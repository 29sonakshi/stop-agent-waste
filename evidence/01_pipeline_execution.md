# Evidence 1: the Lakeflow pipeline ran and produced governed tables

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood), 2026-10-19 21:08 UTC. Every table below is real query output, not illustrative._

The pipeline is 8 SQL files (`pipeline/`) running as a Lakeflow Spark Declarative Pipeline: bronze streaming tables from raw OTel spans, silver per-call tables, gold aggregates. Below: every table it created, with live row counts.

### Tables produced by the pipeline

```sql
SELECT table_name, table_type FROM system.information_schema.tables WHERE table_schema='tiffin_agent_waste' AND table_catalog='main' ORDER BY table_name
```

| table_name | table_type |
|---|---|
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_agent_waste_gold_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_agent_catalog_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_agent_tool_affinity_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_bug_registry_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_deploy_events_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_model_pricing_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_spans_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_bronze_tool_catalog_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_call_risk_features_gold_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_llm_turns_silver_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_sessions_silver_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_tool_calls_silver_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_tool_error_events_gold_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_tool_recovery_silver_1 | MANAGED |
| __materialization_mat_9453c79d_19c2_4168_93c0_778d8aa19fd5_tool_waste_gold_1 | MANAGED |
| agent_waste_gold | MATERIALIZED_VIEW |
| bronze_agent_catalog | MATERIALIZED_VIEW |
| bronze_agent_tool_affinity | MATERIALIZED_VIEW |
| bronze_bug_registry | MATERIALIZED_VIEW |
| bronze_deploy_events | MATERIALIZED_VIEW |
| bronze_model_pricing | MATERIALIZED_VIEW |
| bronze_spans | STREAMING_TABLE |
| bronze_tool_catalog | MATERIALIZED_VIEW |
| call_risk_features_gold | MATERIALIZED_VIEW |
| event_log_9453c79d_19c2_4168_93c0_778d8aa19fd5 | MANAGED |
| fix_queue | MANAGED |
| ground_truth | MANAGED |
| llm_turns_silver | MATERIALIZED_VIEW |
| raw_otel_spans | MANAGED |
| session_outcomes | MANAGED |
| sessions_silver | MATERIALIZED_VIEW |
| tool_calls_silver | MATERIALIZED_VIEW |
| tool_error_events_gold | MATERIALIZED_VIEW |
| tool_recovery_silver | MATERIALIZED_VIEW |
| tool_waste_gold | MATERIALIZED_VIEW |

_35 row(s)_

### Row counts across the medallion layers

```sql
SELECT 'bronze_spans (streaming)' AS tbl, count(*) AS rows FROM main.tiffin_agent_waste.bronze_spans
UNION ALL SELECT 'raw_otel_spans',      count(*) FROM main.tiffin_agent_waste.raw_otel_spans
UNION ALL SELECT 'tool_calls_silver',   count(*) FROM main.tiffin_agent_waste.tool_calls_silver
UNION ALL SELECT 'llm_turns_silver',    count(*) FROM main.tiffin_agent_waste.llm_turns_silver
UNION ALL SELECT 'sessions_silver',     count(*) FROM main.tiffin_agent_waste.sessions_silver
UNION ALL SELECT 'tool_recovery_silver',count(*) FROM main.tiffin_agent_waste.tool_recovery_silver
UNION ALL SELECT 'call_risk_features_gold', count(*) FROM main.tiffin_agent_waste.call_risk_features_gold
UNION ALL SELECT 'agent_waste_gold',    count(*) FROM main.tiffin_agent_waste.agent_waste_gold
UNION ALL SELECT 'tool_waste_gold',     count(*) FROM main.tiffin_agent_waste.tool_waste_gold
UNION ALL SELECT 'fix_queue',           count(*) FROM main.tiffin_agent_waste.fix_queue
ORDER BY rows DESC
```

| tbl | rows |
|---|---|
| bronze_spans (streaming) | 2608170 |
| raw_otel_spans | 2608170 |
| tool_calls_silver | 1300441 |
| call_risk_features_gold | 1300441 |
| llm_turns_silver | 1037729 |
| tool_recovery_silver | 479394 |
| sessions_silver | 270000 |
| tool_waste_gold | 810 |
| agent_waste_gold | 540 |
| fix_queue | 9 |

_10 row(s)_

### Trace window actually covered

```sql
SELECT min(event_date) AS first_day, max(event_date) AS last_day, count(DISTINCT event_date) AS days, count(DISTINCT trace_id) AS sessions, count(*) AS tool_calls FROM main.tiffin_agent_waste.tool_calls_silver
```

| first_day | last_day | days | sessions | tool_calls |
|---|---|---|---|---|
| 2026-07-15 | 2026-10-13 | 91 | 243112 | 1300441 |

_1 row(s)_
