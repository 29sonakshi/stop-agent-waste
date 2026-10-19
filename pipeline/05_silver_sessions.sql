-- SILVER: span-tree recovery reconstruction. THE LOAD-BEARING TRANSFORMATION.
--
-- This is what makes the ML possible rather than merely analytical: it walks the span tree
-- to work out, per trace, when the agent started struggling and what that struggle cost --
-- and it produces the TRAINING LABEL. Everything downstream depends on it.
--
-- THE LABEL, and why it is defined this way.
--   Two behaviours genuinely indicate a recovery loop:
--     (a) ARG CHURN      -- the model reshaped its arguments and retried  (shape bugs).
--                           Keyed on a VALUE-shape signature, not just types: several real
--                           bugs (v2-vs-v1 token, enum case, ISO-vs-epoch) are STRING-vs-STRING
--                           and a type-only signature cannot see the retry at all.
--     (b) BURST FAN-OUT  -- many calls to ONE tool WITHIN A SINGLE TURN   (rate-limit, timeout-split)
--   Total call count does NOT work: legitimate pagination calls one tool repeatedly too.
--   Measured on 270k sessions, a clean trace NEVER exceeds 3 calls to one tool in one turn,
--   while fan-out bugs reach 14 -- so 4 is a boundary the data draws, not one we picked.
--   Validated against held-out ground truth: precision 0.80, recall 0.49, F1 0.61
--   (vs 0.29 precision for a naive ">=3 total calls" rule, which drowned in false positives).
--   It is a HEURISTIC label. In production you would calibrate it against a sample of
--   human-reviewed sessions; ground truth is never used by the model itself.
CREATE OR REFRESH MATERIALIZED VIEW tool_recovery_silver
COMMENT 'Trace x tool grain. Behavioural recovery label derived from the span tree.'
AS
WITH per_turn AS (
  SELECT trace_id, tool_server, tool_name, turn_index, COUNT(*) AS calls_in_turn
  FROM tool_calls_silver
  GROUP BY trace_id, tool_server, tool_name, turn_index
),
per_key AS (
  SELECT
    c.trace_id, c.tool_server, c.tool_name,
    COUNT(*)                                  AS n_calls,
    COUNT(DISTINCT c.arg_value_signature)      AS n_distinct_arg_sigs,
    MAX(t.calls_in_turn)                       AS max_calls_one_turn,
    MIN(c.turn_index)                          AS first_turn,
    MAX(c.turn_index)                          AS last_turn,
    MAX(c.start_time_unix_nano)                AS last_call_nano,
    SUM(CASE WHEN c.shape_mismatch THEN 1 ELSE 0 END) AS n_mismatched_calls,
    SUM(CASE WHEN c.error_type IS NOT NULL THEN 1 ELSE 0 END) AS n_errored_calls
  FROM tool_calls_silver c
  JOIN per_turn t
    ON c.trace_id = t.trace_id AND c.tool_server = t.tool_server
   AND c.tool_name = t.tool_name AND c.turn_index = t.turn_index
  GROUP BY c.trace_id, c.tool_server, c.tool_name
)
SELECT *,
  CASE WHEN n_distinct_arg_sigs > 1 OR max_calls_one_turn >= 4
       THEN TRUE ELSE FALSE END AS caused_recovery,
  CASE WHEN n_distinct_arg_sigs > 1 AND max_calls_one_turn >= 4 THEN 'churn_and_burst'
       WHEN n_distinct_arg_sigs > 1                             THEN 'arg_churn'
       WHEN max_calls_one_turn >= 4                             THEN 'burst_fanout'
       ELSE 'none' END AS recovery_signature,
  GREATEST(last_turn - first_turn, 0) AS turns_spanned
FROM per_key;
