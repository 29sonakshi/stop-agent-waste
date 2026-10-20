# Evidence 5: the agent, guard OFF vs guard ON

_Real execution output from notebook `10_test_agent`, run as a Databricks job on e2-dogfood on
2026-10-20 04:44 UTC. The notebook imports the **same** `agent.py` the deployed app imports, so what is tested
here is what ships. Both runs hit the live Claude endpoint and the live guard endpoint._

The dispatcher asks the same question both times:

> *"Which of courier CR-4172's deliveries are running late?"*

The answer is **D-4410315, 18 minutes late**, and it sits on **page 2** of a paginated response.
Reaching page 2 means handing back a token the tool server emits in v2 format but only accepts in
v1.

`found_late` is the field that matters. `answered` only records that the agent emitted a final
message, which says nothing about whether it found anything.

**Everything in the code blocks below is verbatim job output. It has not been edited.**


## Guard OFF

| metric | value |
|---|---|
| turns | 6 |
| tokens | 9899 |
| answered | True |
| found_late | False |
| guard_on | False |
| run_id | None |

**Trace**

```
--- turn 1
  propose list_deliveries {"courier_id": "CR-4172"}
  ok {"page": 1, "deliveries": [{"delivery_id": "D-4410221", "status": "en_route", "minutes_late": 0, "pickup_merch
--- turn 2
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 3
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 4
  propose list_deliveries {"courier_id": "CR-4172", "limit": 25, "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 5
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 6
  FINAL: I attempted to retrieve further pages of CR-4172's deliveries, but the pagination token is causing a persistent server-side error, so I could only reliably retrieve the first page of results (2 deliveries):

| Delivery ID | Status | Minutes Late |
|---|---|---|
| D-4410221 | en_route | 0 |
| D-44102
```

## Guard ON

| metric | value |
|---|---|
| turns | 3 |
| tokens | 4344 |
| answered | True |
| found_late | True |
| guard_on | True |
| run_id | d5bb7c9c6936 |

**Trace**

```
--- turn 1
  propose list_deliveries {"courier_id": "CR-4172"}
  GUARD allow p=0.0002 cls=missing_required
  ok {"page": 1, "deliveries": [{"delivery_id": "D-4410221", "status": "en_route", "minutes_late": 0, "pickup_merch
--- turn 2
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  GUARD coerce p=0.8919 cls=token_version
  COERCED rewrote page_token from v2 to v1 format
  ok {"page": 2, "deliveries": [{"delivery_id": "D-4410315", "status": "en_route", "minutes_late": 18, "destination
--- turn 3
  FINAL: Here's the status of courier **CR-4172**'s deliveries:

| Delivery ID | Status | Minutes Late |
|---|---|---|
| D-4410221 | en_route | 0 (on time) |
| D-4410288 | picked_up | 0 (on time) |
| **D-4410315** | en_route | **18 minutes late** |
| D-4410402 | delivered | 0 (on time) |

**Only one delivery
```

## Side by side

| | guard OFF | guard ON |
|---|---|---|
| turns | 6 | 3 |
| tokens | 9,899 | 4,344 |
| **found the late delivery** | **no** | **yes** |

Guard on used **5,555 fewer tokens** and **3 fewer turns**, but the last row
is the one that matters.

The guarded run also has a `run_id`, because its verdicts were written to the Lakebase audit
trail. The unguarded run has none: no guard, no verdicts to record. That trail is in
[`08_lakebase_verdict_log.md`](08_lakebase_verdict_log.md).

## What the unguarded agent actually does

It does not fabricate an answer. Unguarded, the agent retries the malformed token, never reaches
page 2, and then reports honestly that it is blocked:

> *"I attempted to retrieve the next page of CR-4172's deliveries... but the pagination request is
> failing with a server-side error (`TypeError in handler`) despite several retries. I don't want
> to report an incomplete picture, so here's what I can confirm..."*

Honest, and still a business failure: the dispatcher asked which deliveries are running late,
spent 9,899 tokens over 6 turns, and never learned that D-4410315 is 18 minutes late.

The loop is non-deterministic, so turn and token counts move between runs. The deterministic part
is `found_late`: the tool server always rejects a v2 token, so unguarded the agent never gets to
page 2.

Screenshots of this comparison in the running app are in
[`../docs/demo-walkthrough.md`](../docs/demo-walkthrough.md).
