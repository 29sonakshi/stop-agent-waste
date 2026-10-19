# Databricks notebook source
# DBTITLE 1,00_config — The Reviewable Matrix
# 00_config.py — Project configuration for Tiffin Agent Waste Detection
# %run by all other notebooks in this project. Nothing is written here.
#
# STORY
#   Tiffin is a fictitious DoorDash-shaped food & grocery marketplace (18M consumers,
#   450K merchants, ~900 engineers) running 6 AI agents through Databricks AI Gateway.
#   When an MCP tool rejects a semantically reasonable argument, the agent does not fail
#   loudly -- it retries, guesses, and works around the problem, silently burning tokens.
#   Fleet waste: ~$585K/yr. The error log shows the failures that announced themselves;
#   the expensive ones are invisible.
#
#   Grounded in production reality: a real AI Gateway TOOL span was observed carrying
#   error.type = "tool_execution_error" alongside http.response.status_code = 200,
#   with arguments {"to": "[\"placeholder@example.com\"]"} -- a JSON array sent as a
#   string -- and the result "Your input couldn't be processed". That is this bug class,
#   in production, returning HTTP 200.
#
# ALL DATA IS SYNTHETIC. No real company, customer, or employee data.

# COMMAND ----------

# DBTITLE 1,Target objects & scale
from datetime import date

CATALOG = "main"
SCHEMA  = "tiffin_agent_waste"

# Traces land in a Delta table, mirroring how AI Gateway -> Zerobus actually works.
# There is no file-landing step for traces in the real product.
RAW_SPANS_TABLE      = f"{CATALOG}.{SCHEMA}.raw_otel_spans"
RAW_SPANS_VALIDATION = f"{CATALOG}.{SCHEMA}.raw_otel_spans_validation"

# Reference/business exports arrive as files, which IS realistic (MCP tools/list dumps,
# CI/CD exports, ticketing-system extracts) -- ingested with read_files.
REFERENCE_VOLUME = f"/Volumes/{CATALOG}/{SCHEMA}/reference"

WINDOW_START      = date(2026, 7, 15)
WINDOW_END        = date(2026, 10, 12)   # 90 days
SESSIONS_PER_DAY  = 3000
SEED              = 7
DERISK_SEED       = 11                   # step-1 stop-or-go sample
VALIDATION_SEED   = 101                  # different seed, long horizon, ground-truth means

SERVICE_NAME = "tiffin-agent-platform"

# COMMAND ----------

# DBTITLE 1,Verified OTel attribute keys
# Every key below was verified present in real Unity AI Gateway OTel spans.
# Referenced by BOTH the generator and the pipeline so they cannot drift apart.
#
# NOTE: these are FLAT DOTTED STRING keys inside a VARIANT column. Extraction therefore
# requires bracket notation -- variant_get(attributes, '$["gen_ai.tool.name"]', 'string').
# Dot-path notation resolves to a nested field that does not exist and returns NULL
# with NO error: the pipeline "succeeds" and every column is silently empty.

class A:
    # shared
    SPAN_TYPE        = "mlflow.spanType"            # AGENT | LLM | TOOL
    AGENT_ID         = "gen_ai.agent.id"
    ENDUSER_ID       = "enduser.id"
    OPERATION        = "gen_ai.operation.name"      # invoke_agent | chat | execute_tool
    LATENCY_MS       = "databricks.latency_ms"
    UC_SERVICE_TYPE  = "databricks.uc_service_type" # model_service | mcp_service
    HTTP_STATUS      = "http.response.status_code"
    ERROR_TYPE       = "error.type"
    # LLM
    ENDPOINT_NAME    = "databricks.endpoint_name"
    IN_TOKENS        = "gen_ai.usage.input_tokens"
    OUT_TOKENS       = "gen_ai.usage.output_tokens"
    TOKEN_COUNT      = "mlflow.tokenCount"
    IN_MESSAGES      = "gen_ai.input.messages"
    OUT_MESSAGES     = "gen_ai.output.messages"
    # TOOL
    TOOL_NAME        = "gen_ai.tool.name"
    TOOL_ARGS        = "gen_ai.tool.call.arguments"  # <-- the hero feature's source
    TOOL_RESULT      = "gen_ai.tool.call.result"
    TOOL_SOURCE      = "tool_source"                 # the MCP server identity
    MCP_METHOD       = "mcp.method.name"             # tools/call
    CONNECTION_TYPE  = "databricks.tool.connection_type"  # EXTERNAL_MCP
    # custom (only two -- no real equivalent observed)
    AGENT_VERSION    = "tiffin.agent_version"
    TURN_INDEX       = "tiffin.turn_index"

