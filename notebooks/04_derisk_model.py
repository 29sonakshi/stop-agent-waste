# Databricks notebook source
# DBTITLE 1,04_derisk_model — STOP-OR-GO gate
# 04_derisk_model.py
#
# THE QUESTION THIS NOTEBOOK ANSWERS
#   Can purely BEHAVIOURAL session features detect token waste that leaves NO error trace?
#
#   If yes, the premise holds: "your error log found the failures that announced themselves;
#   the model finds the ones that didn't." If no, there is no ML story -- only a dashboard --
#   and we re-pick the model before investing in the generator, pipeline, or app.
#
# EXPERIMENT DESIGN
#   TRAIN  on clean sessions + sessions hit by the 9 LOUD bug families.
#          Label = did this session hit a loud bug.
#   TEST   on clean sessions + sessions hit by the SILENT family, which is held out
#          ENTIRELY (never seen in training, in any form).
#          Label = did this session hit the silent bug.
#   BASELINE "grep the error log": predict waste iff the session emitted any error.
#          On the silent test set its recall is ZERO BY CONSTRUCTION -- that is the point.
#
# ANTI-LEAKAGE RULES (enforced by assertion below, not by good intentions)
#   - no error.type / http status / error counts in features
#   - no bug_id or bug family identity in features
#   - no per-bug message_quality (only ever "none" on the silent family -> would be the answer)
#   - no session outcome / CSAT (known only after the fact)
#   Features are limited to what a platform team can compute from the span tree alone.
#
# Writes NOTHING. Pure evaluation.

# COMMAND ----------

# MAGIC %run ./00_config

# COMMAND ----------

# DBTITLE 1,Session simulator (in-memory, no writes)
import json, math, random
import numpy as np
import pandas as pd
from collections import Counter, defaultdict

random.seed(DERISK_SEED)
np.random.seed(DERISK_SEED)

N_CLEAN        = 4000   # clean sessions, shared by train and test as the negative class
N_PER_LOUD_BUG = 420    # positives per loud family  (9 families)
N_SILENT       = 500    # positives for the held-out silent family

LOUD_BUGS   = [b for b in BUG_FAMILIES if not b["is_silent"]]
SILENT_BUGS = [b for b in BUG_FAMILIES if b["is_silent"]]
assert len(SILENT_BUGS) == 1, "de-risk assumes exactly one silent family"
SILENT = SILENT_BUGS[0]

AFFINITY_BY_AGENT = defaultdict(list)
for a, s, t, w in AGENT_TOOL_AFFINITY:
    AFFINITY_BY_AGENT[a].append((s, t, w))


def _weighted(pairs):
    tot = sum(w for _, w in pairs)
    r = random.random() * tot
    c = 0.0
    for v, w in pairs:
        c += w
        if r <= c:
            return v
    return pairs[-1][0]


def _pick_agent():
    return _weighted([(a["agent_id"], a["avg_daily_sessions"]) for a in AGENTS])


def _recovery_turns(bug, server):
    """Turns the agent needs to work around the bug.

    Anchored on the published 4.6-12.1 turn range driven by error-message quality.
    A server that already coerces input neutralises most of the damage.
    """
    base = MSG_QUALITY_BASE[bug["message_quality"]] + MISMATCH_PENALTY[bug["mismatch_type"]]
    if server.get("has_input_coercion"):
        base *= COERCION_RECOVERY_MULTIPLIER
    return max(1, int(round(random.gauss(base, 0.9))))


def _arg_sig(tool_key, variant):
    """Argument shape the model chose. variant>0 means it reshaped and retried."""
    decl = TOOL_BY_KEY[tool_key]["expected_args"]
    keys = sorted(decl)
    if variant == 0:
        return "|".join(f"{k}:{decl[k]}" for k in keys)
    # on retry the model mutates the shape of one parameter
    mutated = dict(decl)
    if keys:
        pk = keys[variant % len(keys)]
        mutated[pk] = f"alt{variant}"
    return "|".join(f"{k}:{mutated[k]}" for k in keys)


