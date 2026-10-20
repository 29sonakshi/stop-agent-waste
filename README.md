# The late delivery the dispatcher never saw

**Tokens burned. Time lost. Nothing logged.**

Tiffin is a food-delivery marketplace. A dispatcher asks its delivery-routing agent a question
they ask a hundred times a day:

> *"Which of courier CR-4172's deliveries are running late?"*

They are not asking out of curiosity. They are asking so they can **act** while there is still
time: reroute the courier, warn the customer, comp the order. That answer has a decay window.

One dispatcher question in twelve runs into a broken tool contract. When it does, the answer
takes **20.6 seconds instead of 8.6**, or never arrives at all. In the run recorded in
[`evidence/05`](evidence/05_agent_ab_run.md), the agent never reached page 2 and never surfaced
`D-4410315, 18 minutes late`. The courier stayed late and nobody was told.

Nothing alerts, because the tool call that failed returned **HTTP 200**.

| | |
|---|---|
| **Industry** | Food delivery and on-demand logistics |
| **Problem** | MCP tool calls fail while returning HTTP 200, so the agent retries blindly and the dispatcher's intervention window closes |
| **Operational impact** | **1 dispatcher question in 12** affected; median wait **8.6 s → 20.6 s**; cost per session **$0.46 💸 → $1.24 💸** |
| **Platform impact** | **$109,418/yr 💸 wasted** of **$528,421/yr 💸** agent spend, which is **20.7%** |
| **Who owns the outcome** | **Head of Delivery Operations** (on-time rate, cost per order). Secondary: the platform team that owns the agent budget, and tool-server owners who get a money-ranked fix queue |
| **What it does** | Scores every tool call *before dispatch* and returns `coerce` or `allow` in ~0.3 s |

Operational figures are derived in [`evidence/09`](evidence/09_delivery_operations_impact.md).
The step from a missed intervention window to an on-time-delivery percentage needs the operator's
own data and is **not** claimed here.

Every number above is computed from trace data in Unity Catalog, not estimated. The queries that
produce them are in [`evidence/`](evidence/) with their real output committed.

---

## The problem, concretely

`delivery-tracking-api` was upgraded to **emit** v2 pagination tokens. Its request validator
still only **accepts** v1. So when the dispatch agent asks for page 2 using the token the server
just handed it, the server replies:

```
HTTP 200 OK
"Your input couldn't be processed: TypeError in handler"
```

The call failed. The status code says it succeeded, which is true in the narrow sense that a
request arrived and a reply was sent. The failure is only expressible in prose, in the body.

So three things follow:

- **Error dashboards count status codes.** They see 100% success. Nobody is paged.
- **The agent notices it did not get what it wanted.** It retries, reshapes the argument, guesses.
- **Each retry is real tokens and real latency.** The failure is invisible. The bill is not.

In this corpus **every one of 1,300,441 tool calls returns HTTP 200**, including all 84,171
genuine failures. That is proven in
[`evidence/02_silent_failure_proof.md`](evidence/02_silent_failure_proof.md): there is nothing in
the status column that separates broken calls from healthy ones.

And it is not only money. In the demo scenario the late delivery sits on **page 2**, behind the
malformed token, so the unguarded agent never reaches it. It does not fabricate an answer: it
reports honestly that it could not paginate. But the dispatcher asked which deliveries are
running late, waited six turns, and **never found out that one of them was 18 minutes late.**

## The insight

The evidence was already being emitted and nobody was reading it. Every AI Gateway OTel span
carries two things:

- `gen_ai.tool.call.arguments`, what the agent actually sent, and
- the `inputSchema` the MCP server itself declared via `tools/list`.

Comparing those two tells you the call is doomed **without needing an error at all**. Doing that
comparison *before dispatch* turns detection into prevention.