# COMMAND ----------

# DBTITLE 1,The 6 agents
# agent_id, display, endpoint, owner_team, domain, avg_daily_sessions, monthly_budget, launched, version
#
# monthly_budget_usd is calibrated against MEASURED spend from the generated window, so
# "budget utilisation" is a credible metric rather than a config artefact. Two agents are
# deliberately left slightly OVER budget -- including the live demo agent -- because
# recovering their wasted spend is what brings them back under.
AGENTS = [
    dict(agent_id="order-support-agent",        display="Order Support Agent",
         endpoint="system.ai.claude-sonnet-5", model="claude-sonnet-5",  owner_team="support-ai",
         domain="customer_support", avg_daily_sessions=1150, budget_usd=22000.0,
         launched=date(2026,2,18), version="2.1.0"),
    dict(agent_id="merchant-onboarding-agent",  display="Merchant Onboarding Agent",
         endpoint="system.ai.gemini-3-5-flash", model="gemini-3-5-flash", owner_team="merchant-ai",
         domain="merchant_ops", avg_daily_sessions=420, budget_usd=600.0,
         launched=date(2026,4,2),  version="1.3.4"),
    dict(agent_id="delivery-routing-agent",     display="Delivery Routing Agent",
         endpoint="system.ai.claude-sonnet-5", model="claude-sonnet-5",  owner_team="logistics-ai",
         domain="courier_ops", avg_daily_sessions=680, budget_usd=14000.0,
         launched=date(2026,3,10), version="1.4.2"),          # <-- LIVE DEMO AGENT
    dict(agent_id="fraud-detection-agent",      display="Fraud Detection Agent",
         endpoint="system.ai.claude-sonnet-5", model="claude-sonnet-5",  owner_team="risk-ai",
         domain="risk", avg_daily_sessions=350, budget_usd=5000.0,
         launched=date(2026,1,22), version="3.0.1"),
    dict(agent_id="customer-retention-agent",   display="Customer Retention Agent",
         endpoint="system.ai.gpt-5-2", model="gpt-5-2",          owner_team="growth-ai",
         domain="growth", avg_daily_sessions=240, budget_usd=1800.0,
         launched=date(2026,5,14), version="0.9.7"),
    dict(agent_id="menu-recommendation-agent",  display="Menu Recommendation Agent",
         endpoint="system.ai.gemini-3-5-flash", model="gemini-3-5-flash", owner_team="discovery-ai",
         domain="discovery", avg_daily_sessions=160, budget_usd=400.0,
         launched=date(2026,6,1),  version="1.0.3"),
]

LIVE_AGENT = "delivery-routing-agent"   # the one agent built for real (step 7)

# COMMAND ----------

# DBTITLE 1,The 11 MCP tool servers
# doc_quality drives recovery difficulty: how well the server tells the model what it wants.
# Grounded in the published observation that recovery took 4.6-12.1 turns depending on
# error-message quality.
TOOL_SERVERS = [
    dict(server="order-history-api",     owner_team="orders-platform",    doc_quality="fair",
         has_examples=True,  has_input_coercion=False, version="4.2.0", released=date(2026,6,30)),
    dict(server="payment-gateway",       owner_team="payments-platform",  doc_quality="poor",
         has_examples=False, has_input_coercion=False, version="7.1.3", released=date(2026,5,12)),
    dict(server="customer-profile-api",  owner_team="identity-platform",  doc_quality="good",
         has_examples=True,  has_input_coercion=True,  version="3.0.0", released=date(2026,4,1)),
    dict(server="merchant-catalog-api",  owner_team="merchant-platform",  doc_quality="poor",
         has_examples=False, has_input_coercion=False, version="5.4.1", released=date(2026,7,20)),
    dict(server="menu-service",          owner_team="merchant-platform",  doc_quality="poor",
         has_examples=False, has_input_coercion=False, version="2.9.0", released=date(2026,8,5)),
    dict(server="compliance-api",        owner_team="trust-platform",     doc_quality="good",
         has_examples=True,  has_input_coercion=True,  version="1.8.2", released=date(2026,3,15)),
    dict(server="courier-eta-api",       owner_team="logistics-platform", doc_quality="fair",
         has_examples=True,  has_input_coercion=False, version="2.4.0", released=date(2026,8,22)),
    dict(server="geolocation-service",   owner_team="logistics-platform", doc_quality="fair",
         has_examples=True,  has_input_coercion=False, version="6.0.5", released=date(2026,5,2)),
    dict(server="delivery-tracking-api", owner_team="logistics-platform", doc_quality="poor",
         has_examples=False, has_input_coercion=False, version="2.0.0", released=date(2026,9,3)),
    dict(server="analytics-aggregator",  owner_team="data-platform",      doc_quality="fair",
         has_examples=True,  has_input_coercion=False, version="3.7.1", released=date(2026,6,10)),
    dict(server="promo-engine",          owner_team="growth-platform",    doc_quality="good",
         has_examples=True,  has_input_coercion=False, version="1.2.0", released=date(2026,7,1)),
]