def simulate_session(bug=None):
    """One agent conversation. Returns the span-derived behavioural record.

    Only quantities genuinely reconstructible from a span tree are recorded.
    """
    agent_id = _pick_agent()
    agent = AGENT_BY_ID[agent_id]
    opts = AFFINITY_BY_AGENT[agent_id]

    # ---- session archetype: this is what creates genuine CLASS OVERLAP ----
    # Without a long tail of legitimately complex clean sessions, "long session" alone
    # separates the classes perfectly and the experiment measures the simulator rather
    # than the model. Real traffic contains chatty-but-healthy sessions (multi-step
    # planning, pagination) and lucky one-retry recoveries. Both are simulated here.
    pag_key = None
    if bug:
        # route the session through an agent that actually owns the buggy tool
        owners = list(bug["agent_ids"]) or [agent_id]
        agent_id = random.choice(owners)
        agent = AGENT_BY_ID[agent_id]
        opts = AFFINITY_BY_AGENT[agent_id]
        server = SERVER_BY_NAME[bug["server"]]
        if random.random() < 0.22:
            archetype = "lucky"          # model guesses an accepted shape almost at once
            struggle = random.randint(1, 2)
        else:
            archetype = "struggle"
            struggle = _recovery_turns(bug, server)
        n_turns = struggle + random.randint(1, 2)
    else:
        struggle = 0
        archetype = _weighted([("simple", 0.70), ("multi_step", 0.22), ("paginated", 0.08)])
        n_turns = {"simple":     lambda: random.randint(1, 3),
                   "multi_step": lambda: random.randint(4, 8),
                   "paginated":  lambda: random.randint(3, 12)}[archetype]()
        if archetype == "paginated":
            # one tool, called over and over with a STABLE shape -- the closest healthy
            # behavioural analogue to a retry loop, and the hardest negative to reject
            _s, _t, _w = _weighted([((s, t, w), w) for s, t, w in opts])
            pag_key = (_s, _t)

    first_in = random.randint(*TOKENS_FIRST_TURN)
    tool_calls = []          # (turn, server, tool, arg_sig, latency_ms)
    turn_tokens = []

    for turn in range(n_turns):
        # context accumulates -> every extra turn costs more than the last
        tin = int(first_in * (1 + TOKEN_GROWTH_PER_TURN * turn))
        tout = random.randint(*OUTPUT_TOKENS)
        turn_tokens.append(tin + tout)

        in_struggle = bug is not None and turn < struggle
        if in_struggle:
            key = (bug["server"], bug["tool"])
            # silent bug: args stay VALID, so the model re-queries with the same shape.
            # loud bug: the model reshapes arguments on each retry -> signature churn.
            variant = 0 if bug["is_silent"] else turn
            lat = TOOL_BY_KEY[key]["sla_p99_ms"]
            tool_calls.append((turn, key[0], key[1],
                               _arg_sig(key, variant),
                               int(random.gauss(lat * 0.6, lat * 0.15))))
            if bug["mismatch_type"] == "timeout_split":
                # the batch-timeout family fans one call out into many small ones
                for _ in range(random.randint(6, 14)):
                    tool_calls.append((turn, key[0], key[1], _arg_sig(key, 0),
                                       int(random.gauss(400, 80))))
            if bug["mismatch_type"] == "rate_limit":
                for _ in range(random.randint(2, 5)):
                    tool_calls.append((turn, key[0], key[1], _arg_sig(key, 0),
                                       int(random.gauss(300, 60))))
        elif pag_key is not None:
            # legitimate pagination -- repeated calls, stable argument shape, no churn
            lat = TOOL_BY_KEY[pag_key]["sla_p99_ms"]
            for _ in range(random.randint(1, 3)):
                tool_calls.append((turn, pag_key[0], pag_key[1], _arg_sig(pag_key, 0),
                                   int(random.gauss(lat * 0.5, lat * 0.2))))
        else:
            for _ in range(random.randint(0, 2)):
                s, t, _w = _weighted([((s, t, w), w) for s, t, w in opts])
                k = (s, t)
                lat = TOOL_BY_KEY[k]["sla_p99_ms"]
                tool_calls.append((turn, s, t, _arg_sig(k, 0),
                                   int(random.gauss(lat * 0.5, lat * 0.2))))

    return dict(
        agent_id=agent_id,
        endpoint=agent["endpoint"],
        n_turns=n_turns,
        turn_tokens=turn_tokens,
        tool_calls=tool_calls,
        # ---- ground truth, never a feature ----
        _gt_bug_id=bug["bug_id"] if bug else None,
        _gt_is_silent=bool(bug and bug["is_silent"]),
        _gt_is_loud=bool(bug and not bug["is_silent"]),
        _gt_struggle_turns=struggle,
        _gt_archetype=archetype,
        # ---- what an error log would have seen ----
        _emitted_error=bool(bug and not bug["is_silent"]),
    )

