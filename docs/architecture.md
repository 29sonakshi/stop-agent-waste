# Architecture

![Pre-Flight architecture](architecture.png)

Five stages, left to right, with the loop closing along the bottom. The same flow is below as
Mermaid source so it is readable as text, diffable in review, and editable without a drawing
tool, followed by the medallion detail inside the Lakehouse stage.

## 1. The closed loop, as source

Source: [`architecture-overview.mmd`](architecture-overview.mmd)

```mermaid
flowchart LR
  classDef agents fill:#FFF1EE,stroke:#FF3621,stroke-width:2px,color:#1B3139
  classDef gw     fill:#2272B4,stroke:#1A5A8F,color:#FFFFFF
  classDef lake   fill:#F7F6F3,stroke:#D8D3CC,color:#1B3139
  classDef pipe   fill:#EAF4EF,stroke:#00A972,stroke-width:2px,color:#1B3139
  classDef ml     fill:#F7F6F3,stroke:#2272B4,stroke-width:2px,color:#1B3139
  classDef serve  fill:#FF3621,stroke:#C72A19,color:#FFFFFF
  classDef pg     fill:#FFFFFF,stroke:#00A972,stroke-width:2px,color:#1B3139

  A["<b>6 AI agents</b><br/>11 MCP tool servers<br/>$528K/yr spend"]:::agents
  G["<b>AI Gateway</b><br/>one OTel span<br/>per tool call"]:::gw
  D["<b>UC Delta</b><br/>2.6M spans<br/>90 days"]:::lake
  P["<b>Lakeflow pipeline</b><br/>args sent vs declared<br/>MCP schema · 15 tables"]:::pipe
  L["<b>1.3M labelled calls</b><br/>no human labelling"]:::pipe
  T["<b>MLflow</b><br/>LOFO AUC 0.825<br/>vs 0.604 rules"]:::ml
  S["<b>Model Serving</b><br/>coerce · allow<br/>p50 0.3s"]:::serve
  B["<b>Lakebase Postgres</b><br/>fix queue + audit"]:::pg
  N["<b>Genie</b><br/>NL analytics"]:::pg
  W["<b>Databricks App</b><br/>agent + fix queue"]:::lake

  A --> G --> D --> P --> L --> T --> S
  S -->|"verdict BEFORE dispatch"| A
  P --> B
  P --> N
  S --> B
  B --> W
  S --> W
```

The arrow back from Model Serving to the agents is the point of the project. Everything else in
this space is after-the-fact analysis. Here the traces the agents emit train the model that
guards those same agents, and the verdict arrives before the tool call is dispatched.

Unity Catalog governs every box: the raw spans, all 15 pipeline tables, model lineage, the
serving endpoint, the Lakebase project, and the Genie space.

## 2. Medallion detail

Source: [`data-flow.mmd`](data-flow.mmd)

```mermaid
flowchart TB
  classDef raw    fill:#F7F6F3,stroke:#D8D3CC,color:#1B3139
  classDef bronze fill:#FDF3E7,stroke:#B45309,color:#1B3139
  classDef silver fill:#F1F5F9,stroke:#64748B,color:#1B3139
  classDef gold   fill:#EAF4EF,stroke:#00A972,stroke-width:2px,color:#1B3139
  classDef out    fill:#FFFFFF,stroke:#2272B4,stroke-width:2px,color:#1B3139

  R["raw_otel_spans<br/>2.6M AI Gateway spans · VARIANT"]:::raw

  subgraph BR["BRONZE &nbsp; flatten and reference"]
    direction LR
    B1["bronze_spans<br/>streaming table"]:::bronze
    B2["bronze_tool_catalog<br/>declared inputSchema"]:::bronze
    B3["bronze_bug_registry<br/>seeded failure modes"]:::bronze
    B4["bronze_model_pricing<br/>token prices"]:::bronze
  end

  subgraph SI["SILVER &nbsp; one row per tool call, labelled"]
    direction LR
    S1["tool_calls_silver<br/>args sent vs declared schema<br/>mismatch_class · shape_mismatch"]:::silver
    S2["llm_turns_silver<br/>tokens per turn"]:::silver
    S3["tool_recovery_silver<br/>retry signatures"]:::silver
    S4["sessions_silver"]:::silver
  end

  subgraph GO["GOLD &nbsp; money and features"]
    direction LR
    G1["agent_waste_gold<br/>spend and waste per agent"]:::gold
    G2["tool_waste_gold<br/>waste per tool server"]:::gold
    G3["call_risk_features_gold<br/>the training set"]:::gold
    G4["fix_queue<br/>ranked by dollars"]:::gold
  end

  M["MLflow model<br/>tool_call_risk"]:::out
  P["Lakebase Postgres<br/>operational serving"]:::out
  N["Genie space<br/>NL analytics"]:::out

  R --> B1
  B1 --> S1
  B2 --> S1
  B3 --> S1
  B1 --> S2
  S1 --> S3
  S2 --> S4
  S1 --> G2
  S3 --> G2
  S2 --> G1
  B4 --> G1
  S1 --> G3
  G2 --> G4
  G3 --> M
  G4 --> P
  G1 --> N
  G2 --> N
```

### Where the work happens

`tool_calls_silver` is the table that matters. It joins the spans against
`bronze_tool_catalog`, which holds the `inputSchema` each MCP server declared through
`tools/list`, and compares every argument the agent actually sent against what the server said
it accepts. The output is `mismatch_class` and `shape_mismatch` per call, which is where the
1.3M training labels come from. No human labelled anything: the server's own published contract
is the ground truth.

The SQL is in [`../pipeline/03_silver_tool_calls.sql`](../pipeline/03_silver_tool_calls.sql).

### Two serving paths, on purpose

`agent_waste_gold` and `tool_waste_gold` stay in Delta and are what Genie reads, because
"how much are we wasting and where" is an analytical question over 1.3M rows.

`fix_queue` is pushed into Lakebase Postgres, because "what should my team fix first" is a point
lookup by `owner_team` that an app runs on every page view. Different access pattern, different
store, same governed numbers. The Postgres totals reconcile exactly to the Delta totals.
