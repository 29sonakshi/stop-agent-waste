# Evidence 9: what this costs the delivery operation

_Executed against `main.tiffin_agent_waste` on Databricks (e2-dogfood) on 2026-10-20 05:11 UTC._

The token figures in `03_business_impact.md` are what the platform team sees. This file is what
the delivery operation sees, because a dispatcher does not ask "which deliveries are running
late?" out of curiosity. They ask in order to **act**: reroute a courier, warn the customer, comp
the order. That answer has a decay window, and a broken tool contract eats into it.

**What is derived here and what is not.** Everything in the tables below is computed from the
trace data. The final link, from a missed intervention window to an on-time-delivery percentage,
is **not** in this dataset and is not claimed here. That coefficient belongs to the operator and
is stated as an assumption wherever it appears, never as a finding.

### How often a dispatcher question hits a silent tool failure

One session in twelve. Not an edge case.

```sql
WITH bad AS (
  SELECT trace_id, max(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS hit
  FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY trace_id
)
SELECT CASE WHEN hit=1 THEN 'hit a silent tool failure' ELSE 'clean session' END AS cohort,
       count(*) AS sessions,
       round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct_of_sessions
FROM bad GROUP BY cohort ORDER BY sessions DESC
```

| cohort | sessions | pct_of_sessions |
|---|---|---|
| clean session | 223711 | 92.0 |
| hit a silent tool failure | 19401 | 8.0 |

_2 row(s)_

### What it costs the dispatcher in time

The median dispatcher waits **8.6 seconds** for a clean answer and **20.6 seconds** when the
agent is fighting a broken tool contract. Nothing in the generator inflates latency for the
failing cohort: per-turn LLM latency is drawn from the same distribution either way
(`random.randint(700, 4200)` in `notebooks/01_generate_traces.py`). The gap is purely the
extra turns. Means are not used here because clean sessions have a long tail of legitimately
slow tool calls that pulls the average the wrong way; the median is the honest comparison.

```sql
WITH bad AS (
  SELECT trace_id, max(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS hit
  FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY trace_id
),
llm AS (
  SELECT trace_id, sum(latency_ms) AS llm_ms, count(*) AS turns
  FROM main.tiffin_agent_waste.llm_turns_silver GROUP BY trace_id
),
tool AS (
  SELECT trace_id, sum(latency_ms) AS tool_ms
  FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY trace_id
)
SELECT CASE WHEN b.hit=1 THEN 'hit a silent tool failure' ELSE 'clean session' END AS cohort,
       count(*) AS sessions,
       round(avg(l.turns),2) AS avg_turns,
       round(percentile(l.llm_ms + coalesce(t.tool_ms,0),0.5)/1000.0,1)  AS median_wait_seconds,
       round(percentile(l.llm_ms + coalesce(t.tool_ms,0),0.75)/1000.0,1) AS p75_wait_seconds
FROM llm l JOIN bad b USING (trace_id) LEFT JOIN tool t USING (trace_id)
GROUP BY cohort ORDER BY cohort
```

| cohort | sessions | avg_turns | median_wait_seconds | p75_wait_seconds |
|---|---|---|---|---|
| clean session | 223711 | 3.82 | 8.6 | 17.4 |
| hit a silent tool failure | 19401 | 7.47 | 20.6 | 30.0 |

_2 row(s)_

### And in money, per session

2.7x the cost per session, and the waste column shows where it goes.

```sql
WITH bad AS (
  SELECT trace_id, max(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS hit
  FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY trace_id
)
SELECT CASE WHEN b.hit=1 THEN 'hit a silent tool failure' ELSE 'clean session' END AS cohort,
       count(*) AS sessions,
       round(avg(s.total_cost_usd),4) AS avg_cost_per_session,
       round(avg(s.wasted_cost_usd),4) AS avg_wasted_per_session
FROM main.tiffin_agent_waste.sessions_silver s JOIN bad b USING (trace_id)
GROUP BY cohort ORDER BY cohort
```

| cohort | sessions | avg_cost_per_session | avg_wasted_per_session |
|---|---|---|---|
| clean session | 223711 | 0.4622 | 0.0215 |
| hit a silent tool failure | 19401 | 1.2385 | 1.1433 |

_2 row(s)_

### Which agents serve dispatch-facing work, and what it costs them

`delivery-routing-agent` is the one a dispatcher talks to, and it is already running at
**112.9% of its monthly budget**.

```sql
SELECT agent_id, max(display_name) AS display_name, max(owner_team) AS owner_team,
       sum(sessions) AS sessions,
       round(sum(waste_usd)/90*365,0) AS waste_per_year,
       max(monthly_budget_usd) AS monthly_budget_usd,
       round(100*(sum(spend_usd)/3)/max(monthly_budget_usd),1) AS pct_of_monthly_budget
FROM main.tiffin_agent_waste.agent_waste_gold
GROUP BY agent_id ORDER BY waste_per_year DESC
```

| agent_id | display_name | owner_team | sessions | waste_per_year | monthly_budget_usd | pct_of_monthly_budget |
|---|---|---|---|---|---|---|
| order-support-agent | Order Support Agent | support-ai | 103853 | 54484.0 | 22000.0 | 90.7 |
| delivery-routing-agent | Delivery Routing Agent | logistics-ai | 61104 | 38218.0 | 14000.0 | 112.9 |
| fraud-detection-agent | Fraud Detection Agent | risk-ai | 31121 | 10186.0 | 5000.0 | 112.8 |
| customer-retention-agent | Customer Retention Agent | growth-ai | 21702 | 5040.0 | 1800.0 | 84.7 |
| menu-recommendation-agent | Menu Recommendation Agent | discovery-ai | 14329 | 803.0 | 400.0 | 42.3 |
| merchant-onboarding-agent | Merchant Onboarding Agent | merchant-ai | 37891 | 687.0 | 600.0 | 55.9 |