sessions  = [simulate_session() for _ in range(N_CLEAN)]
for b in LOUD_BUGS:
    sessions += [simulate_session(b) for _ in range(N_PER_LOUD_BUG)]
sessions += [simulate_session(SILENT) for _ in range(N_SILENT)]

print(f"simulated {len(sessions):,} sessions  "
      f"(clean={N_CLEAN:,}  loud={len(LOUD_BUGS)*N_PER_LOUD_BUG:,}  silent={N_SILENT:,})")

# COMMAND ----------

# DBTITLE 1,Behavioural feature extraction (span-tree only)
def featurize(s):
    tc = s["tool_calls"]
    by_tool = Counter((srv, tl) for _t, srv, tl, _a, _l in tc)
    sigs_per_tool = defaultdict(set)
    for _t, srv, tl, sig, _l in tc:
        sigs_per_tool[(srv, tl)].add(sig)

    # longest run of consecutive calls to the same tool
    longest = cur = 0
    prev = None
    for _t, srv, tl, _a, _l in tc:
        if (srv, tl) == prev:
            cur += 1
        else:
            cur, prev = 1, (srv, tl)
        longest = max(longest, cur)

    lats = [l for _t, _s, _tl, _a, l in tc] or [0]
    servers = {srv for _t, srv, _tl, _a, _l in tc}
    docq = [SERVER_BY_NAME[srv]["doc_quality"] for srv in servers] or ["good"]
    docq_rank = {"good": 0, "fair": 1, "poor": 2}
    toks = s["turn_tokens"] or [0]

    return dict(
        # conversation shape
        n_turns               = s["n_turns"],
        n_tool_calls          = len(tc),
        tool_calls_per_turn   = len(tc) / max(s["n_turns"], 1),
        n_distinct_tools      = len(by_tool),
        n_distinct_servers    = len(servers),
        # repetition -- the struggle signature
        max_calls_one_tool    = max(by_tool.values()) if by_tool else 0,
        max_consecutive_same  = longest,
        repeat_ratio          = (max(by_tool.values()) / len(tc)) if tc else 0.0,
        # argument churn -- reshaping on retry
        max_arg_sigs_one_tool = max((len(v) for v in sigs_per_tool.values()), default=0),
        total_arg_sig_churn   = sum(max(0, len(v) - 1) for v in sigs_per_tool.values()),
        # cost shape -- context accumulation
        total_tokens          = sum(toks),
        token_growth_ratio    = (toks[-1] / toks[0]) if toks[0] else 1.0,
        mean_turn_tokens      = float(np.mean(toks)),
        # latency
        mean_tool_latency_ms  = float(np.mean(lats)),
        max_tool_latency_ms   = float(np.max(lats)),
        # static, pre-call-knowable server properties
        worst_doc_quality     = max(docq_rank[d] for d in docq),
        any_server_no_coercion= int(any(not SERVER_BY_NAME[srv]["has_input_coercion"]
                                        for srv in servers)) if servers else 0,
    )

rows = []
for s in sessions:
    r = featurize(s)
    r.update({k: v for k, v in s.items() if k.startswith("_gt") or k == "_emitted_error"})
    r["agent_id"] = s["agent_id"]
    rows.append(r)

df = pd.DataFrame(rows)
FEATURES = [c for c in df.columns
            if not c.startswith("_gt") and c not in ("_emitted_error", "agent_id")]

# --- anti-leakage assertions: fail loudly rather than quietly cheat ---
BANNED = ("error", "status", "bug", "silent", "message_quality", "csat", "outcome", "resolution")
for f in FEATURES:
    assert not any(b in f.lower() for b in BANNED), f"LEAKY FEATURE: {f}"
assert "_emitted_error" not in FEATURES and "_gt_is_silent" not in FEATURES

print(f"{len(FEATURES)} features: {FEATURES}")

# COMMAND ----------

# DBTITLE 1,Train on LOUD only / test on the held-out SILENT family
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import GroupKFold, train_test_split
from sklearn.metrics import (precision_score, recall_score, f1_score,
                             roc_auc_score, average_precision_score,
                             precision_recall_curve)

clean  = df[df["_gt_bug_id"].isna()]
loud   = df[df["_gt_is_loud"]]
silent = df[df["_gt_is_silent"]]

# split the clean pool so train and test never share a negative
clean_shuf = clean.sample(frac=1.0, random_state=DERISK_SEED)
cut = len(clean_shuf) // 2
clean_tr, clean_te = clean_shuf.iloc[:cut], clean_shuf.iloc[cut:]

