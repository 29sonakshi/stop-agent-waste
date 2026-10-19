-- SILVER: session grain. Joins the recovery reconstruction to real token cost so wasted
-- spend is COMPUTED from telemetry, never hardcoded. Wasted turns are the turns that
-- happened AFTER the agent first hit a recovery loop -- those exist only because a tool
-- rejected a reasonable argument, and they cost more than earlier turns because the
-- transcript keeps growing.
CREATE OR REFRESH MATERIALIZED VIEW sessions_silver
COMMENT 'Session grain: turns, tool calls, total cost, and behaviourally-derived wasted cost.'
AS
WITH rec AS (   -- when did this trace first start struggling?
  SELECT trace_id, MIN(first_turn) AS first_recovery_turn,
         MAX(caused_recovery) AS any_recovery,
         COUNT(*) AS n_recovering_tools
  FROM tool_recovery_silver
  WHERE caused_recovery
  GROUP BY trace_id
),
turns AS (
  SELECT l.trace_id, l.agent_id, l.endpoint_name, l.model_id,
         COUNT(*)                     AS total_turns,
         SUM(l.input_tokens)          AS input_tokens,
         SUM(l.output_tokens)         AS output_tokens,
         SUM(l.turn_cost_usd)         AS total_cost_usd,
         MIN(l.event_date)            AS session_date,
         MAX(l.latency_ms)            AS max_turn_latency_ms,
         -- cost incurred strictly after the struggle began
         SUM(CASE WHEN r.first_recovery_turn IS NOT NULL
                   AND l.turn_index > r.first_recovery_turn
                  THEN l.turn_cost_usd ELSE 0 END) AS wasted_cost_usd,
         SUM(CASE WHEN r.first_recovery_turn IS NOT NULL
                   AND l.turn_index > r.first_recovery_turn
                  THEN l.input_tokens + l.output_tokens ELSE 0 END) AS wasted_tokens
  FROM llm_turns_silver l
  LEFT JOIN rec r ON l.trace_id = r.trace_id
  GROUP BY l.trace_id, l.agent_id, l.endpoint_name, l.model_id
),
calls AS (
  SELECT trace_id,
         COUNT(*)                                   AS total_tool_calls,
         COUNT(DISTINCT tool_server)                AS n_distinct_servers,
         COUNT(DISTINCT CONCAT(tool_server,'.',tool_name)) AS n_distinct_tools,
         SUM(CASE WHEN shape_mismatch THEN 1 ELSE 0 END)   AS n_mismatched_calls,
         SUM(CASE WHEN error_type IS NOT NULL THEN 1 ELSE 0 END) AS n_errored_calls,
         MAX(CASE WHEN has_input_coercion = false THEN 1 ELSE 0 END) AS any_server_no_coercion
  FROM tool_calls_silver GROUP BY trace_id
)
SELECT
  t.trace_id, t.session_date, t.agent_id, t.endpoint_name, t.model_id,
  t.total_turns, t.input_tokens, t.output_tokens,
  t.input_tokens + t.output_tokens AS total_tokens,
  ROUND(t.total_cost_usd, 8)  AS total_cost_usd,
  ROUND(t.wasted_cost_usd, 8) AS wasted_cost_usd,
  t.wasted_tokens, t.max_turn_latency_ms,
  COALESCE(c.total_tool_calls, 0)   AS total_tool_calls,
  COALESCE(c.n_distinct_servers, 0) AS n_distinct_servers,
  COALESCE(c.n_distinct_tools, 0)   AS n_distinct_tools,
  COALESCE(c.n_mismatched_calls, 0) AS n_mismatched_calls,
  COALESCE(c.n_errored_calls, 0)    AS n_errored_calls,
  COALESCE(c.any_server_no_coercion, 0) AS any_server_no_coercion,
  ROUND(COALESCE(c.total_tool_calls,0) / NULLIF(t.total_turns,0), 3) AS calls_per_turn,
  COALESCE(r.any_recovery, FALSE)     AS had_recovery,
  r.first_recovery_turn,
  COALESCE(r.n_recovering_tools, 0)   AS n_recovering_tools
FROM turns t
LEFT JOIN calls c ON t.trace_id = c.trace_id
LEFT JOIN rec   r ON t.trace_id = r.trace_id;
