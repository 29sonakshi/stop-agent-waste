# Pre-Flight — stopping silent AI-agent waste in food-delivery operations

**Tokens burned. Time lost. Nothing logged.**

Tiffin is a food-delivery marketplace. Six AI agents run its operations — dispatch, order
support, fraud, merchant onboarding, retention, menu discovery — and they do their work by
calling eleven MCP tool servers through the Databricks AI Gateway.

One in five dollars of their token budget is wasted, and no dashboard at Tiffin can see it.

| | |
|---|---|
| **Industry** | Food delivery / on-demand logistics |
| **Problem** | MCP tool calls that fail while returning HTTP 200, so agents retry blindly and burn tokens |
| **Measured impact** | **$109,418/yr 💸 wasted** out of **$528,421/yr 💸** agent spend — **20.7%** |
| **Target users** | The platform team that owns the agent budget; the tool-server owners who get a money-ranked fix queue |
| **What it does** | Scores every tool call *before dispatch* and returns `coerce` / `warn` / `allow` in ~0.3 s |

Every number above is computed from trace data in Unity Catalog, not estimated. The queries that
produce them are in [`evidence/`](evidence/) with their real output committed.

---

## The problem, concretely

`delivery-tracking-api` was upgraded to **emit** v2 pagination tokens. Its request validator still
only **accepts** v1. So when the dispatch agent asks for page 2 using the token the server just
handed it, the server replies:

```
HTTP 200 OK
"Your input couldn't be processed: TypeError in handler"
```

The call failed. The status code says it succeeded — truthfully, in that a request arrived and a
reply was sent. The failure is only expressible in prose, in the body.

So:

- **Error dashboards count status codes.** They see 100% success. Nobody is paged.
- **The agent notices it didn't get what it wanted.** It retries, reshapes the argument, guesses.
- **Each retry is real tokens and real latency.** The failure is invisible; the bill is not.

In this corpus **every one of 1,300,441 tool calls returns HTTP 200**, including all 84,171
genuine failures — proven in [`evidence/02_silent_failure_proof.md`](evidence/02_silent_failure_proof.md).
There is nothing in the status column that separates broken calls from healthy ones.

And it is not only money. In the demo scenario the late delivery sits on **page 2**, so the
unguarded agent doesn't merely cost more — it answers the dispatcher **wrongly**.

## The insight

The evidence was already being emitted and nobody was reading it. Every AI Gateway OTel span
carries both:

- `gen_ai.tool.call.arguments` — what the agent actually sent, and
- the `inputSchema` the MCP server itself declared via `tools/list`.

Comparing those two tells you the call is doomed **without needing an error at all**. Doing that
comparison *before dispatch* turns detection into prevention.

