"""
Lakebase (Postgres) verdict log for the Tiffin guard.

Every decision the guard makes is appended here, in two phases: the verdict is known
before the tool runs, the outcome only after it returns. So the caller inserts the
decision, keeps the row id, and updates that row once the tool answers.

What this records that a Model Serving inference log cannot:
  * what the wrapper actually DID -- `coerce` is a recommendation, and whether the
    argument was really rewritten (and to what) is known only here
  * whether the tool call then succeeded, which is the label that separates a good
    coercion from a bad one
  * fail-open events, where the guard was unreachable so no request was ever logged

Credentials are short-lived OAuth tokens minted from the app's own identity. Nothing
is stored.
"""
import json
import os
import time
import uuid
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from databricks.sdk.core import Config

PROJECT  = os.getenv("LAKEBASE_PROJECT", "tiffin-preflight")
BRANCH   = os.getenv("LAKEBASE_BRANCH", "production")
ENDPOINT = f"projects/{PROJECT}/branches/{BRANCH}/endpoints/primary"
PGHOST   = os.getenv("PGHOST", "ep-delicate-grass-y1r5bmqd.database.us-west-2."
                               "staging.cloud.databricks.com")
PGDB     = os.getenv("PGDATABASE", "databricks_postgres")
SCHEMA   = os.getenv("LAKEBASE_SCHEMA", "tiffin")

_cfg = Config()
_tok: Optional[str] = None
_tok_exp: float = 0.0
_conn = None


def new_run_id() -> str:
    """Correlates every decision in one agent run."""
    return uuid.uuid4().hex[:12]


def _token(attempts: int = 4) -> str:
    """Mint/refresh the Lakebase credential, with backoff.

    The credentials endpoint returns a transient TEMPORARILY_UNAVAILABLE often enough
    to matter, so retry rather than lose the write over one bad call.
    """
    global _tok, _tok_exp
    if _tok and time.time() < _tok_exp:
        return _tok
    last = None
    for i in range(attempts):
        try:
            r = requests.post(f"{_cfg.host.rstrip('/')}/api/2.0/postgres/credentials",
                              headers=_cfg.authenticate(),
                              json={"endpoint": ENDPOINT}, timeout=30)
            if r.status_code == 200:
                _tok = r.json()["token"]
                _tok_exp = time.time() + 3000      # tokens last ~1h
                return _tok
            last = f"{r.status_code} {r.text[:120]}"
            if r.status_code < 500 and r.status_code != 429:
                break                              # a real client error will not fix itself
        except requests.RequestException as e:
            last = str(e)
        time.sleep(0.6 * (2 ** i))
    raise RuntimeError(f"could not mint Lakebase credential: {last}")


def _pguser() -> str:
    """The app authenticates to Postgres as its own service principal."""
    return (os.getenv("PGUSER") or os.getenv("DATABRICKS_CLIENT_ID")
            or _cfg.client_id or "")


def connect():
    """One lazily-created connection, reopened if the server dropped it."""
    global _conn
    if _conn is not None and _conn.closed == 0:
        try:
            with _conn.cursor() as c:
                c.execute("SELECT 1")
            return _conn
        except Exception:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
    _conn = psycopg2.connect(host=PGHOST, dbname=PGDB, user=_pguser(),
                             password=_token(), sslmode="require", connect_timeout=10)
    _conn.autocommit = True
    return _conn


# ---------------------------------------------------------------- the two-phase write
# Both calls swallow their own errors on purpose. An audit trail that can fail the
# call it is describing is worse than no audit trail.

def log_decision(run_id, agent_id, tool_server, tool_name, turn_index, pred,
                 args_before, args_after, coercion_applied, guard_latency_ms,
                 guard_error=None):
    """Phase 1: record the verdict, before the tool runs. Returns the row id."""
    try:
        with connect().cursor() as c:
            c.execute(
                f"""INSERT INTO {SCHEMA}.guard_decisions
                    (run_id, agent_id, tool_server, tool_name, turn_index, action,
                     failure_prob, mismatch_class, mismatched_param, guard_latency_ms,
                     coercion_applied, args_before, args_after, guard_error)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id""",
                (run_id, agent_id, tool_server, tool_name, turn_index,
                 pred.get("action") or ("guard_error" if guard_error else "allow"),
                 pred.get("failure_probability"), pred.get("mismatch_class"),
                 pred.get("mismatched_param"), guard_latency_ms, coercion_applied,
                 json.dumps(args_before), json.dumps(args_after), guard_error))
            return c.fetchone()[0]
    except Exception:
        return None


def log_outcome(decision_id, tool_ok):
    """Phase 2: record whether the tool call the verdict described actually worked."""
    if decision_id is None:
        return False
    try:
        with connect().cursor() as c:
            c.execute(f"UPDATE {SCHEMA}.guard_decisions SET tool_ok = %s WHERE id = %s",
                      (tool_ok, decision_id))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- reads
def recent_decisions(limit: int = 20):
    with connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"""SELECT decided_at, tool_name, action, failure_prob,
                             mismatch_class, coercion_applied, tool_ok, guard_latency_ms
                      FROM {SCHEMA}.guard_decisions
                      ORDER BY decided_at DESC LIMIT %s""", (limit,))
        return [dict(r) for r in c.fetchall()]


def run_summary(run_id: str):
    """Everything the guard did in one agent run. Point lookup on an indexed column."""
    with connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"""SELECT turn_index, tool_name, action, failure_prob,
                             coercion_applied, tool_ok,
                             args_before->>mismatched_param AS before_value,
                             args_after->>mismatched_param  AS after_value
                      FROM {SCHEMA}.guard_decisions
                      WHERE run_id = %s ORDER BY id""", (run_id,))
        return [dict(r) for r in c.fetchall()]
