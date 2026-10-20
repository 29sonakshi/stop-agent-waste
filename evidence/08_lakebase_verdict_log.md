# Evidence 8: the Lakebase verdict log

_Real output from notebook `10_test_agent`, run as a Databricks job on e2-dogfood on 2026-10-20 04:44 UTC.
Lakebase project `projects/tiffin-preflight`, branch `production`, table
`tiffin.guard_decisions`._

The guard decides something about every tool call and then throws that decision away. This
table keeps it, written by the same `agent.py` the deployed app runs.

## Why Postgres and not Delta

One row per guarded tool call, roughly 14,500 a day across the fleet, written one at a time at
dispatch and read back by `run_id` while still being written. Single-row inserts at that rate
are the shape a warehouse handles worst. The read the app makes is a point lookup on an indexed
column, not a scan.

## Why not simply enable inference tables

Model Serving can capture every request and response automatically, and for the model's
*recommendation* that would be the better answer. Three things it cannot see:

1. **What the wrapper did.** `coerce` is a recommendation. `apply_coercion()` declines when the
   named parameter is absent, so the model saying coerce and the argument actually changing are
   two different events. Only the caller knows the second one.
2. **The outcome.** Inference logging stops at the model's response. Whether the tool call then
   succeeded is the label that separates a good coercion from a bad one.
3. **Fail-open events.** When the endpoint is unreachable no request is sent, so there is nothing
   to log. Those rows carry `action = 'guard_error'` here.

## The write is two-phase

The verdict is known before the tool runs; the outcome only after it returns. So `agent.py`
inserts the decision, keeps the row id, and updates that row once the tool answers:

```python
decision_id = logger.log_decision(..., coercion_applied=bool(note), guard_latency_ms=guard_ms)
ok, result = run_tool(fn, args)
logger.log_outcome(decision_id, ok)
```

Both calls swallow their own errors. An audit trail that can fail the call it is describing is
worse than no audit trail.

## The trail for one real run

`run_id = d5bb7c9c6936`, retrieved by point lookup. Verbatim notebook output:

| turn | tool | verdict | p(fail) | rewrote the arg? | tool succeeded? | value before | value after |
|---|---|---|---|---|---|---|---|
| 0 | `list_deliveries` | **allow** | 0.0002 | no | yes | `-` | `-` |
| 1 | `list_deliveries` | **coerce** | 0.8919 | yes | yes | `v2:eyJvZmZzZXQiOjI1fQ==` | `v1-eyJvZmZzZXQiOjI1` |

_2 decision(s) for this run._

Read the second row across. The model returned `coerce` at 0.8919, the wrapper really did rewrite
the argument, the value changed from the v2 token the server emitted to the v1 token it accepts,
and the call then **succeeded**. The first three columns an inference log would have given you.
The last two it would not.

## Aggregate over the table

```sql
SELECT action,
       count(*)                                                 AS decisions,
       sum(CASE WHEN coercion_applied THEN 1 ELSE 0 END)         AS args_rewritten,
       sum(CASE WHEN tool_ok THEN 1 ELSE 0 END)                  AS tool_succeeded,
       sum(CASE WHEN tool_ok IS FALSE THEN 1 ELSE 0 END)         AS tool_failed,
       sum(CASE WHEN guard_error IS NOT NULL THEN 1 ELSE 0 END)  AS fail_opens,
       round(avg(guard_latency_ms))                              AS avg_guard_ms
FROM tiffin.guard_decisions
GROUP BY action ORDER BY decisions DESC
```

| action | decisions | args_rewritten | tool_succeeded | tool_failed | fail_opens | avg_guard_ms |
|---|---|---|---|---|---|---|
| allow | 1 | 0 | 1 | 0 | 0 | 153 |
| coerce | 1 | 1 | 1 | 0 | 0 | 157 |

`avg_guard_ms` is the round trip as the agent experienced it, measured by the caller rather than
reported by the server, so it includes network time the endpoint's own metrics would not show.

## Schema

```sql
CREATE TABLE tiffin.guard_decisions (
  id                bigserial PRIMARY KEY,
  decided_at        timestamptz NOT NULL DEFAULT now(),
  run_id            text,              -- correlates one agent run
  agent_id          text NOT NULL,
  tool_server       text,
  tool_name         text NOT NULL,
  turn_index        int,
  action            text NOT NULL,     -- coerce | allow | guard_error
  failure_prob      numeric(6,4),
  mismatch_class    text,
  mismatched_param  text,
  guard_latency_ms  int,               -- round trip as the caller saw it
  coercion_applied  boolean NOT NULL,  -- did the wrapper really change the argument
  args_before       jsonb,
  args_after        jsonb,
  tool_ok           boolean,           -- did the call then succeed (phase 2)
  guard_error       text               -- set on a fail-open
);
```

Indexed on `decided_at DESC`, `run_id`, and `(tool_name, action)`.

Setup, grants and a self-test of the two-phase write are in
[`../notebooks/11_lakebase_verdict_log.py`](../notebooks/11_lakebase_verdict_log.py). The app
reads the trail back through [`../app/lakebase.py`](../app/lakebase.py).
