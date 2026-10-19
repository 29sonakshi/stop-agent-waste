# Databricks notebook source
# DBTITLE 1,01_generate_traces — simulate the AI Gateway
# 01_generate_traces.py
#
# WHAT THIS IS
#   A stand-in for Unity AI Gateway. In production, the Gateway emits OTel spans and
#   Zerobus persists them to a Unity Catalog Delta table within seconds -- there is no
#   file-landing step. So this notebook writes DIRECTLY to a Delta table whose schema
#   matches the real unity_gateway_otel_spans table.
#
# WHAT IT WRITES
#   main.tiffin_agent_waste.raw_otel_spans        flat, ONE ROW PER SPAN (the Gateway analogue)
#   main.tiffin_agent_waste.session_outcomes      business outcome per session
#   main.tiffin_agent_waste.ground_truth          hidden labels, eval only, never a feature
#
# TWO THINGS ARE DELIBERATELY *NOT* IN THE SPANS
#   1. bug family labels -- in reality a trace never tells you which bug family it belongs
#      to. Bug families are DISCOVERED by clustering. Labels live in ground_truth only.
#   2. arg_signature -- derived in the pipeline's silver layer by comparing the arguments
#      actually sent against the schema the MCP server declares. That derivation is real
#      transformation work, not a handed-over answer.
#
# ALL DATA IS SYNTHETIC.

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# DBTITLE 1,Run parameters
dbutils.widgets.text("sessions_per_day", str(SESSIONS_PER_DAY))
dbutils.widgets.text("days_per_batch", "10")
dbutils.widgets.dropdown("mode", "full", ["full", "smoke"])

MODE = dbutils.widgets.get("mode")
SESSIONS = int(dbutils.widgets.get("sessions_per_day"))
DAYS_PER_BATCH = int(dbutils.widgets.get("days_per_batch"))

if MODE == "smoke":
    SESSIONS = 200                      # fast end-to-end shakeout

print(f"mode={MODE}  sessions/day={SESSIONS}  window={WINDOW_START}..{WINDOW_END}")

# COMMAND ----------

# DBTITLE 1,Argument generation — the hero feature's source
# The model sends arguments; the server declares a schema. Most of the time they agree.
# When they disagree, the agent retries, reshapes, and burns tokens. The pipeline later
# compares what was sent against what was declared -- so the mismatch is COMPUTABLE,
# never guessed.
#
# Grounded in production: a real Gateway TOOL span was observed carrying
#   gen_ai.tool.call.arguments = {"to": "[\"placeholder@example.com\"]"}
#   gen_ai.tool.call.result    = "Your input couldn't be processed"
#   http.response.status_code  = 200
# -- a JSON array sent where a scalar was expected, failing with HTTP 200.

import json, random, uuid
from datetime import datetime, timedelta, timezone, date as _date

_IDS = {
    "order_id":    lambda: f"O-{random.randint(10**7, 10**8-1)}",
    "customer_id": lambda: f"C-{random.randint(10**6, 10**7-1)}",
    "merchant_id": lambda: f"M-{random.randint(10**5, 10**6-1)}",
    "payment_id":  lambda: f"PAY-{uuid.uuid4().hex[:12]}",
    "courier_id":  lambda: f"CR-{random.randint(1000, 9999)}",
    "delivery_id": lambda: f"D-{random.randint(10**6, 10**7-1)}",
    "item_id":     lambda: f"I-{random.randint(10**4, 10**5-1)}",
    "promo_id":    lambda: f"P-{random.randint(1000, 9999)}",
    "segment_id":  lambda: f"SEG-{random.randint(100, 999)}",
    "geo_id":      lambda: f"G-{random.randint(100, 999)}",
    "ticket_id":   lambda: f"T-{random.randint(10**5, 10**6-1)}",
}

_FIELD_POOL = ["name", "address", "phone", "status", "rating", "cuisine", "hours"]


