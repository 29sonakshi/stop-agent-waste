# Evidence 2: the failures are invisible to error monitoring

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood), 2026-10-19 21:08 UTC. Every table below is real query output, not illustrative._

This is the core claim of the whole build: these tool calls fail while returning **HTTP 200**, so no error dashboard can see them. Proven below from the trace data.

### Every tool call returns HTTP 200: including the failures

If status code were a usable signal there would be non-200 rows here. There are none.

```sql
SELECT http_status, coalesce(error_type,'(none)') AS error_type, count(*) AS calls FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY 1,2 ORDER BY calls DESC
```

| http_status | error_type | calls |
|---|---|---|
| 200 | (none) | 1216270 |
| 200 | tool_execution_error | 84171 |

_2 row(s)_

### Argument-shape mismatches, also all HTTP 200

```sql
SELECT shape_mismatch, http_status, count(*) AS calls, round(100.0*count(*)/sum(count(*)) OVER (),2) AS pct_of_all_calls FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY 1,2 ORDER BY calls DESC
```

| shape_mismatch | http_status | calls | pct_of_all_calls |
|---|---|---|---|
| false | 200 | 1264127 | 97.21 |
| true | 200 | 36314 | 2.79 |

_2 row(s)_

### What the silver layer detects that the status code cannot

Produced by comparing the arguments the agent sent against the `inputSchema` the MCP server itself declares. See `pipeline/03_silver_tool_calls.sql`.

```sql
SELECT coalesce(mismatch_class,'(clean)') AS mismatch_class, coalesce(mismatched_param,'-') AS param, count(*) AS calls FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY 1,2 HAVING count(*) > 100 ORDER BY calls DESC
```

| mismatch_class | param | calls |
|---|---|---|
| (clean) | - | 1264127 |
| numeric_vs_string | since | 13117 |
| token_version | page_token | 8390 |
| enum_case | status | 3560 |
| array_vs_scalar | fields | 3476 |
| missing_required | - | 3268 |
| (clean) | idempotency_key | 2891 |
| string_vs_boolean | active | 1612 |

_8 row(s)_

### Labelled training rows, with zero human labelling

```sql
SELECT count(*) AS labelled_tool_calls, sum(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS positives, round(100.0*sum(CASE WHEN shape_mismatch THEN 1 ELSE 0 END)/count(*),2) AS positive_rate_pct FROM main.tiffin_agent_waste.tool_calls_silver
```

| labelled_tool_calls | positives | positive_rate_pct |
|---|---|---|
| 1300441 | 36314 | 2.79 |

_1 row(s)_
