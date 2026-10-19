"""
Tiffin delivery-routing agent + pre-flight guard. Pure logic, no UI.

The app imports this and so does the test, so what gets tested is what ships.

THE STORY
    A dispatcher asks a normal operational question: which of this courier's deliveries
    are running late? Answering it needs page 2 of the delivery list.

    `delivery-tracking-api` was upgraded to EMIT v2 pagination tokens, but its validator
    still only ACCEPTS v1 -- a post-deploy contract mismatch (the kind `deploy_events`
    records). So the agent gets a v2 token from page 1, hands it straight back, and is
    rejected with a useless error message.

    Without the guard the agent never reaches page 2, so it cannot answer the question
    at all. With the guard the mismatch is caught before the call runs, rewritten from
    the server's own declared schema, and the agent answers on turn 3.

    The user never types a token. The bug lives in the tool contract, where it belongs.
"""
import json
import os
from typing import Callable

import requests
from databricks.sdk.core import Config

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "databricks-claude-sonnet-5")
RISK_ENDPOINT = os.getenv("RISK_ENDPOINT", "tiffin-tool-call-risk")
AGENT_ID = "delivery-routing-agent"
MAX_TURNS = 6

DEFAULT_REQUEST = "Which of courier CR-4172's deliveries are running late?"

WARN_REQUEST = ("Courier CR-4172 says the pickup location for their first delivery has gone "
                "dark. Pull up CR-4172's deliveries, then pause that merchant so we stop "
                "routing new orders there.")

ALLOW_REQUEST = "What addresses do we have on file for customer C-9931112?"

# The three demo scenarios, each verified end-to-end against the live endpoint.
SCENARIOS = {
    "Late deliveries — expect COERCE": DEFAULT_REQUEST,
    "Merchant gone dark — expect WARN": WARN_REQUEST,
    "Customer addresses — expect ALLOW": ALLOW_REQUEST,
}

_cfg = Config()


def _post(path: str, payload: dict, timeout: int = 300) -> dict:
    r = requests.post(f"{_cfg.host}{path}", headers=_cfg.authenticate(),
                      json=payload, timeout=timeout)
    if r.status_code >= 400:
        # Surface the server's message. raise_for_status() hides the body, which turns a
        # precise "bad field X" into an unactionable "400 Bad Request".
        raise RuntimeError(f"{r.status_code} from {path}: {r.text[:500]}")
    return r.json()