_6 row(s)_

## The chain, stated plainly

1. A tool contract is broken, and the failure returns HTTP 200 (`02_silent_failure_proof.md`).
2. The agent retries. Turns roughly double, 3.82 to 7.47.
3. The dispatcher's median wait rises from 8.6 to 20.6 seconds, or the answer never arrives at
   all: in the recorded guard-off run the agent never reached page 2 and never surfaced
   `D-4410315, 18 minutes late` (`05_agent_ab_run.md`).
4. **Assumption, not a finding:** a late delivery surfaced after the reroute window has closed
   cannot be intervened on, so it stays late. Converting step 3 into a change in on-time
   delivery rate requires the operator's own window and intervention-success data.

Steps 1 to 3 are measured. Step 4 is the operator's number to supply, and the guard moves steps
1 to 3 regardless of what that coefficient turns out to be.

## Who is asking, and how often they get no straight answer

_Added 2026-10-20 17:58 UTC, same warehouse and catalog as above._

The cohort split at the top of this file is fleet-wide. Dual-audience framing needs it per agent,
because the two agents a human actually talks to are not equally affected. `order-support-agent`
is the one an end customer reaches when asking where their order is; `delivery-routing-agent` is
the one a dispatcher asks about courier runs.

```sql
WITH bad AS (
  SELECT trace_id, max(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS hit
  FROM main.tiffin_agent_waste.tool_calls_silver GROUP BY trace_id
)
SELECT s.agent_id,
       count(*) AS sessions,
       sum(b.hit) AS blind_sessions,
       round(100.0*sum(b.hit)/count(*),1) AS pct_blind,
       round(sum(b.hit)/90.0,1) AS blind_per_day
FROM main.tiffin_agent_waste.sessions_silver s JOIN bad b USING (trace_id)
GROUP BY s.agent_id
ORDER BY blind_sessions DESC
```

| agent_id | sessions | blind_sessions | pct_blind | blind_per_day |
|---|---|---|---|---|
| order-support-agent | 93254 | 10198 | 10.9 | 113.3 |
| delivery-routing-agent | 55723 | 4439 | 8.0 | 49.3 |
| merchant-onboarding-agent | 34004 | 2033 | 6.0 | 22.6 |
| menu-recommendation-agent | 12993 | 1836 | 14.1 | 20.4 |
| customer-retention-agent | 19368 | 895 | 4.6 | 9.9 |
| fraud-detection-agent | 27770 | 0 | 0.0 | 0.0 |

_6 row(s)_

The blind column sums to 19,401, which is the fleet-wide figure at the top of this file, and the
sessions column sums to 243,112. The two human-facing agents account for 14,637 of the 19,401.

### Waiting longer is not the same as being told something false

A blind conversation is not automatically a wrong answer. Sometimes the agent retries and
eventually gets what it was denied, so the only cost is the wait. Sometimes the tool never
returns at all and the agent answers from whatever it already had. Those are different harms and
the slide claims both, so both are counted here.

**Definition.** A conversation is counted as `answered_without_the_data` when some tool it called
failed the schema check and that same tool never returned successfully anywhere in the same
conversation. It is a lower bound on wrong answers, not an upper one: the agent may also have
been wrong in conversations where a retry did eventually succeed, and it may occasionally have
recovered the same fact from a different tool.

```sql
WITH per_tool AS (
  SELECT trace_id, tool_server, tool_name,
         MAX(CASE WHEN shape_mismatch THEN 1 ELSE 0 END) AS had_fail,
         MAX(CASE WHEN NOT shape_mismatch THEN 1 ELSE 0 END) AS had_ok
  FROM main.tiffin_agent_waste.tool_calls_silver
  WHERE agent_id = 'order-support-agent'   -- re-run with 'delivery-routing-agent'
  GROUP BY trace_id, tool_server, tool_name
)
SELECT COUNT(*) AS blind_sessions,
       SUM(never_got_data) AS answered_without_the_data,
       ROUND(100.0*SUM(never_got_data)/COUNT(*),1) AS pct_never_recovered
FROM (
  SELECT trace_id,
         MAX(CASE WHEN had_fail=1 AND had_ok=0 THEN 1 ELSE 0 END) AS never_got_data
  FROM per_tool GROUP BY trace_id
  HAVING MAX(had_fail)=1
)
```

`order-support-agent`:

| blind_sessions | answered_without_the_data | pct_never_recovered |
|---|---|---|
| 10198 | 1461 | 14.3 |

_1 row(s)_

`delivery-routing-agent`:

| blind_sessions | answered_without_the_data | pct_never_recovered |
|---|---|---|
| 4439 | 595 | 13.4 |

_1 row(s)_

### The per-day figures used on the deck

Divide by the 90-day window. One conversation is one trace.

| | 90 days | per day |
|---|---|---|
| Customer asks, no straight answer (Order Support) | 10,198 | 113 |
| of which the agent retried and got there | 8,737 | 97 |
| of which the tool never returned | 1,461 | 16 |
| Dispatcher asks, no straight answer (Delivery Routing) | 4,439 | 49 |
| of which the agent retried and got there | 3,844 | 43 |
| of which the tool never returned | 595 | 7 |
| **Both audiences, no straight answer** | **14,637** | **163** |
| **Both audiences, answered without the data** | **2,056** | **23** |

The recorded A/B run in `05_agent_ab_run.md` is one of the 595: the page-2 call was rejected, the
agent never retried it, and `D-4410315, 18 minutes late` never reached the dispatcher.