> Databricks' own engineering team found seven bugs of exactly this shape and fixed them in about
> an hour ([write-up](https://www.databricks.com/blog/how-we-eliminated-1-million-year-wasted-ai-agent-spend-one-hour)).
> That is diagnosis, after the money is spent. This project closes the loop: the traces these
> agents emit train the model that stops the *next* wasted call.

---

## Architecture

![Architecture](docs/architecture.png)


Read it left to right. The red arrow along the bottom is the point: the traces these agents
emit train the model that guards those same agents, and the verdict comes back before the tool
is ever called. Everything else in this space is after-the-fact analysis.

[`docs/architecture.md`](docs/architecture.md) has the same flow as Mermaid source, plus the
medallion detail showing where the labelling actually happens.

| Stage | Built with | Where |
|---|---|---|
| **Lakeflow** | Spark Declarative Pipeline, 8 SQL files, bronze streaming tables through silver to gold materialized views, 15 tables | [`pipeline/`](pipeline/) |
| **Unity Catalog** | `main.tiffin_agent_waste` governs raw spans, every pipeline table, the model, lineage, and billing attribution | [`evidence/04`](evidence/04_unity_catalog_governance.md) |
| **ML** | MLflow `HistGradientBoostingClassifier`, custom PyFunc, registered to UC, behind Model Serving | [`notebooks/09`](notebooks/09_train_model.py), [`evidence/07`](evidence/07_model_metrics.md) |
| **Lakebase** | Postgres `tiffin.guard_decisions`: every guard verdict appended at dispatch, then updated with the outcome. Two-phase write, point-read by `run_id` | [`notebooks/11`](notebooks/11_lakebase_verdict_log.py), [`evidence/08`](evidence/08_lakebase_verdict_log.md) |
| **Genie** | Genie space over the gold tables for natural-language analysis of waste | [space](https://e2-dogfood.staging.cloud.databricks.com/genie/rooms/01f1c816b3e4161f9560d92ae0fc4ab4) |
| **Databricks App** | Streamlit. Runs the live agent loop with the guard inline, turn by turn | [`app/`](app/) |

**It is one journey, not a set of demos.** The app's guard calls the served model. The served
model was trained on labels the pipeline produced. The pipeline reads the spans the agents emit.
Every verdict the guard returns is appended to Lakebase and updated with the outcome. Genie reads
the same governed gold tables. Remove any stage and the next one has nothing to stand on.

---

## Results

### The agent, guard off vs guard on

Same dispatcher question, *"Which of courier CR-4172's deliveries are running late?"*, against
the live Claude endpoint and the live guard. Full traces in
[`evidence/05_agent_ab_run.md`](evidence/05_agent_ab_run.md).

| | guard OFF | guard ON |
|---|---|---|
| turns | 6 | **3** |
| tokens | 9,899 | **4,344** |
| **found the late delivery** | **no** | **yes** |

The guarded run used 5,555 fewer tokens, but the last row is the headline. Unguarded, the agent
retried the same malformed token and never reached page 2. Guarded, it got `coerce p=0.8919`,
rewrote the token from the server's own declared schema, and answered correctly on the first
attempt: **D-4410315, 18 minutes late**.

Token counts move between runs because the loop is non-deterministic. `found_late` does not: the
tool server always rejects a v2 token, so unguarded the agent never gets to page 2.

A screenshot walkthrough of both runs is in
[`docs/demo-walkthrough.md`](docs/demo-walkthrough.md).

### The model

Scored by **leave-one-bug-family-out**, which holds out an entire failure mode and asks the model
to flag something it has never seen.

| | AUC |
|---|---|
| **This model, mean over folds** | **0.825** |
| Hand-written rule baseline | 0.604 |
| Error-log baseline | **0.000**, because there are no errors to key on |
| Worst single fold | **0.336**, below random. See limitations |

### What it costs the delivery operation

| | clean session | hit a silent failure |
|---|---|---|
| share of dispatcher sessions | 92% | **8%, or 1 in 12** |
| turns | 3.82 | **7.47** |
| **median dispatcher wait** | **8.6 s** | **20.6 s** |
| cost per session | $0.46 💸 | **$1.24 💸** |

Per-turn LLM latency is drawn from the same distribution for both cohorts, so the wait-time gap
is purely the extra turns rather than anything built into the data. Full queries in
[`evidence/09`](evidence/09_delivery_operations_impact.md).

### Prioritisation: fix by money, not by count

Cost per failed call varies **102x** across tools. Ranking by error count puts
`update_merchant` (1,167 failures at $0.02 each) above `list_deliveries` (3,844 failures at $2.18
each). Ranking by money inverts that order. Genie writes that query itself over
`tool_waste_gold`, so the fix list is a question anyone can ask in English rather than a report
somebody has to maintain.

### What it costs to run

About **$170/month 💸** for the guard itself, meaning the serving endpoint. Measured from
`system.billing.usage` rather than list-price arithmetic, in
[`evidence/04`](evidence/04_unity_catalog_governance.md). Against $9,100/month 💸 of waste
prevented, that is roughly **50x**. Per call, scoring costs about $0.0004 against $0.02 to $2.35
per prevented failure.

---

## Evidence, committed as text rather than screenshots

| File | What it proves |
|---|---|
| [`01_pipeline_execution.md`](evidence/01_pipeline_execution.md) | The pipeline ran. Every table with live row counts |
| [`02_silent_failure_proof.md`](evidence/02_silent_failure_proof.md) | All 1.3M calls return HTTP 200, failures included |
| [`03_business_impact.md`](evidence/03_business_impact.md) | The money, per agent and per tool, with the SQL |
| [`04_unity_catalog_governance.md`](evidence/04_unity_catalog_governance.md) | UC lineage edges and billing attribution |
| [`05_agent_ab_run.md`](evidence/05_agent_ab_run.md) | Full agent traces, guard off vs on, from a job run |
| [`docs/demo-walkthrough.md`](docs/demo-walkthrough.md) | Screenshots of the same comparison in the running app |
| [`06_guard_endpoint.md`](evidence/06_guard_endpoint.md) | Six live endpoint calls showing coerce and allow, with latency |
| [`07_model_metrics.md`](evidence/07_model_metrics.md) | MLflow metrics pulled live, including the weak fold |
| [`08_lakebase_verdict_log.md`](evidence/08_lakebase_verdict_log.md) | The Postgres audit trail: two-phase write, real rows, aggregate |
| [`09_delivery_operations_impact.md`](evidence/09_delivery_operations_impact.md) | What it costs the delivery operation: 1 in 12 sessions, 8.6 s → 20.6 s |

---

## How this was built

Roughly 20 hours over six days, on a dogfood workspace, built conversationally with
**Claude Code** driving the Databricks CLI, the SQL Statement API, the Jobs API and the Google
Slides API directly. Nothing here was generated in one pass; the order mattered and several
things had to be thrown away.

**The sequence.** Generate a synthetic trace corpus that imitates the real AI Gateway OTel schema
→ build the Lakeflow pipeline against it → discover the labels were wrong and fix the generator →
train and evaluate → serve → wrap an agent around the endpoint → add the audit trail. Each stage
exposed a problem in the one before it, which is why they were not built in parallel.

**Where the AI was a genuine force multiplier**

- *Throwaway verification.* Dozens of one-off probes to answer "does this actually work": does
  `variant_get` resolve a dotted OTel key, does `serving-endpoints query` forward a `tools`
  payload, does the app's service principal exist as a Postgres role yet. Each is a two-minute
  script that would not be worth writing by hand, and each caught a wrong assumption early.
- *Volume with discipline.* 2.6M spans and 15 pipeline tables are only tractable if every claim
  is re-queried rather than remembered. Every figure in `evidence/` is a committed query result
  for that reason.
- *Adversarial review of my own results.* The most valuable use was asking it to attack a number
  I liked. The model scoring near 1.0 on a random split was treated as a red flag rather than a
  win, which is what led to the leave-one-bug-family-out protocol.
- *Slides as code.* The architecture diagram is built through the Slides API from a script, so a
  layout change is a diff rather than an afternoon of dragging boxes.

**Where it got things wrong, and what that cost**

- *It built to the checklist.* The first Lakebase layer was a nine-row table copied out of Delta
  to satisfy a requirement. It was removed, and the second attempt was scoped against what Model
  Serving inference logs already capture, which is the version that earned its place.
- *It over-trusted its own synthetic data.* A correlation between tool failures and escalation
  rate was presented to me as a finding before anyone checked the generator, where that
  correlation had been hand-written. It is now labelled an assumption wherever it appears, and
  the operational numbers used in the business case are restricted to derived ones.
- *Silent failures in the tooling.* `variant_get` with a dotted path returned all-NULL while the
  pipeline reported success; a `# MAGIC %md` cell swallowed the code beneath it and failed three
  cells later with an unrelated `NameError`. Both were found by checking output rather than
  trusting a green status, which became the habit the `evidence/` directory exists to enforce.
- *It needed to be told to stop.* Left alone it kept adding surface area. The fix queue, a second
  app tab, and a third verdict that had no behaviour behind it were all built and then cut.

**The judgment that stayed human.** What problem was worth solving, that a verdict with no
distinct behaviour should not be called a verdict, that a guard which can take the agents offline
is worse than the waste it prevents, and that a component which can be removed without breaking a
feature is decoration. Those decisions are argued in the code comments and in the notebooks.

## Decisions and trade-offs

**Two actions, because there are only two behaviours.** `coerce` rewrites the argument, and only
when the server's declared schema says exactly how. `allow` lets the call through untouched.
The model itself emits a third borderline band it calls `warn`, but the wrapper has nothing
distinct to do with it: an ambiguous fix is one you should not apply silently, so a borderline
score is an allow. `score_call` collapses it and marks the call borderline for display. Naming a
third verdict that behaves identically to the second would be describing a distinction the system
does not make.

**The guard never blocks.** If the endpoint is unreachable, `agent.py` catches it, emits
`guard_error` and **proceeds**. A guard that takes the agent down is worse than the waste it
prevents.

**The model reads no natural language.** It gets seven fields: tool server, tool name, argument
JSON, agent id, turn index, tokens spent so far, and how many times this tool was already called
this session. It never sees the user's question. The signal is in the *shape* of the call, so
feeding it prose would invite it to learn the wrong thing, and would make it far more expensive to
run in the critical path.

**Leave-one-bug-family-out, not a random split.** An early iteration scored a near-perfect AUC on
a random split. That was a red flag rather than a win: rows from one bug family are
near-duplicates, so a random split lets the model memorise the bug it is then graded on. LOFO is a
harsher and more honest test, and it is the number quoted everywhere here.

**Lakebase records what the model could not.** Model Serving can log every request and response
automatically, and for the model's *recommendation* that is the better answer. It cannot see three
things, and those three are why the verdict log exists: whether the wrapper really rewrote the
argument (`coerce` is a recommendation, and `apply_coercion()` declines when the named parameter
is absent), whether the tool call then succeeded, and fail-open events where no request was ever
sent. The first iteration of this layer was a nine-row table copied out of Delta, which earned
nothing and was removed. The test that matters is whether removing a component breaks a feature.

**Labels come from the contract, not from humans.** 1.3M labelled calls with zero hand-labelling,
because the MCP server's own declared `inputSchema` is the ground truth for whether a call was
well-formed. This is the part that makes the approach portable: any team already emitting Gateway
traces can point it at their own catalog.

---

## Why this matters beyond the first $109K

Recovering the current waste is the smaller half of the argument. Tiffin runs six agents today
and is adding more, and every new agent multiplies the same exposure: more tool servers, more
contracts that can drift out of sync with the arguments agents send, and no way to see it happen
because none of it raises an error.

The reusable part is not the model, it is the method. Any team already emitting AI Gateway traces
has the arguments sent and the schema declared sitting in the same span, so the labels come free
and the same guard can sit in front of any agent. That turns agent reliability from something each
team discovers the expensive way into a platform capability with a budget, an owner and an audit
trail, which is what has to exist before an operations team will let an agent act on its own.

## Known limitations

- **Synthetic trace corpus.** The 2.6M spans are generated from a seeded bug registry
  ([`notebooks/01`](notebooks/01_generate_traces.py)), modelled on the real AI Gateway OTel schema.
  No customer data is used anywhere in this repo.
- **The MCP tool servers in the demo app are in-process Python mocks.** Five of them, in
  `app/agent.py`. They exist so the agent loop can fail realistically on demand. The pipeline,
  model, endpoint, Lakebase verdict log, Genie space and app are all real.
- **This guard catches malformed calls, not wrong answers.** The `get_eta` bug returns HTTP 200
  with a plausible but wrong value, seconds where minutes were asked for. The arguments are
  perfectly valid, so there is no shape tell, and that is the fold scoring 0.336. Stated here
  rather than averaged away.
- **One bug family generalises poorly**, per the point above. The mean of 0.825 is carried by the
  families that do generalise.
- **The guard is advisory, never blocking.** It rewrites or it proceeds. It does not hold a
  call for human review, and a borderline score proceeds rather than failing closed.
- **Single workspace, single region.** There is no multi-region or HA story.

## Next steps

1. **Result-consistency features** to catch HTTP 200 wrong-value failures, which is the gap above.
2. **Wire the wrapper into a real MCP proxy** instead of in-process stubs, so the guard sits in
   front of genuine tool servers.
3. **Batch-score the trace history** to seed the fix queue retroactively, plus SQL alerts on waste
   thresholds per owner team.
4. **Feed the verdict log back as training data.** The trail already records what the guard did
   and whether the call then worked, which is a labelled outcome of the model's own judgment.
   Training on it would let the model learn from its own decisions rather than only from traces.

---

## Repo layout

```
notebooks/   00-04  config, synthetic trace generation, validation, de-risking
             09     model training, LOFO evaluation, UC registration
             10     agent A/B harness (imports the shipping agent module) + verdict-log readback
             11     Lakebase verdict log: schema, grants, two-phase write self-test
pipeline/    8 SQL files, the Lakeflow Spark Declarative Pipeline
app/         the Databricks App: app.py (UI), agent.py (agent loop + guard), lakebase.py (Postgres)
evidence/    real execution output, committed as text
docs/        architecture diagrams as Mermaid source plus rendered PNGs
```

`app/agent.py` is imported by both the app and notebook `10`, so **what is tested is what ships**.

## Running it

```bash
# pipeline
databricks pipelines start-update --pipeline-id <id> --profile <profile>

# model training, evaluation and registration
databricks api post /api/2.2/jobs/runs/submit --json '{"run_name":"train", "tasks":[
  {"task_key":"t","notebook_task":{"notebook_path":"<path>/09_train_model"}}]}'

# Lakebase verdict log (table, grants, self-test)
#   run notebooks/11_lakebase_verdict_log.py

# the app
databricks apps deploy tiffin-preflight-demo --source-code-path /Workspace/<path>/demo_app
```

## Submission artifacts

- **Repo:** https://github.com/29sonakshi/stop-agent-waste
- **Deck:** [`Pre-Flight-deck.pdf`](Pre-Flight-deck.pdf), the business-audience slides, with
  [`Pre-Flight-deck.txt`](Pre-Flight-deck.txt) as extracted text
- **Live app:** https://tiffin-preflight-demo-6051921418418893.staging.aws.databricksapps.com
- **Genie space:** https://e2-dogfood.staging.cloud.databricks.com/genie/rooms/01f1c816b3e4161f9560d92ae0fc4ab4

## License

[MIT](LICENSE). Synthetic data throughout. No customer data.
