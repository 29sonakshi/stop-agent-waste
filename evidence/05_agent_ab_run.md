# Evidence 5: the agent, guard OFF vs guard ON

_Real execution output from notebook `10_test_agent`, run as a Databricks job on e2-dogfood on
2026-10-19 23:12 UTC. The notebook imports the **same** `agent.py` the deployed app imports, so what is tested
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
| tokens | 10001 |
| answered | True |
| found_late | False |
| guard_on | False |

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
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ==", "limit": 25}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 5
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
--- turn 6
  FINAL: I attempted to retrieve the next page of CR-4172's deliveries (there's a `next_page_token` indicating more results), but the pagination request is failing with a server-side error (`TypeError in handler`) despite several retries. I don't want to report an incomplete picture, so here's what I can con
```

## Guard ON

| metric | value |
|---|---|
| turns | 3 |
| tokens | 4236 |
| answered | True |
| found_late | True |
| guard_on | True |

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
  FINAL: Courier **CR-4172** currently has one delivery running late:

- **D-4410315** — en route to 1042 Harrison St, **18 minutes late** (picked up from merchant M-553014)

The rest of their deliveries are on time:
- D-4410221 — en route, on time
- D-4410288 — picked up, on time
- D-4410402 — delivered, on
```

## Side by side

| | guard OFF | guard ON |
|---|---|---|
| turns | 6 | 3 |
| tokens | 10,001 | 4,236 |
| **found the late delivery** | **no** | **yes** |

Guard on used **5,765 fewer tokens** and **3 fewer turns**, but the last
row is the one that matters.

## What the unguarded agent actually does

It does not fabricate an answer, and the claim is worth stating precisely rather than
dramatically. Unguarded, the agent retries the malformed token, never reaches page 2, and then
reports honestly that it is blocked:

> *"I attempted to retrieve the next page of CR-4172's deliveries... but the pagination request is
> failing with a server-side error (`TypeError in handler`) despite several retries. I don't want
> to report an incomplete picture, so here's what I can confirm..."*

Honest, and still a business failure: the dispatcher asked which deliveries are running late,
spent 10,001 tokens and 6 turns, and never learned that D-4410315 is 18 minutes late.

The loop is non-deterministic, so turn and token counts move between runs. An earlier run of this
same notebook hit the turn limit without producing a final message at all. The deterministic part
is `found_late`: the tool server always rejects a v2 token, so unguarded the agent never gets to
page 2.

Screenshots of this same comparison in the running app are in
[`../docs/demo-walkthrough.md`](../docs/demo-walkthrough.md).