# COMMAND ----------

# DBTITLE 1,The tool catalog (MCP tools/list inputSchema)
# expected_args is what the server DECLARES via MCP tools/list. The pipeline compares the
# arguments actually sent against this to compute the mismatch -- so suggested_coercion is
# derived from the server's own schema, not guessed by the model.
TOOLS = [
    # order-history-api
    ("order-history-api","get_order","Fetch a single order by id",{"order_id":"string"},350),
    ("order-history-api","list_orders","List orders for a customer since a timestamp",
        {"customer_id":"string","since":"iso8601_string","limit":"int"},600),
    ("order-history-api","get_order_items","Line items for an order",{"order_id":"string"},300),
    # payment-gateway
    ("payment-gateway","create_refund","Issue a refund against a payment",
        {"payment_id":"string","amount_cents":"int","idempotency_key":"string"},800),
    ("payment-gateway","get_payment","Fetch payment detail",{"payment_id":"string"},300),
    ("payment-gateway","list_transactions","Transactions for a customer",
        {"customer_id":"string","limit":"int"},500),
    # customer-profile-api  (CLEAN -- the `allow` control in the live demo)
    ("customer-profile-api","get_customer","Customer profile by id",{"customer_id":"string"},200),
    ("customer-profile-api","get_addresses","Saved addresses",{"customer_id":"string"},200),
    ("customer-profile-api","get_preferences","Dietary/contact preferences",{"customer_id":"string"},200),
    # merchant-catalog-api
    ("merchant-catalog-api","get_merchant","Merchant detail, optional field projection",
        {"merchant_id":"string","fields":"csv_string"},400),
    ("merchant-catalog-api","list_merchants","Merchants in an area",
        {"geo_id":"string","limit":"int"},700),
    ("merchant-catalog-api","update_merchant","Update merchant status",
        {"merchant_id":"string","status":"enum[ACTIVE,PAUSED,SUSPENDED]"},500),
    # menu-service
    ("menu-service","get_menu","Full menu for a merchant",{"merchant_id":"string"},450),
    ("menu-service","search_menu","Search menu items with filters",
        {"merchant_id":"string","query":"string","filters":"flat_string"},600),
    ("menu-service","upsert_menu_item","Create or update a menu item",
        {"merchant_id":"string","item_id":"string","nutrition":"nutrition_v1_object"},700),
    # compliance-api  (CLEAN)
    ("compliance-api","check_merchant","Run compliance checks",{"merchant_id":"string"},900),
    ("compliance-api","get_doc_status","Document verification status",{"merchant_id":"string"},300),
    # courier-eta-api
    ("courier-eta-api","get_eta","Estimated time of arrival for a courier",
        {"courier_id":"string","destination":"string","unit":"enum[seconds,minutes]"},400),
    ("courier-eta-api","get_route","Planned route for a courier",{"courier_id":"string"},550),
    # geolocation-service
    ("geolocation-service","geocode","Address to coordinates",{"address":"string"},300),
    ("geolocation-service","reverse_geocode","Coordinates to address",
        {"lat":"double","lon":"double"},300),
    ("geolocation-service","distance_matrix","Pairwise distances",
        {"origins":"array<string>","destinations":"array<string>"},1200),
    # delivery-tracking-api
    ("delivery-tracking-api","list_deliveries","Paginated delivery list",
        {"courier_id":"string","page_token":"v1_token_string","limit":"int"},600),
    ("delivery-tracking-api","get_delivery","Single delivery detail",{"delivery_id":"string"},300),
    ("delivery-tracking-api","update_status","Update delivery status",
        {"delivery_id":"string","status":"string"},400),
    # analytics-aggregator
    ("analytics-aggregator","query_metrics","Single metric query",
        {"metric":"string","window":"string"},900),
    ("analytics-aggregator","batch_query","Batch metric query (30s server timeout)",
        {"metrics":"array<string>","window":"string","merchant_ids":"array<string>"},30000),
    # promo-engine
    ("promo-engine","create_promo","Create a promotion",
        {"name":"string","discount_pct":"double","active":"boolean"},500),
    ("promo-engine","target_segment","Resolve a targeting segment",{"segment_id":"string"},700),
    ("promo-engine","get_promo","Fetch a promotion",{"promo_id":"string"},250),
]