def _declared_value(param, decl_type):
    """A value whose SHAPE matches what the server declares."""
    if param in _IDS:
        return _IDS[param]()
    if decl_type == "string":
        return random.choice(["downtown", "northside", "M-204881", "pending"])
    if decl_type == "int":
        return random.choice([10, 25, 50, 100])
    if decl_type == "double":
        return round(random.uniform(-122.5, 37.9), 6)
    if decl_type == "boolean":
        return random.choice([True, False])
    if decl_type == "csv_string":
        return ",".join(random.sample(_FIELD_POOL, random.randint(2, 4)))
    if decl_type == "flat_string":
        return random.choice(["vegan", "gluten_free", "spicy", "under_20"])
    if decl_type == "iso8601_string":
        return (datetime.now(timezone.utc) - timedelta(days=random.randint(1, 60))
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if decl_type == "v1_token_string":
        return f"v1-{uuid.uuid4().hex[:16]}"
    if decl_type == "nutrition_v1_object":
        return {"calories": random.randint(200, 900), "protein_g": random.randint(5, 40)}
    if decl_type.startswith("enum["):
        return random.choice(decl_type[5:-1].split(","))
    if decl_type == "array<string>":
        return [_IDS["merchant_id"]() for _ in range(random.randint(2, 5))]
    return "unspecified"


def _mismatched_value(param, decl_type, mismatch_type, attempt):
    """A value the model would plausibly send that the server REJECTS.

    Each branch is a shape the LLM has every reason to choose -- that is the whole
    point of the blog's conclusion that tools should adapt to how models call them.
    """
    if mismatch_type == "array_vs_scalar":
        # declared a comma-separated string; a list is the semantically natural choice
        return random.sample(_FIELD_POOL, random.randint(2, 4))
    if mismatch_type == "type_coercion":
        if decl_type == "int":
            return round(random.uniform(5, 95), 2)          # dollars, not cents
        if decl_type == "boolean":
            return random.choice(["true", "false"])          # stringified bool
        if decl_type == "string":
            return random.randint(10**6, 10**7)              # numeric id, leading zeros lost
        return str(_declared_value(param, decl_type))
    if mismatch_type == "format":
        if decl_type == "iso8601_string":
            return int(datetime.now(timezone.utc).timestamp()) - random.randint(10**4, 10**6)
        if decl_type == "v1_token_string":
            return f"v2:{uuid.uuid4().hex[:24]}=="           # v2 token, v1 endpoint
        return f"alt-{uuid.uuid4().hex[:8]}"
    if mismatch_type == "enum_case":
        return _declared_value(param, decl_type).lower() if decl_type.startswith("enum[") else "active"
    if mismatch_type == "shape_mismatch":
        if decl_type == "nutrition_v1_object":
            return {"energy_kcal": random.randint(200, 900), "protein": random.randint(5, 40)}
        if decl_type == "flat_string":
            return {"diet": "vegan", "max_price": 20}        # nested dict, flat expected
        return None                                          # required param omitted
    # unit_mismatch / rate_limit / timeout_split send VALID arguments -- the failure is
    # elsewhere (wrong unit in the response, or sheer call volume).
    return _declared_value(param, decl_type)


def build_arguments(tool_key, bug=None, attempt=0):
    """Returns (arguments_dict, shape_is_mismatched)."""
    decl = TOOL_BY_KEY[tool_key]["expected_args"]
    args, mismatched = {}, False
    for p, t in decl.items():
        if bug and bug["param"] == p and not bug["is_silent"] and attempt == 0:
            v = _mismatched_value(p, t, bug["mismatch_type"], attempt)
            mismatched = True
            if v is None:
                continue                                     # omitted required param
            args[p] = v
        elif bug and bug["param"] == p and not bug["is_silent"]:
            # on retry the model mutates the shape again -- argument churn, the signal
            # the pre-flight model learns from
            args[p] = (_declared_value(p, t) if attempt >= 2
                       else _mismatched_value(p, t, bug["mismatch_type"], attempt))
            mismatched = attempt < 2
        else:
            args[p] = _declared_value(p, t)
    return args, mismatched


def build_result(tool_key, bug, failed):
    """What the tool returned. Note: failures come back with HTTP 200."""
    if failed:
        q = bug["message_quality"]
        if q == "good":
            msg = (f"Invalid value for '{bug['param']}'. "
                   f"Expected {TOOL_BY_KEY[tool_key]['expected_args'].get(bug['param'])}.")
        elif q == "fair":
            msg = f"Could not process parameter '{bug['param']}'."
        else:
            msg = "Your input couldn't be processed: TypeError in handler"
        return {"content": [{"type": "text", "text": msg}]}, "tool_execution_error"
    if bug and bug["is_silent"]:
        # SILENT: valid-looking success, wrong unit. No error, nothing to grep for.
        return {"content": [{"type": "text",
                             "text": json.dumps({"eta": random.randint(240, 900),
                                                 "unit": "seconds"})}]}, None
    return {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}, None

# COMMAND ----------

# DBTITLE 1,Session simulator
from collections import defaultdict

AFFINITY_BY_AGENT = defaultdict(list)
CALL_WEIGHT = {}
for _a, _s, _t, _w in AGENT_TOOL_AFFINITY:
    AFFINITY_BY_AGENT[_a].append((_s, _t, _w))
    CALL_WEIGHT[(_a, _s, _t)] = _w

PRICE_BY_ENDPOINT = {m["endpoint"]: m for m in MODEL_PRICING}
BUGS_BY_AGENT = defaultdict(list)
for _b in BUG_FAMILIES:
    for _ag in _b["agent_ids"]:
        BUGS_BY_AGENT[_ag].append(_b)


def _weighted(pairs):
    tot = sum(w for _, w in pairs)
    r, c = random.random() * tot, 0.0
    for v, w in pairs:
        c += w
        if r <= c:
            return v
    return pairs[-1][0]


def _recovery_turns(bug, server):
    """Anchored on the published 4.6-12.1 turn range driven by error-message quality.
    A server that already coerces input neutralises most of the damage."""
    base = MSG_QUALITY_BASE[bug["message_quality"]] + MISMATCH_PENALTY[bug["mismatch_type"]]
    if server["has_input_coercion"]:
        base *= COERCION_RECOVERY_MULTIPLIER
    return max(1, int(round(random.gauss(base, 0.9))))


def _nano(ts):
    return int(ts.timestamp() * 1_000_000_000)


def _span(trace_id, span_id, parent, name, start, dur_ms, attrs,
          status_code="STATUS_CODE_OK", status_msg="", is_root=0):
    end = start + timedelta(milliseconds=max(1, dur_ms))
    return dict(
        record_id=str(uuid.uuid4()),
        time=start, date=start.date(),
        service_name=SERVICE_NAME, service_id="svc-001",
        trace_id=trace_id, span_id=span_id, trace_state="",
        parent_span_id=parent or None, flags=0,
        name=name, kind="SPAN_KIND_INTERNAL",
        start_time_unix_nano=_nano(start), end_time_unix_nano=_nano(end),
        attributes_json=json.dumps(attrs),
        dropped_attributes_count=0, dropped_events_count=0, dropped_links_count=0,
        status_message=status_msg, status_code=status_code,
        resource_schema_url="", span_schema_url="",
        scope_name="mlflow", scope_version="3.1.0",
        _is_root=is_root,
    )


def simulate_session(ts, agent, bug=None):
    """One agent conversation -> (spans, outcome, ground_truth)."""
    trace_id = uuid.uuid4().hex
    agent_id = agent["agent_id"]
    endpoint = agent["endpoint"]
    user = f"user_{random.randint(10_000, 99_999)}"
    opts = AFFINITY_BY_AGENT[agent_id]

    # Session archetypes create genuine CLASS OVERLAP. Without a long tail of
    # legitimately complex clean sessions, "long session" alone separates the classes
    # and the model learns nothing transferable.
    pag_key = None
    if bug:
        server = SERVER_BY_NAME[bug["server"]]
        if random.random() < 0.22:
            archetype, struggle = "lucky", random.randint(1, 2)
        else:
            archetype, struggle = "struggle", _recovery_turns(bug, server)
        n_turns = struggle + random.randint(1, 2)
    else:
        struggle = 0
        archetype = _weighted([("simple", 0.70), ("multi_step", 0.22), ("paginated", 0.08)])
        n_turns = {"simple":     lambda: random.randint(1, 3),
                   "multi_step": lambda: random.randint(4, 8),
                   "paginated":  lambda: random.randint(3, 12)}[archetype]()
        if archetype == "paginated":
            pag_key = _weighted([((s, t, w), w) for s, t, w in opts])[:2]

    spans = []
    t = ts
    first_in = random.randint(*TOKENS_FIRST_TURN)
    tot_in = tot_out = 0
    wasted_in = wasted_out = 0
    n_tool_calls = 0
    emitted_error = False

    # AGENT root span -- MLflow autolog emits one per invocation.
    root_id = uuid.uuid4().hex[:16]
    spans.append(_span(trace_id, root_id, None, f"invoke_agent {agent_id}", t, 1,
                       {A.SPAN_TYPE: "AGENT", A.AGENT_ID: agent_id, A.ENDUSER_ID: user,
                        A.OPERATION: "invoke_agent", A.AGENT_VERSION: agent["version"]},
                       is_root=1))

    for turn in range(n_turns):
        # Context accumulates, so each extra turn costs MORE than the last. This is why
        # an 8-turn recovery loop is expensive rather than merely slow.
        tin = int(first_in * (1 + TOKEN_GROWTH_PER_TURN * turn))
        tout = random.randint(*OUTPUT_TOKENS)
        tot_in += tin
        tot_out += tout
        if bug and 0 < turn < max(struggle, 1):
            # tokens spent purely on recovery -- kept split so they can be priced at the
            # real input/output rates rather than a blended average
            wasted_in += tin
            wasted_out += tout

        llm_id = uuid.uuid4().hex[:16]
        lat = random.randint(700, 4200)
        spans.append(_span(trace_id, llm_id, root_id, f"chat {endpoint}", t, lat, {
            A.SPAN_TYPE: "LLM", A.AGENT_ID: agent_id, A.ENDUSER_ID: user,
            A.OPERATION: "chat", A.ENDPOINT_NAME: endpoint,
            A.IN_TOKENS: tin, A.OUT_TOKENS: tout, A.TOKEN_COUNT: tin + tout,
            A.UC_SERVICE_TYPE: "model_service", A.LATENCY_MS: lat,
            A.HTTP_STATUS: 200, A.TURN_INDEX: turn,
            A.IN_MESSAGES: json.dumps([{"role": "user", "content": "<redacted>"}]),
            A.OUT_MESSAGES: json.dumps([{"role": "assistant", "content": "<redacted>"}]),
        }))
        t += timedelta(milliseconds=lat)

        # ---- tool calls for this turn ----
        in_struggle = bug is not None and turn < struggle
        if in_struggle:
            key = (bug["server"], bug["tool"])
            fan = 1
            if bug["mismatch_type"] == "timeout_split":
                fan = random.randint(6, 14)      # one batch call becomes many small ones
            elif bug["mismatch_type"] == "rate_limit":
                fan = random.randint(3, 6)       # hammering a rate-limited endpoint
            for i in range(fan):
                args, mismatched = build_arguments(key, bug, attempt=turn)
                failed = (not bug["is_silent"]) and mismatched
                result, err = build_result(key, bug, failed)
                emitted_error = emitted_error or bool(err)
                tl = int(abs(random.gauss(TOOL_BY_KEY[key]["sla_p99_ms"] * 0.6,
                                          TOOL_BY_KEY[key]["sla_p99_ms"] * 0.15)))
                a = {A.SPAN_TYPE: "TOOL", A.AGENT_ID: agent_id, A.ENDUSER_ID: user,
                     A.OPERATION: "execute_tool", A.TOOL_NAME: key[1],
                     A.TOOL_SOURCE: key[0], A.TOOL_ARGS: json.dumps(args),
                     A.TOOL_RESULT: json.dumps(result),
                     A.MCP_METHOD: "tools/call",
                     A.CONNECTION_TYPE: "EXTERNAL_MCP",
                     A.UC_SERVICE_TYPE: "mcp_service",
                     A.LATENCY_MS: tl,
                     A.HTTP_STATUS: 200,          # failures return 200 -- the whole point
                     A.TURN_INDEX: turn}
                if err:
                    a[A.ERROR_TYPE] = err
                spans.append(_span(trace_id, uuid.uuid4().hex[:16], llm_id, key[1], t, tl, a,
                                   status_code="STATUS_CODE_ERROR" if err else "STATUS_CODE_OK",
                                   status_msg=bug["name"] if err else ""))
                n_tool_calls += 1
                t += timedelta(milliseconds=tl)
        else:
            calls = ([pag_key] * random.randint(1, 3) if pag_key
                     else [_weighted([((s, tl2, w), w) for s, tl2, w in opts])[:2]
                           for _ in range(random.randint(0, 2))])
            for key in calls:
                args, _m = build_arguments(key)
                result, _e = build_result(key, None, False)
                tl = int(abs(random.gauss(TOOL_BY_KEY[key]["sla_p99_ms"] * 0.5,
                                          TOOL_BY_KEY[key]["sla_p99_ms"] * 0.2)))
                spans.append(_span(trace_id, uuid.uuid4().hex[:16], llm_id, key[1], t, tl, {
                    A.SPAN_TYPE: "TOOL", A.AGENT_ID: agent_id, A.ENDUSER_ID: user,
                    A.OPERATION: "execute_tool", A.TOOL_NAME: key[1],
                    A.TOOL_SOURCE: key[0], A.TOOL_ARGS: json.dumps(args),
                    A.TOOL_RESULT: json.dumps(result),
                    A.MCP_METHOD: "tools/call", A.CONNECTION_TYPE: "EXTERNAL_MCP",
                    A.UC_SERVICE_TYPE: "mcp_service", A.LATENCY_MS: tl,
                    A.HTTP_STATUS: 200, A.TURN_INDEX: turn}))
                n_tool_calls += 1
                t += timedelta(milliseconds=tl)

        t += timedelta(seconds=random.randint(2, 18))

    # ---- business outcome ----
    pricing = PRICE_BY_ENDPOINT[endpoint]
    cost = (tot_in / 1000.0) * pricing["in_per_1k"] + (tot_out / 1000.0) * pricing["out_per_1k"]
    if bug and archetype == "struggle":
        resolution = _weighted([("resolved", 0.55), ("escalated", 0.30), ("abandoned", 0.15)])
        csat = random.choice([2, 3, 3, 4, None])
    else:
        resolution = _weighted([("resolved", 0.90), ("escalated", 0.07), ("abandoned", 0.03)])
        csat = random.choice([4, 5, 5, 4, None])

    outcome = dict(
        trace_id=trace_id, agent_id=agent_id, session_date=ts.date(),
        resolution=resolution, customer_satisfaction=csat,
        total_turns=n_turns, total_tool_calls=n_tool_calls,
        total_tokens=tot_in + tot_out, total_cost_usd=round(cost, 6),
        escalated_to_human=(resolution == "escalated"),
    )
    waste_cost = ((wasted_in / 1000.0) * pricing["in_per_1k"]
                  + (wasted_out / 1000.0) * pricing["out_per_1k"])
    truth = dict(
        trace_id=trace_id,
        has_waste=bool(bug),
        is_silent=bool(bug and bug["is_silent"]),
        emitted_error=emitted_error,
        bug_id=bug["bug_id"] if bug else None,
        archetype=archetype,
        struggle_turns=struggle,
        wasted_tokens=wasted_in + wasted_out,
        wasted_cost_usd=round(waste_cost, 6),
    )
    return spans, outcome, truth

# COMMAND ----------

# DBTITLE 1,Generate and write (batched by day)
from pyspark.sql import types as T, functions as F

SPAN_SCHEMA = T.StructType([
    T.StructField("record_id", T.StringType()), T.StructField("time", T.TimestampType()),
    T.StructField("date", T.DateType()), T.StructField("service_name", T.StringType()),
    T.StructField("service_id", T.StringType()), T.StructField("trace_id", T.StringType()),
    T.StructField("span_id", T.StringType()), T.StructField("trace_state", T.StringType()),
    T.StructField("parent_span_id", T.StringType()), T.StructField("flags", T.IntegerType()),
    T.StructField("name", T.StringType()), T.StructField("kind", T.StringType()),
    T.StructField("start_time_unix_nano", T.LongType()),
    T.StructField("end_time_unix_nano", T.LongType()),
    T.StructField("attributes_json", T.StringType()),
    T.StructField("dropped_attributes_count", T.IntegerType()),
    T.StructField("dropped_events_count", T.IntegerType()),
    T.StructField("dropped_links_count", T.IntegerType()),
    T.StructField("status_message", T.StringType()), T.StructField("status_code", T.StringType()),
    T.StructField("resource_schema_url", T.StringType()),
    T.StructField("span_schema_url", T.StringType()),
    T.StructField("scope_name", T.StringType()), T.StructField("scope_version", T.StringType()),
    T.StructField("_is_root", T.IntegerType()),
])

# attributes -> VARIANT, and the nested OTel structs rebuilt, so the table matches the
# shape the real Gateway table has.
SELECT_EXPR = [
    "record_id", "time", "date", "service_name", "service_id", "trace_id", "span_id",
    "trace_state", "parent_span_id", "flags", "name", "kind",
    "start_time_unix_nano", "end_time_unix_nano",
    "parse_json(attributes_json) AS attributes",
    "dropped_attributes_count",
    "CAST(ARRAY() AS ARRAY<STRUCT<name:STRING,time_unix_nano:BIGINT>>) AS events",
    "dropped_events_count",
    "CAST(ARRAY() AS ARRAY<STRUCT<trace_id:STRING,span_id:STRING>>) AS links",
    "dropped_links_count",
    "STRUCT(status_message AS message, status_code AS code) AS status",
    "STRUCT(parse_json(TO_JSON(NAMED_STRUCT('service.name', service_name))) AS attributes, "
    "       0 AS dropped_attributes_count) AS resource",
    "resource_schema_url",
    "STRUCT(scope_name AS name, scope_version AS version) AS instrumentation_scope",
    "span_schema_url", "_is_root",
]

random.seed(SEED)
spark.sql(f"DROP TABLE IF EXISTS {RAW_SPANS_TABLE}")
spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.session_outcomes")
spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.ground_truth")

n_days = (WINDOW_END - WINDOW_START).days + 1
if MODE == "smoke":
    n_days = min(n_days, 3)

tot_spans = tot_sessions = tot_bug = 0
first_write = True

for b0 in range(0, n_days, DAYS_PER_BATCH):
    batch_days = range(b0, min(b0 + DAYS_PER_BATCH, n_days))
    spans, outcomes, truths = [], [], []

    for d in batch_days:
        day = datetime.combine(WINDOW_START + timedelta(days=d),
                               datetime.min.time(), timezone.utc)
        for _ in range(SESSIONS):
            ts = day + timedelta(seconds=random.randint(0, 86_399))
            agent = AGENT_BY_ID[_weighted([(a["agent_id"], a["avg_daily_sessions"])
                                           for a in AGENTS])]
            # a session hits a bug only if one exists on a tool this agent calls,
            # and only at that bug's own frequency
            bug = None
            for c in BUGS_BY_AGENT.get(agent["agent_id"], []):
                w = CALL_WEIGHT.get((agent["agent_id"], c["server"], c["tool"]), 0.0)
                if random.random() < w * c["frequency_pct"]:
                    bug = c
                    break
            sp, oc, gt = simulate_session(ts, agent, bug)
            spans += sp
            outcomes.append(oc)
            truths.append(gt)
            if bug:
                tot_bug += 1

    span_df = spark.createDataFrame(spans, schema=SPAN_SCHEMA).selectExpr(*SELECT_EXPR)

    if first_write:
        # IMPORTANT: create the table with variant shredding DISABLED.
        #
        # Serverless enables delta.feature.variantShredding automatically when it writes a
        # VARIANT column. Shredding is a reader-WRITER feature, so any client that does not
        # understand it refuses the ENTIRE table -- a SQL warehouse on the CURRENT channel
        # fails with DELTA_UNSUPPORTED_FEATURES_IN_CONFIG and cannot read even the non-VARIANT
        # columns. It also cannot be dropped after the fact: ALTER TABLE DROP FEATURE requires
        # the log retention period to elapse in real wall-clock time.
        #
        # Setting the property at CREATE time keeps the VARIANT columns exactly as the real
        # Gateway table has them, while leaving the table readable from warehouses, Genie,
        # and any pipeline channel.
        (span_df.writeTo(RAW_SPANS_TABLE)
                .tableProperty("delta.enableVariantShredding", "false")
                .partitionedBy(F.col("date"))
                .createOrReplace())
    else:
        span_df.writeTo(RAW_SPANS_TABLE).append()

    mode_w = "overwrite" if first_write else "append"
    (spark.createDataFrame(outcomes)
          .write.mode(mode_w).option("overwriteSchema", "true")
          .saveAsTable(f"{CATALOG}.{SCHEMA}.session_outcomes"))
    (spark.createDataFrame(truths)
          .write.mode(mode_w).option("overwriteSchema", "true")
          .saveAsTable(f"{CATALOG}.{SCHEMA}.ground_truth"))

    first_write = False
    tot_spans += len(spans)
    tot_sessions += len(outcomes)
    print(f"  days {batch_days.start:>2}-{batch_days.stop-1:<2}  "
          f"+{len(spans):>7,} spans  (running {tot_spans:,})")

print(f"\nDONE  sessions={tot_sessions:,}  spans={tot_spans:,}  "
      f"bug_sessions={tot_bug:,} ({100*tot_bug/max(tot_sessions,1):.1f}%)")

# COMMAND ----------

# DBTITLE 1,Summary
import json as _json
summary = spark.sql(f"""
  SELECT
    (SELECT COUNT(*) FROM {RAW_SPANS_TABLE})                                   AS spans,
    (SELECT COUNT(DISTINCT trace_id) FROM {RAW_SPANS_TABLE})                   AS traces,
    (SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.session_outcomes)                 AS sessions,
    (SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.ground_truth WHERE has_waste)     AS waste_sessions,
    (SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.ground_truth WHERE is_silent)     AS silent_sessions,
    (SELECT ROUND(SUM(wasted_cost_usd),2) FROM {CATALOG}.{SCHEMA}.ground_truth) AS waste_usd_window,
    (SELECT ROUND(SUM(total_cost_usd),2) FROM {CATALOG}.{SCHEMA}.session_outcomes) AS spend_usd_window
""").collect()[0].asDict()
days = n_days
summary["waste_usd_annualized"] = round(summary["waste_usd_window"] / days * 365, 2)
summary["spend_usd_annualized"] = round(summary["spend_usd_window"] / days * 365, 2)
summary["days"] = days
summary["waste_pct_of_spend"] = round(
    100.0 * summary["waste_usd_window"] / max(summary["spend_usd_window"], 1e-9), 1)
print(_json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(_json.dumps(summary, default=str))