# ---------------------------------------------------------------- tool definitions
TOOLS = [
    {"type": "function", "function": {
        "name": "list_deliveries",
        "description": ("List a courier's deliveries, 25 per page. Omit page_token for the "
                        "first page; pass the next_page_token from the previous response to "
                        "get the following page."),
        "parameters": {"type": "object", "properties": {
            "courier_id": {"type": "string"},
            "page_token": {"type": "string", "description": "Pagination token from a previous response"},
            "limit": {"type": "integer"}},
            "required": ["courier_id"]}}},
    {"type": "function", "function": {
        "name": "get_eta",
        "description": "Current estimated time of arrival for a courier.",
        "parameters": {"type": "object", "properties": {
            "courier_id": {"type": "string"},
            "destination": {"type": "string"},
            "unit": {"type": "string", "enum": ["seconds", "minutes"]}},
            "required": ["courier_id", "destination", "unit"]}}},
    {"type": "function", "function": {
        "name": "distance_matrix",
        "description": "Pairwise distances between origins and destinations.",
        "parameters": {"type": "object", "properties": {
            "origins": {"type": "array", "items": {"type": "string"}},
            "destinations": {"type": "array", "items": {"type": "string"}}},
            "required": ["origins", "destinations"]}}},
    {"type": "function", "function": {
        "name": "update_merchant",
        "description": ("Update a merchant's order-acceptance status, e.g. when a store has "
                        "closed or stopped accepting orders."),
        "parameters": {"type": "object", "properties": {
            "merchant_id": {"type": "string"},
            "status": {"type": "string", "description": "The merchant's new status."}},
            "required": ["merchant_id", "status"]}}},
    {"type": "function", "function": {
        "name": "get_addresses",
        "description": "Saved addresses for a customer.",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"}}, "required": ["customer_id"]}}},
]

TOOL_SERVER = {
    "list_deliveries": "delivery-tracking-api",
    "get_eta": "courier-eta-api",
    "distance_matrix": "geolocation-service",
    "get_addresses": "customer-profile-api",
    "update_merchant": "merchant-catalog-api",
}

# page 1 is all on time; the LATE delivery is only on page 2. That is what makes the
# guard load-bearing rather than cosmetic -- without it the question is unanswerable.
_PAGE1 = [{"delivery_id": "D-4410221", "status": "en_route", "minutes_late": 0,
           "pickup_merchant_id": "M-100195"},
          {"delivery_id": "D-4410288", "status": "picked_up", "minutes_late": 0,
           "pickup_merchant_id": "M-204881"}]
_PAGE2 = [{"delivery_id": "D-4410315", "status": "en_route", "minutes_late": 18,
           "destination": "1042 Harrison St", "pickup_merchant_id": "M-553014"},
          {"delivery_id": "D-4410402", "status": "delivered", "minutes_late": 0,
           "pickup_merchant_id": "M-204881"}]

# The server emits v2 but validates v1 -- the whole bug, in two lines.
_EMITTED_TOKEN = "v2:eyJvZmZzZXQiOjI1fQ=="
_ACCEPTED_PREFIX = "v1-"


def _stable_km(origin: str) -> float:
    """Deterministic pseudo-distance per origin id, so results are rankable."""
    return round(1.2 + (sum(ord(c) for c in origin) * 7919 % 173) / 10.0, 1)


def run_tool(name: str, args: dict):
    """Mock MCP servers. Note failures return successfully at the transport layer --
    there is no HTTP error to grep for, which is the point."""
    if name == "list_deliveries":
        tok = str(args.get("page_token") or "")
        if not tok:
            return True, {"page": 1, "deliveries": _PAGE1, "next_page_token": _EMITTED_TOKEN}
        if not tok.startswith(_ACCEPTED_PREFIX):
            return False, "Your input couldn't be processed: TypeError in handler"
        return True, {"page": 2, "deliveries": _PAGE2, "next_page_token": None}

    if name == "get_eta":
        return True, {"eta": 540, "unit": "seconds"}      # SILENT bug: always seconds

    if name == "distance_matrix":
        origins = args.get("origins") or []
        if len(origins) > 3:
            return False, "429 rate limit exceeded: reduce batch size and retry"
        # Stable per-origin value so batched and repeated calls agree with each other.
        return True, {"distances_km": [_stable_km(o) for o in origins]}

    if name == "update_merchant":
        status = str(args.get("status") or "")
        if status not in ("ACTIVE", "PAUSED", "SUSPENDED"):
            return False, (f"Invalid status '{status}'. "
                           "Allowed values: ACTIVE, PAUSED, SUSPENDED")
        return True, {"merchant_id": args.get("merchant_id"), "status": status,
                      "updated": True}

    if name == "get_addresses":
        return True, {"addresses": [{"label": "home", "city": "Oakland"}]}

    return False, f"unknown tool {name}"


# ---------------------------------------------------------------- the guard
def score_call(tool_name: str, args: dict, turn: int, tokens_so_far: int, seq: int) -> dict:
    out = _post(f"/serving-endpoints/{RISK_ENDPOINT}/invocations", {"dataframe_records": [{
        "tool_server": TOOL_SERVER[tool_name], "tool_name": tool_name,
        "arguments": json.dumps(args), "agent_id": AGENT_ID,
        "turn_index": turn, "tokens_before_turn": tokens_so_far,
        "call_seq_this_tool": seq}]})
    pred = (out.get("predictions") or [{}])[0]
    # The model emits a borderline band it calls "warn". The wrapper has exactly two
    # behaviours -- rewrite the argument, or let the call through untouched -- so a
    # borderline score is an allow. Collapsing it here keeps the contract the app and
    # the demo narrative describe honest: coerce or allow, nothing else.
    if pred.get("action") == "warn":
        pred["action"] = "allow"
        pred["borderline"] = True
    return pred


def apply_coercion(args: dict, pred: dict):
    """Rewrite the flagged argument using the server's declared contract."""
    cls, param = pred.get("mismatch_class"), pred.get("mismatched_param")
    fixed = dict(args)
    if not param or param not in fixed:
        return fixed, None
    if cls == "token_version":
        tok = str(fixed[param])
        fixed[param] = _ACCEPTED_PREFIX + tok.split(":", 1)[-1].rstrip("=")[:16]
        return fixed, f"rewrote {param} from v2 to v1 format"
    if cls == "array_vs_scalar" and isinstance(fixed[param], list):
        fixed[param] = ",".join(str(x) for x in fixed[param])
        return fixed, f"joined {param} into a comma-separated string"
    if cls == "enum_case":
        fixed[param] = str(fixed[param]).upper()
        return fixed, f"upper-cased {param}"
    return fixed, None


def extract_text(content) -> str:
    """Pull the answer out of Claude's block format.

    The final answer arrives as reasoning/text blocks -- sometimes already decoded into a
    LIST, sometimes as a JSON STRING. Handle both, or the UI shows base64 thinking
    signatures instead of an answer.
    """
    blocks = content
    if isinstance(content, str):
        try:
            blocks = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return content
    if not isinstance(blocks, list):
        return str(content or "")
    text = "\n".join(b.get("text", "") for b in blocks
                      if isinstance(b, dict) and b.get("type") == "text").strip()
    if text:
        return text
    # Reasoning blocks but no text block = the model exhausted its output budget while
    # still thinking. Say so plainly; dumping the blocks shows a wall of base64 signatures.
    return "_(the model ran out of output tokens before writing an answer)_"


def llm_turn(messages):
    out = _post(f"/serving-endpoints/{LLM_ENDPOINT}/invocations",
                {"messages": messages, "tools": TOOLS, "max_tokens": 4000})
    usage = out.get("usage") or {}
    return out["choices"][0]["message"], usage


# ---------------------------------------------------------------- the loop
def run_agent(request: str, guard_on: bool, emit: Callable[[dict], None]) -> dict:
    """Drive the agent. `emit` receives one event dict per step so any UI can render it."""
    messages = [
        {"role": "system", "content":
            "You are Tiffin's delivery routing agent. Use the tools to answer the "
            "dispatcher's question. Page through results when a next_page_token is "
            "returned. If a tool errors, adjust the arguments and try again."},
        {"role": "user", "content": request},
    ]
    tokens = 0
    seq: dict = {}
    answered = False
    turns_used = 0
    # `answered` only means the agent emitted a final message. It says nothing about whether
    # the answer was right. The late delivery lives on page 2, so reaching page 2 is the
    # signal that actually separates a correct run from a confident wrong one.
    found_late = False

    for turn in range(MAX_TURNS):
        turns_used = turn + 1
        emit({"type": "turn", "turn": turns_used})
        msg, usage = llm_turn(messages)
        tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

        calls = msg.get("tool_calls") or []
        if not calls:
            emit({"type": "final", "text": extract_text(msg.get("content"))})
            answered = True
            break

        # Send back ONLY role + tool_calls. Claude's extended-thinking blocks arrive inside
        # `content`, and replaying them fails with
        #   "The final block in an assistant message cannot be `thinking`".
        # The tool result carries the state forward, so the reasoning blocks are not needed.
        messages.append({"role": "assistant", "tool_calls": calls})

        for call in calls:
            fn = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            seq[fn] = seq.get(fn, 0) + 1
            emit({"type": "proposed", "tool": fn, "args": args})

            if guard_on:
                try:
                    pred = score_call(fn, args, turn, tokens, seq[fn])
                    emit({"type": "guard", "pred": pred})
                    if pred.get("action") == "coerce":
                        args, note = apply_coercion(args, pred)
                        if note:
                            emit({"type": "coerced", "note": note, "args": args})
                except Exception as e:  # noqa: BLE001
                    # FAIL OPEN: a guard that takes the agent offline is worse than the waste.
                    emit({"type": "guard_error", "error": str(e)})

            ok, result = run_tool(fn, args)
            if ok and isinstance(result, dict):
                for d in (result.get("deliveries") or []):
                    if (d.get("minutes_late") or 0) > 0:
                        found_late = True
            emit({"type": "result", "ok": ok, "result": result})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result) if isinstance(result, dict) else str(result)})

    return {"turns": turns_used, "tokens": tokens, "answered": answered,
            "found_late": found_late,
            "guard_on": guard_on}
