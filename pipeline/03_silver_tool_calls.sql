-- SILVER: one row per tool call, with the ARGUMENT-SHAPE COMPARISON.
--
-- THE HERO TRANSFORMATION. The model sends arguments; the MCP server declares a schema via
-- tools/list. This compares what was actually sent against what was declared, so the
-- mismatch is COMPUTED from the server's own contract rather than guessed. That is also why
-- the coercion suggestion downstream can be trusted.
--
-- GOTCHA: OTel attribute keys are flat dotted strings inside a VARIANT, so extraction needs
-- bracket notation -- variant_get(attributes, '$["gen_ai.tool.name"]', ...). Dot-path
-- notation resolves to a nested field that does not exist and returns NULL with NO error:
-- the pipeline "succeeds" while every column is silently empty.
CREATE OR REFRESH MATERIALIZED VIEW tool_calls_silver
COMMENT 'Tool-call grain. Flattened attributes plus arg_signature and the declared-schema mismatch.'
AS
WITH raw AS (
  SELECT
    trace_id, span_id, parent_span_id, date AS event_date, time AS event_time,
    start_time_unix_nano,
    variant_get(attributes, '$["gen_ai.agent.id"]', 'string')             AS agent_id,
    variant_get(attributes, '$["enduser.id"]', 'string')                  AS enduser_id,
    variant_get(attributes, '$["tool_source"]', 'string')                 AS tool_server,
    variant_get(attributes, '$["gen_ai.tool.name"]', 'string')            AS tool_name,
    variant_get(attributes, '$["gen_ai.tool.call.arguments"]', 'string')  AS args_json,
    variant_get(attributes, '$["gen_ai.tool.call.result"]', 'string')     AS result_json,
    variant_get(attributes, '$["error.type"]', 'string')                  AS error_type,
    variant_get(attributes, '$["http.response.status_code"]', 'int')      AS http_status,
    variant_get(attributes, '$["databricks.latency_ms"]', 'int')          AS latency_ms,
    CAST(variant_get(attributes, '$["tiffin.turn_index"]', 'int') AS INT) AS turn_index,
    -- the whole argument object's shape, human-readable: OBJECT<fields: ARRAY<STRING>, ...>
    schema_of_variant(parse_json(
      variant_get(attributes, '$["gen_ai.tool.call.arguments"]', 'string'))) AS arg_signature
  FROM bronze_spans
  WHERE variant_get(attributes, '$["mlflow.spanType"]', 'string') = 'TOOL'
),
-- what the server DECLARES, one row per parameter
declared AS (
  SELECT tool_server, tool_name,
         key AS arg_key,
         CAST(value AS STRING) AS declared_type,
         CASE
           WHEN CAST(value AS STRING) IN ('string','csv_string','flat_string',
                                          'iso8601_string','v1_token_string') THEN 'STRING'
           WHEN CAST(value AS STRING) LIKE 'enum[%'                            THEN 'STRING'
           WHEN CAST(value AS STRING) = 'int'                                  THEN 'BIGINT'
           WHEN CAST(value AS STRING) = 'double'                               THEN 'DOUBLE'
           WHEN CAST(value AS STRING) = 'boolean'                              THEN 'BOOLEAN'
           WHEN CAST(value AS STRING) = 'array<string>'                        THEN 'ARRAY'
           WHEN CAST(value AS STRING) LIKE '%_object'                          THEN 'OBJECT'
           ELSE 'STRING'
         END AS expected_variant_type
  FROM bronze_tool_catalog, LATERAL variant_explode(parse_json(expected_args_json))
),
-- what was actually SENT, one row per parameter
sent AS (
  SELECT span_id, tool_server, tool_name,
         key AS arg_key,
         schema_of_variant(value) AS sent_type,
         CAST(value AS STRING)    AS sent_value
  FROM raw, LATERAL variant_explode(parse_json(args_json))
),
arg_cmp AS (
  SELECT s.span_id, s.arg_key, s.sent_type, s.sent_value,
         d.declared_type, d.expected_variant_type,
    -- (1) TYPE disagreement
    CASE
      WHEN d.expected_variant_type IS NULL                                        THEN 0
      WHEN d.expected_variant_type = 'ARRAY'   AND s.sent_type NOT LIKE 'ARRAY%'  THEN 1
      WHEN d.expected_variant_type = 'OBJECT'  AND s.sent_type NOT LIKE 'OBJECT%' THEN 1
      WHEN d.expected_variant_type = 'STRING'  AND s.sent_type <> 'STRING'         THEN 1
      WHEN d.expected_variant_type = 'BIGINT'  AND s.sent_type NOT IN ('BIGINT','INT','LONG') THEN 1
      WHEN d.expected_variant_type = 'DOUBLE'  AND s.sent_type NOT LIKE 'DECIMAL%'
                                               AND s.sent_type NOT IN ('DOUBLE','FLOAT','BIGINT') THEN 1
      WHEN d.expected_variant_type = 'BOOLEAN' AND s.sent_type <> 'BOOLEAN'        THEN 1
      ELSE 0
    END AS type_mismatch,
    -- (2) FORMAT / VALUE disagreement inside a correctly-TYPED string.
    -- Several real bug classes are invisible to a type check: a v2 pagination token where
    -- v1 is expected, a lower-case value against a case-sensitive enum, an ISO timestamp
    -- where epoch millis are wanted. All of them are STRING-vs-STRING. The declared type
    -- NAME carries the format contract, so checking the value against it is legitimate --
    -- and it is still the server's own published schema doing the work, not a guess.
    CASE
      WHEN s.sent_type <> 'STRING' THEN 0
      WHEN d.declared_type = 'v1_token_string'
           AND NOT s.sent_value LIKE 'v1-%'                                       THEN 1
      WHEN d.declared_type = 'iso8601_string'
           AND s.sent_value NOT RLIKE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'               THEN 1
      WHEN d.declared_type = 'csv_string'
           AND (s.sent_value LIKE '[%' OR s.sent_value LIKE '{%')                 THEN 1
      WHEN d.declared_type LIKE 'enum[%'
           AND NOT array_contains(
                 split(regexp_extract(d.declared_type, 'enum\\[(.*)\\]', 1), ','),
                 s.sent_value)                                                    THEN 1
      ELSE 0
    END AS format_mismatch,
    -- name the disagreement so the recommendation can say what to coerce
    CASE
      WHEN d.expected_variant_type = 'STRING'  AND s.sent_type LIKE 'ARRAY%'   THEN 'array_vs_scalar'
      WHEN d.expected_variant_type = 'STRING'  AND s.sent_type LIKE 'OBJECT%'  THEN 'nested_vs_flat'
      WHEN d.expected_variant_type = 'STRING'  AND s.sent_type IN ('BIGINT','INT','LONG','DOUBLE') THEN 'numeric_vs_string'
      WHEN d.expected_variant_type = 'BIGINT'  AND (s.sent_type LIKE 'DECIMAL%' OR s.sent_type='DOUBLE') THEN 'float_vs_int'
      WHEN d.expected_variant_type = 'BIGINT'  AND s.sent_type = 'STRING'      THEN 'string_vs_numeric'
      WHEN d.expected_variant_type = 'BOOLEAN' AND s.sent_type = 'STRING'      THEN 'string_vs_boolean'
      WHEN d.expected_variant_type = 'OBJECT'  AND s.sent_type NOT LIKE 'OBJECT%' THEN 'shape_drift'
      WHEN s.sent_type = 'STRING' AND d.declared_type = 'v1_token_string'
           AND NOT s.sent_value LIKE 'v1-%'                                    THEN 'token_version'
      WHEN s.sent_type = 'STRING' AND d.declared_type = 'iso8601_string'
           AND s.sent_value NOT RLIKE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'            THEN 'date_format'
      WHEN s.sent_type = 'STRING' AND d.declared_type = 'csv_string'
           AND (s.sent_value LIKE '[%' OR s.sent_value LIKE '{%')               THEN 'serialized_collection'
      WHEN s.sent_type = 'STRING' AND d.declared_type LIKE 'enum[%'
           AND NOT array_contains(
                 split(regexp_extract(d.declared_type, 'enum\\[(.*)\\]', 1), ','),
                 s.sent_value)                                                 THEN 'enum_case'
      ELSE NULL
    END AS mismatch_class
  FROM sent s
  LEFT JOIN declared d
    ON s.tool_server = d.tool_server AND s.tool_name = d.tool_name AND s.arg_key = d.arg_key
),
value_sig AS (
  -- A VALUE-shape signature used ONLY for the behavioural churn label. It must be able to
  -- see the agent reshaping an argument between attempts, because several real bugs are
  -- STRING-vs-STRING and leave the TYPE signature identical ("v2:tok" -> "v1-tok").
  --
  -- Two calibration lessons are encoded here, both measured:
  --  1. Distinguishing upper/lower/free-text made ordinary value variation look like
  --     reshaping ("downtown" vs "M-204881" are both plain strings but different buckets)
  --     -> 31,499 false positives, precision fell 0.80 -> 0.46. So everything that is not
  --     a recognisable FORMAT collapses to one bucket.
  --  2. That collapse then hid the enum-case retry entirely ("ACTIVE" and "active" landed
  --     in the same bucket), so merchant_enum_case had 46k calls and ZERO positive labels --
  --     the model would have learned that enum mismatches are harmless.
  --  3. Using the literal enum VALUE to fix (2) then broke the opposite way: an enum is
  --     *supposed* to vary, so get_eta's unit=seconds|minutes churned on every healthy call
  --     and the silent family picked up 83k spurious positives. The retry signature for an
  --     enum is a transition from OUT-OF-DOMAIN to IN-DOMAIN, so the bucket is validity
  --     (2 states), not identity. Legitimate variation inside the domain no longer churns.
  SELECT s.span_id,
    concat_ws('|', sort_array(collect_list(concat(
      s.arg_key, ':', s.sent_type, ':',
      CASE
        WHEN d.declared_type LIKE 'enum[%' THEN
             CASE WHEN array_contains(
                    split(regexp_extract(d.declared_type, 'enum\\[(.*)\\]', 1), ','),
                    s.sent_value) THEN 'enum_ok' ELSE 'enum_bad' END
        WHEN s.sent_type <> 'STRING'                           THEN ''
        WHEN s.sent_value LIKE 'v1-%'                          THEN 'v1'
        WHEN s.sent_value LIKE 'v2:%'                          THEN 'v2'
        WHEN s.sent_value RLIKE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'  THEN 'iso'
        WHEN s.sent_value RLIKE '^[0-9]+$'                      THEN 'numstr'
        WHEN s.sent_value LIKE '[%'                             THEN 'jsonarr'
        WHEN s.sent_value LIKE '{%'                             THEN 'jsonobj'
        ELSE 'str'
      END)))) AS arg_value_signature
  FROM sent s
  LEFT JOIN declared d
    ON s.tool_server = d.tool_server AND s.tool_name = d.tool_name AND s.arg_key = d.arg_key
  GROUP BY s.span_id
),
per_call AS (
  SELECT span_id,
         COUNT(*)                                     AS n_args_sent,
         SUM(GREATEST(type_mismatch, format_mismatch)) AS n_params_mismatched,
         SUM(type_mismatch)                           AS n_params_type_mismatched,
         SUM(format_mismatch)                         AS n_params_format_mismatched,
         MAX(mismatch_class)                          AS mismatch_class,
         MAX(CASE WHEN GREATEST(type_mismatch, format_mismatch) = 1 THEN arg_key END) AS mismatched_param
  FROM arg_cmp GROUP BY span_id
),
-- declared parameters that were omitted entirely
missing AS (
  SELECT r.span_id, COUNT(*) AS n_params_missing
  FROM raw r
  JOIN declared d ON r.tool_server = d.tool_server AND r.tool_name = d.tool_name
  LEFT JOIN sent s ON s.span_id = r.span_id AND s.arg_key = d.arg_key
  WHERE s.arg_key IS NULL
  GROUP BY r.span_id
)
SELECT
  r.trace_id, r.span_id, r.parent_span_id, r.event_date, r.event_time,
  r.start_time_unix_nano, r.agent_id, r.enduser_id,
  r.tool_server, r.tool_name, r.turn_index, r.latency_ms,
  r.arg_signature,
  COALESCE(v.arg_value_signature, '') AS arg_value_signature,
  r.args_json, r.result_json,
  r.error_type, r.http_status,
  COALESCE(p.n_args_sent, 0)         AS n_args_sent,
  COALESCE(p.n_params_mismatched, 0)      AS n_params_mismatched,
  COALESCE(p.n_params_type_mismatched, 0)   AS n_params_type_mismatched,
  COALESCE(p.n_params_format_mismatched, 0) AS n_params_format_mismatched,
  COALESCE(m.n_params_missing, 0)    AS n_params_missing,
  p.mismatched_param,
  COALESCE(p.mismatch_class,
           CASE WHEN COALESCE(m.n_params_missing,0) > 0 THEN 'missing_required' END) AS mismatch_class,
  CASE WHEN COALESCE(p.n_params_mismatched,0) > 0
         OR COALESCE(m.n_params_missing,0) > 0 THEN TRUE ELSE FALSE END AS shape_mismatch,
  -- the server's published quality signals: available BEFORE any call is made,
  -- so they are legitimate pre-flight model features
  t.doc_quality, t.has_examples, t.has_input_coercion, t.owner_team, t.sla_p99_ms
FROM raw r
LEFT JOIN per_call p ON r.span_id = p.span_id
LEFT JOIN missing  m ON r.span_id = m.span_id
LEFT JOIN value_sig v ON r.span_id = v.span_id
LEFT JOIN bronze_tool_catalog t
  ON r.tool_server = t.tool_server AND r.tool_name = t.tool_name;
