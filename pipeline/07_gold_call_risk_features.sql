-- GOLD: the ML training table for the pre-flight risk model.
--
-- GRAIN: one row per tool call -- the same unit the model scores at inference, when a call
-- is about to be made and no outcome exists yet.
--
-- WHAT IS DELIBERATELY EXCLUDED, and why. Every one of these would score beautifully in
-- training and be useless or dishonest in production:
--   * error.type / http_status      -- known only AFTER the call runs. Post-hoc leakage.
--   * bug family identity           -- destroys the held-out-family test the whole claim rests on.
--   * per-bug message_quality       -- only ever "none" on the silent family; hands over the answer.
--   * session outcome / CSAT        -- known only when the session ends.
-- What remains is strictly what a wrapper can know BEFORE dispatching the call: the tool,
-- the shape of the arguments against the declared schema, the server's published quality
-- signals, and the agent's own conversation state so far.
CREATE OR REFRESH MATERIALIZED VIEW call_risk_features_gold
COMMENT 'Tool-call grain ML training table. Pre-call features only; label from the span tree.'
AS
WITH turn_tokens AS (   -- the agent's own token spend per turn
  SELECT trace_id, turn_index, SUM(input_tokens + output_tokens) AS turn_tokens
  FROM llm_turns_silver GROUP BY trace_id, turn_index
),
cum AS (                -- tokens consumed BEFORE the current turn (knowable at call time)
  SELECT trace_id, turn_index,
         COALESCE(SUM(turn_tokens) OVER (
           PARTITION BY trace_id ORDER BY turn_index
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS tokens_before_turn
  FROM turn_tokens
),
ordered AS (
  SELECT c.*,
         ROW_NUMBER() OVER (PARTITION BY c.trace_id, c.tool_server, c.tool_name
                            ORDER BY c.start_time_unix_nano) AS call_seq_this_tool,
         COUNT(*)     OVER (PARTITION BY c.trace_id, c.tool_server, c.tool_name)
                                                              AS total_calls_this_tool
  FROM tool_calls_silver c
)
SELECT
  -- keys / slicing (NOT features -- excluded at training time)
  o.trace_id, o.span_id, o.event_date, o.agent_id, o.tool_server, o.tool_name,

  -- ===== FEATURES: the call itself =====
  o.turn_index,
  o.n_args_sent,
  o.n_params_mismatched,
  o.n_params_missing,
  CAST(o.shape_mismatch AS INT)                       AS f_shape_mismatch,
  COALESCE(o.mismatch_class, 'none')                  AS f_mismatch_class,
  o.arg_signature,

  -- ===== FEATURES: the server's published contract quality =====
  COALESCE(o.doc_quality, 'unknown')                  AS f_doc_quality,
  CAST(COALESCE(o.has_examples, false) AS INT)        AS f_has_examples,
  CAST(COALESCE(o.has_input_coercion, false) AS INT)  AS f_has_input_coercion,
  COALESCE(o.sla_p99_ms, 0)                           AS f_sla_p99_ms,

  -- ===== FEATURES: the agent's own state so far =====
  COALESCE(cu.tokens_before_turn, 0)                  AS f_tokens_before_turn,
  o.call_seq_this_tool                                AS f_call_seq_this_tool,

  -- ===== LABEL =====
  -- The key needed recovery, and this is not the final (successful) attempt.
  CASE WHEN r.caused_recovery AND o.call_seq_this_tool < o.total_calls_this_tool
       THEN 1 ELSE 0 END                              AS label_caused_recovery,
  r.recovery_signature,

  -- carried for cost attribution and dashboards, NOT fed to the model
  o.error_type, o.http_status, o.latency_ms
FROM ordered o
LEFT JOIN tool_recovery_silver r
  ON o.trace_id = r.trace_id AND o.tool_server = r.tool_server AND o.tool_name = r.tool_name
LEFT JOIN cum cu
  ON o.trace_id = cu.trace_id AND o.turn_index = cu.turn_index;