train = pd.concat([clean_tr, loud])
test  = pd.concat([clean_te, silent])
y_tr  = train["_gt_is_loud"].astype(int).values
y_te  = test["_gt_is_silent"].astype(int).values

# the silent family must be absent from training in every form
assert not train["_gt_is_silent"].any(), "silent family leaked into training"

# Hold back a calibration slice of the TRAINING data. The decision threshold is chosen
# there -- on loud+clean only -- and then applied unchanged to the silent test set.
# Tuning the threshold on the test set would be leakage dressed up as a result.
X_fit, X_cal, y_fit, y_cal = train_test_split(
    train[FEATURES].values, y_tr, test_size=0.25,
    random_state=DERISK_SEED, stratify=y_tr)

model = HistGradientBoostingClassifier(
    max_iter=300, learning_rate=0.08, max_depth=6,
    min_samples_leaf=25, l2_regularization=1.0, random_state=DERISK_SEED)
model.fit(X_fit, y_fit)

def _best_f1_threshold(y, p):
    best_t, best_f1 = 0.5, -1.0
    for t in np.linspace(0.05, 0.95, 91):
        f = f1_score(y, (p >= t).astype(int), zero_division=0)
        if f > best_f1:
            best_f1, best_t = f, float(t)
    return best_t, best_f1

THRESH, cal_f1 = _best_f1_threshold(y_cal, model.predict_proba(X_cal)[:, 1])
print(f"operating point chosen on calibration slice: threshold={THRESH:.3f} (cal F1={cal_f1:.3f})")

p_model = model.predict_proba(test[FEATURES].values)[:, 1]

# interpretable comparator, same protocol
lr = make_pipeline(StandardScaler(),
                   LogisticRegression(max_iter=2000, random_state=DERISK_SEED))
lr.fit(X_fit, y_fit)
THRESH_LR, _ = _best_f1_threshold(y_cal, lr.predict_proba(X_cal)[:, 1])
p_lr = lr.predict_proba(test[FEATURES].values)[:, 1]

# BASELINE: what an error-log query would flag. Silent sessions emit nothing.
p_base = test["_emitted_error"].astype(int).values


def _precision_at_recall(y, p, target):
    """Precision achievable while still catching `target` share of the waste."""
    try:
        pr, rc, _ = precision_recall_curve(y, p)
    except ValueError:
        return None
    ok = [pp for pp, rr in zip(pr, rc) if rr >= target]
    return round(float(max(ok)), 4) if ok else None


def _topk_precision(y, p, k_frac):
    """If the team only reviews the top k% of sessions, how many are real waste?"""
    n = max(1, int(len(p) * k_frac))
    idx = np.argsort(p)[::-1][:n]
    return round(float(y[idx].mean()), 4)


def scores(name, y, p, thresh):
    yhat = (p >= thresh).astype(int)
    out = dict(
        approach   = name,
        threshold  = round(float(thresh), 3),
        precision  = round(float(precision_score(y, yhat, zero_division=0)), 4),
        recall     = round(float(recall_score(y, yhat, zero_division=0)), 4),
        f1         = round(float(f1_score(y, yhat, zero_division=0)), 4),
        caught     = int(yhat[y == 1].sum()),
        of_total   = int((y == 1).sum()),
        # threshold-free views -- what the team would actually operate on
        prec_at_recall_50 = _precision_at_recall(y, p, 0.50),
        prec_at_recall_80 = _precision_at_recall(y, p, 0.80),
        precision_top_5pct  = _topk_precision(y, p, 0.05),
        precision_top_10pct = _topk_precision(y, p, 0.10),
    )
    try:
        out["auc"]    = round(float(roc_auc_score(y, p)), 4)
        out["avg_pr"] = round(float(average_precision_score(y, p)), 4)
    except ValueError:
        out["auc"] = out["avg_pr"] = None
    return out

res_model = scores("GBM (behavioural)",        y_te, p_model, THRESH)
res_lr    = scores("LogReg (behavioural)",     y_te, p_lr,    THRESH_LR)
res_base  = scores("Baseline: grep error log", y_te, p_base,  0.5)

_cols = ["approach","auc","avg_pr","precision","recall","f1","caught","of_total",
         "prec_at_recall_50","prec_at_recall_80","precision_top_5pct"]
print(pd.DataFrame([res_model, res_lr, res_base])[_cols].to_string(index=False))

# COMMAND ----------

