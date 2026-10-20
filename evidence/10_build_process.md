# Evidence 10: how this build was produced

_This file is about the construction process, not the solution. The model and pipeline design
decisions are argued in the notebooks and in the code comments; this is an account of the
workflow itself, which AI tooling was used, where it was a force multiplier, and where manual
judgment was required._

Identical content is in [`../BUILD.md`](../BUILD.md) and in the README, so it is findable
whichever way the repository is read.

A record of the build process itself: the workflow, the AI tooling, where it helped, where it was wrong, and the trade-offs made along the way. The product and modelling decisions are
argued in the code and notebooks; this file is about how the work got done.

Roughly 20 hours over six days on a Databricks dogfood workspace.

## Tooling

| | |
|---|---|
| **Claude Code** (Opus) | The whole build, conversationally. It drove the Databricks CLI, the SQL Statement API, the Jobs API, the Model Serving API and the Google Slides API directly rather than generating code for me to run. |
| **Databricks CLI + REST APIs** | Pipeline runs, job submissions, endpoint config, Lakebase credentials, app deploys. |
| **Streamlit `AppTest`** | Headless execution of the real `app.py` to catch errors before deploying. |
| **mermaid-cli** | Architecture diagrams rendered from committed source. |
| **Google Slides API** | The deck is built by script, so a layout change is a diff. |

## The workflow, and why the order mattered

Generate a synthetic trace corpus imitating the real AI Gateway OTel schema → build the Lakeflow
pipeline against it → discover the labels were wrong and fix the generator → train and evaluate →
serve → wrap an agent around the endpoint → add the audit trail.

These were not built in parallel, because each stage exposed a fault in the one before it. The
pipeline revealed that the generator's retry signature was wrong. Training revealed that the
pipeline's labels were separable for the wrong reason. The agent revealed that the model's
three-way verdict only had two behaviours behind it.

The loop that actually ran, dozens of times a day: make a change, run it as a real job, read the
output, write the output into `evidence/`. Anything not verified by output was treated as not
done.

## Where the AI genuinely changed how the work got done

**Throwaway verification became free.** Most of the real work was answering "does this actually
behave the way I think". Does `variant_get` resolve a dotted OTel key, does
`serving-endpoints query` forward a `tools` payload, does the app's service principal exist as a
Postgres role yet? Each answer is a two-minute probe that is not worth writing by hand, so
normally you assume instead. Here I checked, and three of those assumptions were wrong.

**Claims got re-queried instead of remembered.** 2.6M spans and 15 tables are only tractable if
every number is recomputed rather than recalled. That habit is why `evidence/` exists at all:
every figure in the deck is a committed query result.

**Attacking my own results was cheap enough to do routinely.** The highest-value use was asking
it to try to break a number I liked. An early model scored near 1.0 on a random split; treating
that as a red flag rather than a win is what produced the leave-one-bug-family-out protocol and
the anti-leakage assertions.

**The deck became code.** Built through the Slides API from a script, so the architecture diagram
could be rebuilt four times without each attempt costing an afternoon of dragging boxes.

## Where it was wrong, and what that cost

**It built to the checklist.** The first Lakebase layer was a nine-row table copied out of Delta
purely to satisfy a requirement. I removed it. The second attempt was scoped deliberately against
what Model Serving inference logs already capture, and that version earns its place. The test I
should have applied first: *would removing this break a feature?*

**It trusted its own synthetic data.** A correlation between tool failures and escalation rate was
presented to me as a finding. Checking the generator showed the correlation had been hand-written
into it, so reading it back was circular. Those figures are now labelled assumptions wherever they
appear, and the operational numbers in the business case are restricted to derived ones.

**It measured the wrong thing first.** The initial dispatcher-wait metric summed tool latency and
showed bug sessions as *faster*, because validator rejections return quickly and clean sessions
have a long tail of genuinely slow calls. The honest measure is LLM turn latency plus tool
latency, at the median.

**Tooling failed silently and it did not notice.** `variant_get` with a dotted path returned
all-NULL while the pipeline reported success. A `# MAGIC %md` cell swallowed the code beneath it
and failed three cells later with an unrelated `NameError`. Both were caught by reading output
rather than trusting a green status.

**It needed to be told to stop.** Left alone it kept adding surface area: a fix-queue panel, a
second app tab, a third verdict with no behaviour behind it. All built, all cut.

## Trade-offs made in the building process

**Synthetic data over a real corpus.** Real Gateway traces are not mine to publish, and a seeded
bug registry means the ground truth is known, which is what makes leave-one-bug-family-out
possible. The cost is that the data can only be as honest as the generator, which is exactly the
trap described above.

**In-process tool mocks over a real MCP proxy.** Five Python mocks let the agent fail on demand
and kept the demo deterministic. The cost is that the tool servers are the least real part of the
build, and it is listed as a limitation rather than glossed over.

**Verified depth over breadth.** Time went into proving a smaller number of things properly,
nine evidence files of real output, rather than adding more components. Drift-triggered
retraining and row-level security on the verdict log are both listed as next steps rather than
half-built.

**Fail open rather than fail safe.** If the guard endpoint is unreachable the call proceeds. A
governance layer that can take the agents offline is worse than the waste it prevents. That is a
deliberate availability-over-enforcement choice.

## What stayed human

Which problem was worth solving. That a verdict with no distinct behaviour behind it should not be
called a verdict. That a component which can be removed without breaking a feature is decoration.
That an assumption has to be labelled as one even when it would make a better headline. The AI
was fast at producing and checking work; it was consistently wrong about when to stop.