> Databricks' own engineering team found seven bugs of exactly this shape and fixed them in about
> an hour — [write-up here](https://www.databricks.com/blog/how-we-eliminated-1-million-year-wasted-ai-agent-spend-one-hour).
> That is diagnosis, after the money is spent. This project closes the loop: the traces these
> agents emit train the model that stops the *next* wasted call.

---

## The end-to-end journey

```
  ┌──────────────┐   OTel spans    ┌──────────────┐   compare args vs      ┌──────────────┐
  │  6 AI agents │ ──────────────▶ │ Unity Catalog│   declared MCP schema  │   Lakeflow   │
  │ 11 MCP tools │                 │  Delta 2.6M  │ ─────────────────────▶ │  SDP 15 tbls │
  └──────────────┘                 │    spans     │                        │ bronze→gold  │
         ▲                         └──────────────┘                        └──────┬───────┘
         │                                                                        │
         │  coerce / warn / allow                              1.3M labelled calls│
         │  before the call runs                                (no human labels) │
         │                                                                        ▼
  ┌──────┴───────┐                 ┌──────────────┐                        ┌──────────────┐
  │Model Serving │ ◀────────────── │  UC Model    │ ◀───────────────────── │    MLflow    │
  │ ~0.3s, fails │   registered    │  Registry    │    LOFO AUC 0.825      │   training   │
  │     open     │                 │     v1       │                        └──────────────┘
  └──────────────┘                 └──────────────┘
         │
         │  every verdict appended                    ┌──────────────┐     ┌──────────────┐
         └──────────────────────────────────────────▶ │   Lakebase   │ ──▶ │ Databricks   │
                                                      │  Postgres    │     │     App      │
            gold fix queue ────── pushed ───────────▶ │ fix_queue +  │     │  (Streamlit) │
                                                      │guard_decisions     └──────────────┘
                                                      └──────────────┘
            gold tables ──────────────────────────────────────────────────▶  Genie space
                                                                             (NL analytics)
```

| Stage | Built with | Where |
|---|---|---|
| **Lakeflow** | Spark Declarative Pipeline, 8 SQL files, bronze streaming tables → silver → gold materialized views (15 tables) | [`pipeline/`](pipeline/) |
| **Unity Catalog** | `main.tiffin_agent_waste` — governs raw spans, all pipeline tables, the model, lineage, and billing attribution | [`evidence/04`](evidence/04_unity_catalog_governance.md) |
| **Lakebase** | Postgres project `tiffin-preflight`; `tiffin.fix_queue` (read path) + `tiffin.guard_decisions` (write path, appended live by the app) | [`notebooks/11`](notebooks/11_lakebase_serving.py), [`evidence/07`](evidence/07_lakebase_serving.md) |
| **ML** | MLflow `HistGradientBoostingClassifier`, custom PyFunc, registered to UC, behind Model Serving | [`notebooks/09`](notebooks/09_train_model.py), [`evidence/08`](evidence/08_model_metrics.md) |
| **Genie** | Genie space over the gold tables for natural-language analysis of waste | [space](https://e2-dogfood.staging.cloud.databricks.com/genie/rooms/01f1c816b3e4161f9560d92ae0fc4ab4) |
| **Databricks App** | Streamlit. Runs the live agent loop with the guard inline, and serves the fix queue from Lakebase | [`app/`](app/) |

**It is one journey, not six demos.** The app's guard calls the served model; the served model was
trained on labels the pipeline produced; the pipeline reads spans the agents emit; the verdicts the
app produces are written back to Lakebase; Genie reads the same governed gold tables. Remove any
stage and the next one has nothing to stand on.

---

## Results

### The agent, guard off vs guard on

Same dispatcher question — *"Which of courier CR-4172's deliveries are running late?"* — against
the live Claude endpoint and the live guard. Full traces in
[`evidence/05_agent_ab_run.md`](evidence/05_agent_ab_run.md).

| | guard OFF | guard ON |
|---|---|---|
| turns | 6 | **3** |
| tokens | 9,636 | **4,258** |
| answered the question | **no** | **yes** |

The guarded run used 5,378 fewer tokens. But the headline is the last row: unguarded, the agent
retried the same malformed token four times, gave up, and never reached the late delivery. Guarded,
it got `coerce p=0.8919`, rewrote the token from the server's own declared schema, and answered
correctly on the first attempt — **D-4410315, 18 minutes late**.

### The model

Scored by **leave-one-bug-family-out**: hold out an entire failure mode and ask the model to flag
something it has never seen.

| | AUC |
|---|---|
| **Pre-Flight model (mean over folds)** | **0.825** |
| Hand-written rule baseline | 0.604 |
| Error-log baseline | **0.000** — there are no errors to key on |
| Worst single fold | **0.336** — below random; see limitations |

### Prioritisation: fix by money, not by count

Cost per failed call varies **102x** across tools. Ranking by error count puts
`update_merchant` (1,167 failures, $0.02 each) above `list_deliveries` (3,844 failures, $2.18
each). Ranking by money inverts it. That ordering is what Lakebase serves to the app.

### What it costs to run

~**$170/month 💸** for the guard (the serving endpoint). Measured from `system.billing.usage`, not
list-price arithmetic — see [`evidence/04`](evidence/04_unity_catalog_governance.md). Against
$9,100/month 💸 of waste prevented, roughly **50x**. Per call: scoring costs about $0.0004 against
$0.02–$2.35 per prevented failure.

---

## Evidence — committed as text, not screenshots

| File | What it proves |
|---|---|
| [`01_pipeline_execution.md`](evidence/01_pipeline_execution.md) | The pipeline ran; every table with live row counts |
| [`02_silent_failure_proof.md`](evidence/02_silent_failure_proof.md) | All 1.3M calls return HTTP 200, failures included |
| [`03_business_impact.md`](evidence/03_business_impact.md) | The money, per agent and per tool, with the SQL |
| [`04_unity_catalog_governance.md`](evidence/04_unity_catalog_governance.md) | UC lineage edges + billing attribution |
| [`05_agent_ab_run.md`](evidence/05_agent_ab_run.md) | Full agent traces, guard off vs on, from a job run |
| [`06_guard_endpoint.md`](evidence/06_guard_endpoint.md) | Six live endpoint calls showing coerce / warn / allow + latency |
| [`07_lakebase_serving.md`](evidence/07_lakebase_serving.md) | Cell-by-cell Postgres output: schema, load, point lookup |
| [`08_model_metrics.md`](evidence/08_model_metrics.md) | MLflow metrics pulled live, including the weak fold |

---

## Decisions and trade-offs

**The guard never blocks.** Three actions, not two. `coerce` rewrites the argument only when the
server's declared schema says exactly how. `warn` flags but lets the call through — if the fix is
ambiguous, silently rewriting someone's argument is worse than letting it fail. And if the guard
endpoint is unreachable, `agent.py` catches it, emits `guard_error`, and **proceeds**. A guard that
takes the agent down is worse than the waste it prevents.

**The model reads no natural language.** It gets seven fields: tool server, tool name, argument
JSON, agent id, turn index, tokens spent so far, and how many times this tool was already called
this session. It never sees the user's question. The signal is in the *shape* of the call, so
feeding it prose would invite it to learn the wrong thing — and would make it a far more expensive
thing to run in the critical path.

**Leave-one-bug-family-out, not a random split.** An early iteration scored a near-perfect AUC on a
random split. That was a red flag, not a win: rows from one bug family are near-duplicates, so a
random split lets the model memorise the bug it is graded on. LOFO is a harsher and more honest
test, and it is the number quoted.

**Lakebase for the queue, Delta for the analysis.** The gold tables are the analytical source of
truth and Genie reads them. But the app page a tool-server owner opens all day is a point lookup by
owner team at interactive latency — an indexed Postgres read, not a warehouse scan. Different
access pattern, different store, same governed numbers. The Postgres total reconciles exactly to
the Delta total: `$109,418.46/yr`.

**Labels come from the contract, not from humans.** 1.3M labelled calls with zero hand-labelling,
because the MCP server's own declared `inputSchema` is the ground truth for whether a call was
well-formed. This is the part that makes the approach portable: any team already emitting Gateway
traces can point this at their catalog.

---

## Known limitations

- **Synthetic trace corpus.** The 2.6M spans are generated from a seeded bug registry
  ([`notebooks/01`](notebooks/01_generate_traces.py)), modelled on the real AI Gateway OTel schema.
  No customer data is used anywhere in this repo.
- **The MCP tool servers in the demo app are in-process Python mocks** — five of them, in
  `app/agent.py`. They exist so the agent loop can fail realistically on demand. The pipeline,
  model, endpoint, Lakebase, Genie space and app are all real.
- **This guard catches malformed calls, not wrong answers.** The `get_eta` bug returns HTTP 200
  with a plausible but wrong value (seconds where minutes were asked). The arguments are perfectly
  valid, so there is no shape tell — and that is the fold scoring 0.336. Stated rather than
  averaged away.
- **One bug family generalises poorly** (above). The mean of 0.825 is carried by the families that
  do generalise.
- **The guard is advisory on `warn`.** It flags and proceeds; it does not hold the call for a human.
- **Single workspace, single region.** No multi-region or HA story.

## Next steps

1. **Result-consistency features** — catch HTTP 200 wrong-value failures, the gap above.
2. **Wire the wrapper into a real MCP proxy** instead of in-process stubs, so the guard sits in
   front of genuine tool servers.
3. **Batch-score the trace history** to seed the fix queue retroactively, plus SQL alerts on waste
   thresholds per owner team.
4. **Close the loop on `guard_decisions`** — feed the audit trail back as training data so the
   model learns from its own verdicts.

---

## Repo layout

```
notebooks/     00–04 config, synthetic trace generation, validation, de-risking
               09    model training + LOFO evaluation + UC registration
               10    agent A/B harness (imports the shipping agent module)
               11    Lakebase operational serving
               12    Lakebase grants for the app service principal
pipeline/      8 SQL files — the Lakeflow Spark Declarative Pipeline
app/           the Databricks App: app.py (UI), agent.py (agent + guard), lakebase.py (Postgres)
evidence/      real execution output, committed as text
```

`app/agent.py` is imported by both the app and notebook `10`, so **what is tested is what ships**.

## Running it

```bash
# pipeline
databricks pipelines start-update --pipeline-id <id> --profile <profile>

# model training + evaluation + registration
databricks api post /api/2.2/jobs/runs/submit --json '{"run_name":"train", "tasks":[
  {"task_key":"t","notebook_task":{"notebook_path":"<path>/09_train_model"}}]}'

# Lakebase serving + grants
#   -> notebooks/11_lakebase_serving.py then notebooks/12_lakebase_grants.py

# the app
databricks apps deploy tiffin-preflight-demo --source-code-path /Workspace/<path>/demo_app
```

---

*Built on Databricks (e2-dogfood) with Claude Code. Synthetic data throughout; no customer data.*

## Submission artifacts

- **Deck:** [`Pre-Flight-deck.pdf`](Pre-Flight-deck.pdf) — the business-audience slides
  (also [`Pre-Flight-deck.txt`](Pre-Flight-deck.txt) as extracted text)
- **Live app:** https://tiffin-preflight-demo-6051921418418893.staging.aws.databricksapps.com
- **Genie space:** https://e2-dogfood.staging.cloud.databricks.com/genie/rooms/01f1c816b3e4161f9560d92ae0fc4ab4
