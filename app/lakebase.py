"""
Lakebase (Postgres) access for the Tiffin app.

Two paths, both operational rather than analytical:
  read  -- tiffin.fix_queue, the money-ranked "what should my team fix first" queue
  write -- tiffin.guard_decisions, an append-only audit trail of every verdict

The gold Delta tables stay the analytical source of truth; Genie reads those. This module
exists because an app page that someone opens all day wants an indexed point lookup, not a
warehouse scan.

Credentials are short-lived OAuth tokens minted from the app's own identity and refreshed
before expiry -- there is no stored password anywhere.
"""
import json
import os
import time
from typing import Optional

import psycopg2
import psycopg2.extras
import requests
from databricks.sdk.core import Config

PROJECT  = os.getenv("LAKEBASE_PROJECT", "tiffin-preflight")
BRANCH   = os.getenv("LAKEBASE_BRANCH", "production")
ENDPOINT = f"projects/{PROJECT}/branches/{BRANCH}/endpoints/primary"
PGHOST   = os.getenv("PGHOST", "ep-delicate-grass-y1r5bmqd.database.us-west-2.staging.cloud.databricks.com")
PGDB     = os.getenv("PGDATABASE", "databricks_postgres")
SCHEMA   = os.getenv("LAKEBASE_SCHEMA", "tiffin")

_cfg = Config()
_tok: Optional[str] = None
_tok_exp: float = 0.0
_conn = None


def _token(attempts: int = 4) -> str:
    """Mint/refresh the Lakebase credential. Tokens last ~1h; refresh at 50 min.

    The credentials endpoint returns a transient TEMPORARILY_UNAVAILABLE often enough
    to matter, so retry with backoff rather than losing the panel over one bad call.
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
                _tok_exp = time.time() + 3000
                return _tok
            last = f"{r.status_code} {r.text[:120]}"
            if r.status_code < 500 and r.status_code != 429:
                break           # a real client error will not fix itself
        except requests.RequestException as e:
            last = str(e)
        time.sleep(0.6 * (2 ** i))
    raise RuntimeError(f"could not mint Lakebase credential: {last}")


def _pguser() -> str:
    """The app authenticates to Postgres as its own service principal."""
    return os.getenv("PGUSER") or os.getenv("DATABRICKS_CLIENT_ID") or _cfg.client_id or ""


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


def fix_queue(owner_team: Optional[str] = None, limit: int = 20):
    """The ranked fix queue. Filtered by owner_team, this is a single indexed lookup."""
    sql = (f"SELECT fix_rank, owner_team, tool_server, tool_name, waste_usd_per_year, "
           f"failed_calls, failure_rate_pct, waste_usd_per_failed_call "
           f"FROM {SCHEMA}.fix_queue ")
    args: tuple = ()
    if owner_team and owner_team != "all teams":
        sql += "WHERE owner_team = %s "
        args = (owner_team,)
    sql += "ORDER BY fix_rank LIMIT %s"
    args = args + (limit,)
    with connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, args)
        return [dict(r) for r in c.fetchall()]


def owner_teams():
    with connect().cursor() as c:
        c.execute(f"SELECT DISTINCT owner_team FROM {SCHEMA}.fix_queue ORDER BY 1")
        return [r[0] for r in c.fetchall()]


def log_decision(agent_id, tool_server, tool_name, turn_index, pred,
                 args_before, args_after):
    """Append one guard verdict. Never raises -- a failed audit write must not fail the call."""
    try:
        with connect().cursor() as c:
            c.execute(
                f"""INSERT INTO {SCHEMA}.guard_decisions
                    (agent_id, tool_server, tool_name, turn_index, action, failure_prob,
                     mismatch_class, mismatched_param, args_before, args_after)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (agent_id, tool_server, tool_name, turn_index,
                 pred.get("action"), pred.get("failure_probability"),
                 pred.get("mismatch_class"), pred.get("mismatched_param"),
                 json.dumps(args_before), json.dumps(args_after)))
        return True
    except Exception:
        return False


def recent_decisions(limit: int = 15):
    with connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"""SELECT decided_at, tool_name, action, failure_prob, mismatch_class
                      FROM {SCHEMA}.guard_decisions
                      ORDER BY decided_at DESC LIMIT %s""", (limit,))
        return [dict(r) for r in c.fetchall()]