# COMMAND ----------

# DBTITLE 1,The 10 bug families
# is_silent=True  -> HTTP 200, no error.type, wrong RESULT. Invisible to every error log.
#                    This is the class the model must find and a GROUP BY cannot.
#
# message_quality is a GENERATOR PARAMETER ONLY -- it drives how many turns recovery takes.
# It is deliberately NOT a model feature: "none" only ever occurs on the silent family, so
# exposing it would hand the model the answer. The pre-call-knowable analogue the model may
# use is the SERVER's doc_quality (static, published, available before any call is made).
BUG_FAMILIES = [
    dict(bug_id="order_date_format", name="Order history since= ISO-vs-epoch",
         server="order-history-api", tool="list_orders", param="since",
         mismatch_type="format", is_silent=False, message_quality="fair",
         frequency_pct=0.22, introduced=date(2026,6,30), fixed=None,
         suggested_coercion="Convert ISO-8601 string to epoch milliseconds"),
    dict(bug_id="payment_missing_idempotency", name="Refund requires undocumented idempotency_key",
         server="payment-gateway", tool="create_refund", param="idempotency_key",
         mismatch_type="shape_mismatch", is_silent=False, message_quality="poor",
         frequency_pct=0.14, introduced=date(2026,7,8), fixed=None,
         suggested_coercion="Generate and inject a UUID idempotency_key"),
    dict(bug_id="merchant_enum_case", name="Merchant status enum is case-sensitive",
         server="merchant-catalog-api", tool="update_merchant", param="status",
         mismatch_type="enum_case", is_silent=False, message_quality="good",
         frequency_pct=0.18, introduced=date(2026,7,20), fixed=None,
         suggested_coercion="Upper-case the status value"),
    dict(bug_id="menu_schema_drift", name="Menu nutrition v2 field names rejected by v1",
         server="menu-service", tool="upsert_menu_item", param="nutrition",
         mismatch_type="shape_mismatch", is_silent=False, message_quality="poor",
         frequency_pct=0.11, introduced=date(2026,8,5), fixed=None,
         suggested_coercion="Map v2 nutrition keys to the v1 object shape"),
    dict(bug_id="courier_eta_units", name="ETA seconds-vs-minutes SILENT mismatch",
         server="courier-eta-api", tool="get_eta", param="unit",
         mismatch_type="unit_mismatch", is_silent=True, message_quality="none",
         frequency_pct=0.35, introduced=date(2026,8,22), fixed=None,
         suggested_coercion="Convert response unit to match the requested unit"),
    dict(bug_id="geo_rate_limit", name="Distance matrix rate-limit hammering",
         server="geolocation-service", tool="distance_matrix", param="origins",
         mismatch_type="rate_limit", is_silent=False, message_quality="fair",
         frequency_pct=0.09, introduced=date(2026,7,2), fixed=None,
         suggested_coercion="Batch origins and apply exponential backoff"),
    dict(bug_id="delivery_pagination_token", name="v2 page_token rejected by v1 endpoint",
         server="delivery-tracking-api", tool="list_deliveries", param="page_token",
         mismatch_type="format", is_silent=False, message_quality="poor",
         frequency_pct=0.28, introduced=date(2026,9,3), fixed=None,
         suggested_coercion="Rewrite v2 page_token into v1 token format"),
    dict(bug_id="analytics_batch_timeout", name="Batch query times out; agent splits into N calls",
         server="analytics-aggregator", tool="batch_query", param="merchant_ids",
         mismatch_type="timeout_split", is_silent=False, message_quality="fair",
         frequency_pct=0.16, introduced=date(2026,6,10), fixed=None,
         suggested_coercion="Chunk merchant_ids into batches under the 30s budget"),
    dict(bug_id="promo_bool_as_string", name="Promo active sent as string \"true\"",
         server="promo-engine", tool="create_promo", param="active",
         mismatch_type="type_coercion", is_silent=False, message_quality="good",
         frequency_pct=0.12, introduced=date(2026,7,1), fixed=None,
         suggested_coercion="Cast \"true\"/\"false\" to native boolean"),
    dict(bug_id="merchant_fields_csv", name="get_merchant fields= array sent, CSV string expected",
         server="merchant-catalog-api", tool="get_merchant", param="fields",
         mismatch_type="array_vs_scalar", is_silent=False, message_quality="poor",
         frequency_pct=0.31, introduced=date(2026,7,20), fixed=None,
         suggested_coercion="Join the array into a comma-separated string"),
]

