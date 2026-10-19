-- SILVER: one row per LLM turn, priced against the real rate card.
-- Input and output tokens are priced SEPARATELY -- input dominates (tens of thousands of
-- tokens of system prompt + tool defs + transcript) and is several times cheaper, so a
-- blended rate materially overstates cost.
CREATE OR REFRESH MATERIALIZED VIEW llm_turns_silver
COMMENT 'LLM-turn grain with per-turn cost from bronze_model_pricing.'
AS
WITH raw AS (
  SELECT
    trace_id, span_id, date AS event_date, time AS event_time,
    variant_get(attributes, '$["gen_ai.agent.id"]', 'string')              AS agent_id,
    variant_get(attributes, '$["enduser.id"]', 'string')                   AS enduser_id,
    variant_get(attributes, '$["databricks.endpoint_name"]', 'string')     AS endpoint_name,
    variant_get(attributes, '$["gen_ai.usage.input_tokens"]', 'bigint')    AS input_tokens,
    variant_get(attributes, '$["gen_ai.usage.output_tokens"]', 'bigint')   AS output_tokens,
    variant_get(attributes, '$["databricks.latency_ms"]', 'int')           AS latency_ms,
    CAST(variant_get(attributes, '$["tiffin.turn_index"]', 'int') AS INT)  AS turn_index
  FROM bronze_spans
  WHERE variant_get(attributes, '$["mlflow.spanType"]', 'string') = 'LLM'
)
SELECT r.*,
       p.model_id,
       ROUND(r.input_tokens  / 1000.0 * p.input_cost_per_1k, 8) AS input_cost_usd,
       ROUND(r.output_tokens / 1000.0 * p.output_cost_per_1k, 8) AS output_cost_usd,
       ROUND(r.input_tokens  / 1000.0 * p.input_cost_per_1k
           + r.output_tokens / 1000.0 * p.output_cost_per_1k, 8) AS turn_cost_usd
FROM raw r
LEFT JOIN bronze_model_pricing p ON r.endpoint_name = p.endpoint_name;
