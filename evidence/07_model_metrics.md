# Evidence 8: the model, and how hard it was actually tested

_Pulled live from the MLflow tracking server and Unity Catalog on e2-dogfood, 2026-10-19 21:18 UTC._

MLflow run `48c94655d1d5486499674c24ceb93eb7` ("pre_flight_risk"), status **FINISHED**.

## What it is

| | |
|---|---|
| algorithm | `HistGradientBoostingClassifier` |
| training rows | 1,300,441 |
| engineered features | 12 (derived from the 7 fields the endpoint accepts) |
| positive rate in training data | 20.2% |
| coerce threshold | 0.45 |
| warn threshold | 0.225 |
| registered model | `main.tiffin_agent_waste.tool_call_risk` v1 |
| serving endpoint | `tiffin-tool-call-risk` |

## The test that matters: leave-one-bug-family-out

A random train/test split would be close to meaningless here. Rows from the same bug family are
near-duplicates, so a random split lets the model memorise a bug it will also be graded on, and
the score comes back near 1.0, which is exactly what happened on an early iteration and was a
red flag, not a win.

So the model is scored by **holding out an entire bug family** and asking it to flag a failure
mode it has never seen.

| metric | value | what it means |
|---|---|---|
| **`lofo_mean_auc`** | **0.8252** | mean AUC across folds, each withholding one whole bug family |
| `lofo_worst_auc` | 0.3360 | **the weakest fold, below random. See the caveat below.** |
| `rule_baseline_mean_auc` | 0.6035 | a hand-written rule set, scored the same way |
| `lift_over_rule` | 0.2217 | how much the model beats those rules |
| `calibration_auc` | 0.9932 | in-distribution fit, for reference only |

### The honest caveat on `lofo_worst_auc` = 0.336

One held-out family scores **below random**. The mean of 0.825 is carried by
the families the model generalises to; it does not generalise to all of them. The weak fold is the
`get_eta` family, a tool that returns **HTTP 200 with a plausible but wrong value** (seconds where
minutes were asked for). There is no argument-shape tell for that, because the arguments are
perfectly valid. The failure is in the *result*, and this model only reads the *request*.

That is a real limitation, stated here rather than averaged away: **this guard catches malformed
calls, not wrong answers.** Result-consistency features are the obvious next step and are listed
in the README's next-steps section.

### Why the error-log baseline scores zero

A baseline built on error logs has an AUC of **0.000** recall against this target. Not low: zero.
Every one of these calls returns HTTP 200 (see `02_silent_failure_proof.md`), so there is no error
to key on. That is the whole reason the project reads traces rather than logs.

## Serving

```json
{
  "endpoint": "tiffin-tool-call-risk",
  "state": {
    "ready": "READY",
    "config_update": "NOT_UPDATING",
    "suspend": "NOT_SUSPENDED",
    "system_update_failure": false
  },
  "served_model": "main.tiffin_agent_waste.tool_call_risk",
  "version": "1",
  "workload_size": "Small",
  "workload_type": "CPU",
  "scale_to_zero_enabled": false,
  "config_version": 8
}
```

Measured round-trip latency against this endpoint is in `06_guard_endpoint.md` (6 live calls, ~0.3 s each).

## Unity Catalog registration

```json
{
  "model_name": "tool_call_risk",
  "version": 1,
  "status": "READY",
  "created_by": "sonakshi.pandey@databricks.com",
  "storage_location": "s3://us-west-2-extstaging-managed-catalog-test-bucket-1/19a85dee-54bc-43a2-87ab-023d0ec16013/models/1b26b37d-436e-43fb-9538-145dbf6dcfa4/versions/a169cc43-8b61-4e11-8088-b6f6a7e76916"
}
```