# The live demo agent's four tools map to the three endpoint actions:
#   delivery-tracking-api  -> coerce  (Act 1: the visible before/after)
#   geolocation-service    -> warn    (second action type)
#   customer-profile-api   -> allow   (proves the model discriminates)
#   courier-eta-api        -> Act 2   (the silent bug no error log can find)

# COMMAND ----------

# DBTITLE 1,Agent -> tool affinity (generator call distribution)
# Each agent only touches the tools its job needs. This is what makes "which app is
# bleeding?" a diagnostic question rather than a restatement of traffic volume.
AGENT_TOOL_AFFINITY = [
    # order-support-agent
    ("order-support-agent","order-history-api","list_orders",0.30),
    ("order-support-agent","order-history-api","get_order",0.20),
    ("order-support-agent","payment-gateway","create_refund",0.25),
    ("order-support-agent","customer-profile-api","get_customer",0.25),
    # merchant-onboarding-agent
    ("merchant-onboarding-agent","merchant-catalog-api","update_merchant",0.30),
    ("merchant-onboarding-agent","menu-service","upsert_menu_item",0.35),
    ("merchant-onboarding-agent","compliance-api","check_merchant",0.35),
    # delivery-routing-agent  (LIVE)
    ("delivery-routing-agent","courier-eta-api","get_eta",0.35),
    ("delivery-routing-agent","delivery-tracking-api","list_deliveries",0.30),
    ("delivery-routing-agent","geolocation-service","distance_matrix",0.20),
    ("delivery-routing-agent","customer-profile-api","get_addresses",0.15),
    # fraud-detection-agent
    ("fraud-detection-agent","analytics-aggregator","batch_query",0.40),
    ("fraud-detection-agent","payment-gateway","get_payment",0.35),
    ("fraud-detection-agent","customer-profile-api","get_customer",0.25),
    # customer-retention-agent
    ("customer-retention-agent","promo-engine","create_promo",0.35),
    ("customer-retention-agent","analytics-aggregator","query_metrics",0.35),
    ("customer-retention-agent","customer-profile-api","get_preferences",0.30),
    # menu-recommendation-agent
    ("menu-recommendation-agent","merchant-catalog-api","get_merchant",0.40),
    ("menu-recommendation-agent","menu-service","search_menu",0.35),
    ("menu-recommendation-agent","geolocation-service","geocode",0.25),
]

# COMMAND ----------

# DBTITLE 1,Derived lookups (never hand-maintained)
# Which agents are exposed to each bug -- derived from the affinity matrix, so it cannot
# drift away from the call distribution the generator actually uses.
_AFFINITY_BY_TOOL = {}
for _a, _s, _t, _w in AGENT_TOOL_AFFINITY:
    _AFFINITY_BY_TOOL.setdefault((_s, _t), []).append(_a)

for _b in BUG_FAMILIES:
    _b["agent_ids"] = sorted(_AFFINITY_BY_TOOL.get((_b["server"], _b["tool"]), []))

SERVER_BY_NAME = {s["server"]: s for s in TOOL_SERVERS}
TOOL_BY_KEY    = {(s, t): dict(server=s, tool=t, description=d, expected_args=a, sla_p99_ms=l)
                  for s, t, d, a, l in TOOLS}
BUG_BY_TOOL    = {(b["server"], b["tool"]): b for b in BUG_FAMILIES}
AGENT_BY_ID    = {a["agent_id"]: a for a in AGENTS}

# COMMAND ----------