# DBTITLE 1,Does it generalise ACROSS bug families? (leave-one-family-out)
# Guards against the model memorising one family's signature rather than learning
# the mechanism. Each fold holds out a whole loud family.
# BUG FIXED: previously every clean row shared the group "__clean__", so GroupKFold put
# all negatives in a single fold -- that fold had no positives, the rest had no negatives,
# and every fold was skipped (the metric silently returned null). Clean rows are now
# spread across synthetic groups so each fold holds both classes.
N_SPLITS = 5
_clean_grp = clean_tr.assign(
    _grp=[f"__clean_{i % N_SPLITS}__" for i in range(len(clean_tr))])
_loud_grp = loud.assign(_grp=loud["_gt_bug_id"])
grp_src = pd.concat([_clean_grp, _loud_grp]).reset_index(drop=True)

lofo, lofo_detail = [], []
gkf = GroupKFold(n_splits=N_SPLITS)
for tr_i, te_i in gkf.split(grp_src[FEATURES], grp_src["_gt_is_loud"].astype(int),
                            groups=grp_src["_grp"]):
    a, b = grp_src.iloc[tr_i], grp_src.iloc[te_i]
    yb = b["_gt_is_loud"].astype(int).values
    held = sorted(x for x in b["_gt_bug_id"].dropna().unique())
    if yb.min() == yb.max() or not held:
        continue                      # fold lacks one class -> uninformative
    m = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.08, max_depth=6,
                                       min_samples_leaf=25, random_state=DERISK_SEED)
    m.fit(a[FEATURES].values, a["_gt_is_loud"].astype(int).values)
    auc = round(float(roc_auc_score(yb, m.predict_proba(b[FEATURES].values)[:, 1])), 4)
    lofo.append(auc)
    lofo_detail.append({"held_out_families": held, "auc": auc})

print(f"leave-one-family-out AUCs: {lofo}")
print(f"mean {np.mean(lofo):.4f}" if lofo else "no informative folds")
for d in lofo_detail:
    print(f"  auc={d['auc']:.4f}  held out: {', '.join(d['held_out_families'])}")
assert lofo, "LOFO produced no informative folds -- fold construction is broken again"

# COMMAND ----------

# DBTITLE 1,Which behaviours carry the signal?
from sklearn.inspection import permutation_importance
imp = permutation_importance(model, test[FEATURES].values, y_te,
                             n_repeats=5, random_state=DERISK_SEED, scoring="roc_auc")
top = sorted(zip(FEATURES, imp.importances_mean), key=lambda kv: -kv[1])[:8]
for f, v in top:
    print(f"  {f:24} {v:+.4f}")

# COMMAND ----------

# DBTITLE 1,Verdict
GO_AUC, GO_RECALL = 0.75, 0.50
passed = (res_model["auc"] or 0) >= GO_AUC and res_model["recall"] >= GO_RECALL

verdict = dict(
    decision          = "GO" if passed else "NO-GO",
    gate              = f"auc>={GO_AUC} and recall>={GO_RECALL} on the held-out silent family",
    model             = res_model,
    logreg            = res_lr,
    baseline          = res_base,
    baseline_recall   = res_base["recall"],
    lofo_auc_mean     = round(float(np.mean(lofo)), 4) if lofo else None,
    lofo_folds        = lofo_detail,
    operating_point   = round(float(THRESH), 3),
    top_features      = [f for f, _ in top[:5]],
    held_out_family   = SILENT["bug_id"],
    silent_sessions   = int((y_te == 1).sum()),
    clean_archetype_mix = {k: int(v) for k, v in
                           df[df["_gt_bug_id"].isna()]["_gt_archetype"].value_counts().items()},
    bug_archetype_mix   = {k: int(v) for k, v in
                           df[df["_gt_bug_id"].notna()]["_gt_archetype"].value_counts().items()},
    overlap_check       = {
        "clean_p95_turns":  float(df[df["_gt_bug_id"].isna()]["n_turns"].quantile(0.95)),
        "bug_p05_turns":    float(df[df["_gt_bug_id"].notna()]["n_turns"].quantile(0.05)),
    },
    # the honest caveat: the silent family is the only unit_mismatch in the taxonomy,
    # so the model must extrapolate from other dimensions rather than a near neighbour.
    feature_neighbours_for_silent = sum(
        1 for b in LOUD_BUGS if b["mismatch_type"] == SILENT["mismatch_type"]),
)
print(json.dumps(verdict, indent=2))
dbutils.notebook.exit(json.dumps(verdict))
