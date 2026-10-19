# Evidence 6: live calls against the served model

_Real responses from the Model Serving endpoint `tiffin-tool-call-risk`
(model `main.tiffin_agent_waste.tool_call_risk` v1) on e2-dogfood, 2026-10-19 21:11 UTC._

**On the `warn` rows below.** The served model emits three bands. The wrapper collapses the
middle one: `score_call` maps `warn` to `allow` and sets `borderline`, because an ambiguous fix is
one that should not be applied silently, and "flag it but proceed" is behaviourally identical to
allowing it. So the raw endpoint responses here show `warn`, and the agent acts on **two** verdicts,
`coerce` or `allow`. Both are shown so the mapping is auditable rather than hidden.

The model sees **seven fields and no natural language**: tool server, tool name, the argument
JSON, agent id, turn index, tokens spent so far, and how many times this tool has already been
called this session. It never sees the user's question. That is deliberate: the signal is in the
shape of the call, not in the prose around it.


### The live demo case: v2 pagination token the server will reject

**Request**
```json
{
  "tool_server": "delivery-tracking-api",
  "tool_name": "list_deliveries",
  "arguments": "{\"courier_id\": \"CR-4172\", \"page_token\": \"v2:eyJvZmZzZXQiOjI1fQ==\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 1,
  "tokens_before_turn": 1200,
  "call_seq_this_tool": 2
}
```
**Response** _(round trip 337 ms)_
```json
{
  "failure_probability": 0.8919,
  "action": "coerce",
  "mismatch_class": "token_version",
  "mismatched_param": "page_token",
  "declared_type": "v1_token_string",
  "suggested_coercion": "page_token: rewrite the pagination token into v1 format",
  "confidence": "high"
}
```

### First page, nothing wrong with it

**Request**
```json
{
  "tool_server": "delivery-tracking-api",
  "tool_name": "list_deliveries",
  "arguments": "{\"courier_id\": \"CR-4172\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 0,
  "tokens_before_turn": 600,
  "call_seq_this_tool": 1
}
```
**Response** _(round trip 385 ms)_
```json
{
  "failure_probability": 0.0002,
  "action": "allow",
  "mismatch_class": "missing_required",
  "mismatched_param": "page_token",
  "declared_type": "v1_token_string",
  "suggested_coercion": "",
  "confidence": "high"
}
```

### Enum in the wrong case: server declares ACTIVE/PAUSED/SUSPENDED

**Request**
```json
{
  "tool_server": "merchant-catalog-api",
  "tool_name": "update_merchant",
  "arguments": "{\"merchant_id\": \"M-553014\", \"status\": \"paused\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 2,
  "tokens_before_turn": 3100,
  "call_seq_this_tool": 1
}
```
**Response** _(round trip 306 ms)_
```json
{
  "failure_probability": 0.3117,
  "action": "warn",
  "mismatch_class": "enum_case",
  "mismatched_param": "status",
  "declared_type": "enum[ACTIVE,PAUSED,SUSPENDED]",
  "suggested_coercion": "",
  "confidence": "medium"
}
```

### Batch above the server's documented limit

**Request**
```json
{
  "tool_server": "geolocation-service",
  "tool_name": "distance_matrix",
  "arguments": "{\"origins\": [\"a\",\"b\",\"c\",\"d\",\"e\"], \"destination\": \"1042 Harrison St\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 2,
  "tokens_before_turn": 2800,
  "call_seq_this_tool": 1
}
```
**Response** _(round trip 322 ms)_
```json
{
  "failure_probability": 0.0066,
  "action": "allow",
  "mismatch_class": "missing_required",
  "mismatched_param": "destinations",
  "declared_type": "array<string>",
  "suggested_coercion": "",
  "confidence": "high"
}
```

### A clean lookup

**Request**
```json
{
  "tool_server": "customer-profile-api",
  "tool_name": "get_addresses",
  "arguments": "{\"customer_id\": \"C-9931112\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 0,
  "tokens_before_turn": 500,
  "call_seq_this_tool": 1
}
```
**Response** _(round trip 327 ms)_
```json
{
  "failure_probability": 0.0,
  "action": "allow",
  "mismatch_class": "none",
  "mismatched_param": "",
  "declared_type": "",
  "suggested_coercion": "",
  "confidence": "high"
}
```

### Same malformed call, 4th retry: the model sees the retry context

**Request**
```json
{
  "tool_server": "delivery-tracking-api",
  "tool_name": "list_deliveries",
  "arguments": "{\"courier_id\": \"CR-4172\", \"page_token\": \"v2:eyJvZmZzZXQiOjI1fQ==\"}",
  "agent_id": "delivery-routing-agent",
  "turn_index": 4,
  "tokens_before_turn": 7400,
  "call_seq_this_tool": 4
}
```
**Response** _(round trip 318 ms)_
```json
{
  "failure_probability": 0.6299,
  "action": "coerce",
  "mismatch_class": "token_version",
  "mismatched_param": "page_token",
  "declared_type": "v1_token_string",
  "suggested_coercion": "page_token: rewrite the pagination token into v1 format",
  "confidence": "medium"
}
```

## Summary

| tool | action | failure_probability | mismatch_class | param | latency |
|---|---|---|---|---|---|
| `list_deliveries` | **coerce** | 0.8919 | token_version | page_token | 337 ms |
| `list_deliveries` | **allow** | 0.0002 | missing_required | page_token | 385 ms |
| `update_merchant` | **warn** | 0.3117 | enum_case | status | 306 ms |
| `distance_matrix` | **allow** | 0.0066 | missing_required | destinations | 322 ms |
| `get_addresses` | **allow** | 0.0 | none |  | 327 ms |
| `list_deliveries` | **coerce** | 0.6299 | token_version | page_token | 318 ms |

Latency across 6 calls: min 306 ms, max 385 ms, mean 332 ms. The guard sits in the agent's critical path, so this has to stay well inside a single turn's budget.

**Fail-open contract.** If this endpoint is unreachable or slow, `agent.py` catches the exception, emits a `guard_error` event and lets the tool call proceed. A guard that takes the agent down is worse than the waste it prevents.
