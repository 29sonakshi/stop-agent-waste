-- GOLD: analytics for the dashboard and Genie. Dimensions are kept wide on purpose --
-- anything an analyst might filter by has to exist as a column here.

-- Which tool servers are burning spend, and can it be tied to a deploy?
CREATE OR REFRESH MATERIALIZED VIEW tool_waste_gold
COMMENT 'Tool x day waste rollup. Every dollar is computed from telemetry, never hardcoded.'
AS
WITH call_cost AS (   -- attribute session waste to the tools that caused it
  SELECT r.trace_id, r.tool_server, r.tool_name, r.recovery_signature,
         r.n_calls, r.n_distinct_arg_sigs, r.max_calls_one_turn,
         s.session_date, s.agent_id,
         s.wasted_cost_usd / NULLIF(s.n_recovering_tools, 0) AS attributed_waste_usd,
         s.wasted_tokens   / NULLIF(s.n_recovering_tools, 0) AS attributed_waste_tokens
  FROM tool_recovery_silver r
  JOIN sessions_silver s ON r.trace_id = s.trace_id
  WHERE r.caused_recovery
)
SELECT
  session_date, tool_server, tool_name, recovery_signature, agent_id,
  COUNT(*)                                   AS recovering_traces,
  SUM(n_calls)                               AS total_calls,
  ROUND(AVG(n_distinct_arg_sigs), 2)         AS avg_arg_shapes_tried,
  MAX(max_calls_one_turn)                    AS worst_burst_in_one_turn,
  ROUND(SUM(attributed_waste_usd), 6)        AS waste_usd,
  SUM(attributed_waste_tokens)               AS waste_tokens
FROM call_cost
GROUP BY session_date, tool_server, tool_name, recovery_signature, agent_id;

-- Which applications are bleeding, and are they over budget?
CREATE OR REFRESH MATERIALIZED VIEW agent_waste_gold
COMMENT 'Agent x day spend, waste, and budget utilisation.'
AS
SELECT
  s.session_date, s.agent_id, a.display_name, a.owner_team, a.domain,
  s.model_id, s.endpoint_name,
  a.monthly_budget_usd,
  CAST(a.is_live_demo_agent AS BOOLEAN)        AS is_live_demo_agent,
  COUNT(*)                                     AS sessions,
  SUM(s.total_turns)                           AS turns,
  SUM(s.total_tool_calls)                      AS tool_calls,
  SUM(s.total_tokens)                          AS tokens,
  ROUND(SUM(s.total_cost_usd), 6)              AS spend_usd,
  ROUND(SUM(s.wasted_cost_usd), 6)             AS waste_usd,
  ROUND(100.0 * SUM(s.wasted_cost_usd) / NULLIF(SUM(s.total_cost_usd), 0), 2) AS waste_pct_of_spend,
  SUM(CASE WHEN s.had_recovery THEN 1 ELSE 0 END) AS sessions_with_recovery,
  ROUND(AVG(s.calls_per_turn), 3)              AS avg_calls_per_turn
FROM sessions_silver s
LEFT JOIN bronze_agent_catalog a ON s.agent_id = a.agent_id
GROUP BY s.session_date, s.agent_id, a.display_name, a.owner_team, a.domain,
         s.model_id, s.endpoint_name, a.monthly_budget_usd, a.is_live_demo_agent;

-- Error-visible failures only. Deliberately separate from waste: this is what a
-- conventional error dashboard would show, and the gap between the two is the story.
CREATE OR REFRESH MATERIALIZED VIEW tool_error_events_gold
COMMENT 'Error-emitting tool calls by day/server/tool -- what a conventional error log sees.'
AS
SELECT
  event_date, tool_server, tool_name, agent_id, error_type,
  COALESCE(mismatch_class, 'none')   AS mismatch_class,
  doc_quality,
  COUNT(*)                           AS error_calls,
  COUNT(DISTINCT trace_id)           AS affected_traces,
  COUNT(DISTINCT enduser_id)         AS affected_users,
  ROUND(AVG(latency_ms), 1)          AS avg_latency_ms
FROM tool_calls_silver
WHERE error_type IS NOT NULL
GROUP BY event_date, tool_server, tool_name, agent_id, error_type,
         COALESCE(mismatch_class, 'none'), doc_quality;
