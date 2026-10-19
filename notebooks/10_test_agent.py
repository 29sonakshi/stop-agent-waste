# Databricks notebook source
# Tests the SHIPPING agent module end to end against the live endpoints.
import sys, json
sys.path.insert(0, "/Workspace/Users/sonakshi.pandey@databricks.com/fe-bar-sonakshi/demo_app")
import agent

results = {}
for guard in (False, True):
    events = []
    try:
        summary = agent.run_agent(agent.DEFAULT_REQUEST, guard_on=guard, emit=events.append)
    except Exception as e:
        results[f"guard_{guard}"] = {"ERROR": f"{type(e).__name__}: {e}"}
        continue
    # compact trace: what the agent proposed, what the guard said, what happened
    trace = []
    for ev in events:
        t = ev["type"]
        if t == "turn":      trace.append(f"--- turn {ev['turn']}")
        elif t == "proposed":trace.append(f"  propose {ev['tool']} {json.dumps(ev['args'])[:110]}")
        elif t == "guard":
            p = ev["pred"]
            trace.append(f"  GUARD {p.get('action')} p={p.get('failure_probability')} cls={p.get('mismatch_class')}")
        elif t == "coerced": trace.append(f"  COERCED {ev['note']}")
        elif t == "guard_error": trace.append(f"  GUARD-ERROR {ev['error'][:80]}")
        elif t == "result":  trace.append(f"  {'ok' if ev['ok'] else 'FAIL'} {json.dumps(ev['result'])[:110]}")
        elif t == "final":   trace.append(f"  FINAL: {ev['text'][:300]}")
    results[f"guard_{guard}"] = {"summary": summary, "trace": trace}

print(json.dumps(results, indent=2, default=str))
dbutils.notebook.exit(json.dumps(results, default=str)[:14000])