# DBTITLE 1,Model pricing (Databricks-served)
MODEL_PRICING = [
    dict(model_id="claude-sonnet-5",  endpoint="system.ai.claude-sonnet-5", model="claude-sonnet-5",
         provider="anthropic", in_per_1k=0.003,   out_per_1k=0.015,  context=200000),
    dict(model_id="gemini-3-5-flash", endpoint="system.ai.gemini-3-5-flash", model="gemini-3-5-flash",
         provider="google",    in_per_1k=0.00015, out_per_1k=0.0006, context=1000000),
    dict(model_id="gpt-5-2",          endpoint="system.ai.gpt-5-2", model="gpt-5-2",
         provider="openai",    in_per_1k=0.00125, out_per_1k=0.01,   context=400000),
    dict(model_id="claude-haiku-5-5", endpoint="system.ai.claude-haiku-5-5",
         provider="anthropic", in_per_1k=0.0008,  out_per_1k=0.004,  context=200000),
    dict(model_id="claude-opus-5-5",  endpoint="system.ai.claude-opus-5-5",
         provider="anthropic", in_per_1k=0.015,   out_per_1k=0.075,  context=200000),
    dict(model_id="gpt-5-nano",       endpoint="system.ai.gpt-5-nano",
         provider="openai",    in_per_1k=0.00005, out_per_1k=0.0004, context=400000),
]

PRICING_EFFECTIVE = date(2026, 7, 1)
ENG_HOURLY_USD    = 95.0   # documented constant for "engineering hours redeployed"

# COMMAND ----------

# DBTITLE 1,Behaviour model — why recovery loops are expensive
# Recovery turns are a LEARNABLE FUNCTION of features, not noise. That is precisely what
# lets the model price a bug family it has never seen, from its feature neighbours.
# Anchored on the published 4.6-12.1 turn range by error-message quality.
MSG_QUALITY_BASE = {"good": 2.1, "fair": 4.6, "poor": 8.3, "none": 6.0}

MISMATCH_PENALTY = {
    "enum_case":      0.2,
    "type_coercion":  0.5,
    "format":         1.0,
    "rate_limit":     1.5,
    "shape_mismatch": 2.0,
    "timeout_split":  2.5,
    "array_vs_scalar":2.0,
    "unit_mismatch":  3.0,
}

# A tool-calling agent carries a system prompt + tool definitions + the accumulated
# transcript, so every extra turn costs MORE than the last. Context accumulation is
# the reason an 8-turn recovery loop is expensive rather than merely slow.
TOKENS_FIRST_TURN   = (12_000, 40_000)
TOKEN_GROWTH_PER_TURN = 0.35
OUTPUT_TOKENS       = (180, 1_400)

# A server that already coerces input neutralises most mismatches.
COERCION_RECOVERY_MULTIPLIER = 0.25

# COMMAND ----------

# DBTITLE 1,Self-check
_srv = {s["server"] for s in TOOL_SERVERS}
_tool_keys = {(s, t) for s, t, _d, _a, _l in TOOLS}

assert len(AGENTS) == 6, "expected 6 agents"
assert len(TOOL_SERVERS) == 11, "expected 11 tool servers"
assert len(BUG_FAMILIES) == 10, "expected 10 bug families"
assert LIVE_AGENT in {a["agent_id"] for a in AGENTS}
for b in BUG_FAMILIES:
    assert b["server"] in _srv, f"bug {b['bug_id']} -> unknown server {b['server']}"
    assert (b["server"], b["tool"]) in _tool_keys, f"bug {b['bug_id']} -> unknown tool {b['tool']}"
for a_id, srv, tl, _w in AGENT_TOOL_AFFINITY:
    assert srv in _srv, f"affinity -> unknown server {srv}"
    assert (srv, tl) in _tool_keys, f"affinity -> unknown tool {srv}.{tl}"
_endpoints = {m["endpoint"] for m in MODEL_PRICING}
for a in AGENTS:
    assert a["endpoint"] in _endpoints, f"agent {a['agent_id']} endpoint missing from pricing"

assert all(b["agent_ids"] for b in BUG_FAMILIES), "every bug must reach >=1 agent"
assert all("model" in a for a in AGENTS), "agents need a short model id"

print(f"config OK  |  {len(AGENTS)} agents  {len(TOOL_SERVERS)} servers  "
      f"{len(TOOLS)} tools  {len(BUG_FAMILIES)} bugs  {len(AGENT_TOOL_AFFINITY)} affinity rows")
print(f"live agent: {LIVE_AGENT}")
print(f"silent bugs: {[b['bug_id'] for b in BUG_FAMILIES if b['is_silent']]}")
