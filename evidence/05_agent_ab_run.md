# Evidence 5: the agent, guard OFF vs guard ON

_Real execution output from notebook `10_test_agent`, run as a Databricks job on e2-dogfood
on 2026-10-19 21:10 UTC. The notebook imports the **same** `agent.py` the deployed app imports, so what is
tested here is what ships. Both runs hit the live Claude endpoint and the live guard endpoint._

The dispatcher asks the same question both times:

> *"Which of courier CR-4172's deliveries are running late?"*

The honest headline is not cost. It is **correctness**: the late delivery sits on page 2 of a
paginated tool response, and without the guard the agent never gets there.

**Everything in the code blocks below is verbatim job output. It has not been edited.**


## Guard OFF

| metric | value |
|---|---|
| turns | 6 |
| tokens | 9636 |
| answered | False |
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
  propose list_deliveries {"courier_id": "CR-4172", "page_token": "v2:eyJvZmZzZXQiOjI1fQ=="}
  FAIL "Your input couldn't be processed: TypeError in handler"
```

## Guard ON

| metric | value |
|---|---|
| turns | 3 |
| tokens | 4258 |
| answered | True |
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

- **D-4410315** — en route to 1042 Harrison St, picked up from merchant M-553014 — **18 minutes late**

All other deliveries are on time:
- D-4410221 — en route, 0 min late
- D-4410288 — picked up, 0 min late
- D-4410402 — delivered, 0 mi
```

## Side by side

| | guard OFF | guard ON |
|---|---|---|
| turns | 6 | 3 |
| tokens | 9,636 | 4,258 |
| answered the question | no | yes |

Guard on used **5,378 fewer tokens** and **3 fewer turns**.

Unguarded, the agent retried the same malformed pagination token four times, gave up, and never
reached page 2. Guarded, it received `coerce p=0.8919`, rewrote the token from the tool server's
own declared schema, and answered correctly on the first attempt.
