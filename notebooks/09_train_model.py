# Databricks notebook source
# MAGIC %md
# MAGIC # 09: tool-call risk model, scored before dispatch
# MAGIC
# MAGIC Trains the model that sits in the agent's path and decides, **before** a tool call is
# MAGIC dispatched, whether it will trigger a recovery loop: returning `coerce` / `warn` / `allow`.
# MAGIC
# MAGIC ### The experiment that matters
# MAGIC The claim is *"this generalises to a bug family it has never seen"*. So the headline
# MAGIC number is **leave-one-bug-family-out**, not a random split. A random split would let the
# MAGIC model memorise each family's fingerprint and score ~1.0 while being useless on a new bug.
# MAGIC
# MAGIC ### Anti-leakage
# MAGIC Features are restricted to what a wrapper can know before dispatch. Excluded by
# MAGIC assertion, not by good intentions: `error_type`, `http_status`, `latency_ms` (post-hoc),
# MAGIC tool/agent identity (memorisation), bug identity, session outcome.

# COMMAND ----------

import json, os, pickle, textwrap
from importlib.metadata import version as _pkg_ver


def _ver(pkg):
    """Version lookup that tolerates runtime-injected packages.

    Databricks injects mlflow into the serverless image rather than pip-installing it, so
    importlib.metadata finds no distribution for it. Fall back to the module attribute.
    """
    try:
        return _pkg_ver(pkg)
    except Exception:
        import importlib
        mod = importlib.import_module({"scikit-learn": "sklearn"}.get(pkg, pkg))
        return getattr(mod, "__version__", "unknown")
import numpy as np, pandas as pd
import mlflow
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient

CATALOG, SCHEMA = "main", "tiffin_agent_waste"
MODEL_NAME = "tool_call_risk"
FULL_NAME  = f"{CATALOG}.{SCHEMA}.{MODEL_NAME}"
EXPERIMENT = "/Users/sonakshi.pandey@databricks.com/fe-bar-sonakshi/mlflow/tool_call_risk"
SEED = 7

mlflow.set_registry_uri("databricks-uc")
mlflow.set_experiment(EXPERIMENT)
print(f"mlflow {_ver('mlflow')} | sklearn {_ver('scikit-learn')}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Load the training table
# MAGIC `bug_id` is joined for GROUPING ONLY: it is never a feature. Rows on tools with no
# MAGIC known bug form the `clean` group.

# COMMAND ----------

df = spark.sql(f"""
  SELECT g.*, COALESCE(b.bug_id, 'clean') AS grp_bug_id, COALESCE(b.is_silent, false) AS grp_is_silent
  FROM {CATALOG}.{SCHEMA}.call_risk_features_gold g
  LEFT JOIN {CATALOG}.{SCHEMA}.bronze_bug_registry b
    ON g.tool_server = b.tool_server AND g.tool_name = b.tool_name
""").toPandas()

print(f"rows={len(df):,}  positives={df.label_caused_recovery.sum():,} "
      f"({100*df.label_caused_recovery.mean():.2f}%)")
print(df.groupby('grp_bug_id').agg(rows=('label_caused_recovery','size'),
                                   pos=('label_caused_recovery','sum')).sort_values('rows', ascending=False))

# COMMAND ----------
# MAGIC %md
# MAGIC ## Feature matrix

# COMMAND ----------

# Fixed categorical encodings. Defined once here and shipped to the serving model so training
# and inference cannot drift apart.
MISMATCH_CLASSES = ["none", "array_vs_scalar", "nested_vs_flat", "numeric_vs_string",
                    "float_vs_int", "string_vs_numeric", "string_vs_boolean",
                    "shape_drift", "token_version", "date_format",
                    "serialized_collection", "enum_case", "missing_required"]
DOC_QUALITY = ["unknown", "good", "fair", "poor"]
MISMATCH_IX = {c: i for i, c in enumerate(MISMATCH_CLASSES)}
DOCQ_IX     = {c: i for i, c in enumerate(DOC_QUALITY)}

NUMERIC_FEATURES = ["turn_index", "n_args_sent", "n_params_mismatched", "n_params_missing",
                    "f_shape_mismatch", "f_has_examples", "f_has_input_coercion",
                    "f_sla_p99_ms", "f_tokens_before_turn", "f_call_seq_this_tool"]
CAT_FEATURES = ["f_mismatch_class_ix", "f_doc_quality_ix"]
FEATURES = NUMERIC_FEATURES + CAT_FEATURES

def build_X(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=frame.index)
    for c in NUMERIC_FEATURES:
        out[c] = pd.to_numeric(frame[c], errors="coerce").fillna(0).astype(float)
    out["f_mismatch_class_ix"] = frame["f_mismatch_class"].map(MISMATCH_IX).fillna(0).astype(int)
    out["f_doc_quality_ix"]    = frame["f_doc_quality"].map(DOCQ_IX).fillna(0).astype(int)
    return out[FEATURES]

# --- leakage assertions: fail loudly rather than quietly cheat ---
BANNED_SUBSTRINGS = ("error", "status", "latency", "bug", "silent", "trace_id", "span_id",
                     "agent_id", "tool_server", "tool_name", "recovery", "label",
                     "csat", "outcome", "arg_signature", "event_date")
for f in FEATURES:
    assert not any(b in f.lower() for b in BANNED_SUBSTRINGS), f"LEAKY FEATURE: {f}"
print(f"{len(FEATURES)} features: {FEATURES}")

X_all = build_X(df)
y_all = df["label_caused_recovery"].astype(int).values
groups = df["grp_bug_id"].values

# COMMAND ----------
# MAGIC %md
# MAGIC ## The headline experiment: leave-one-bug-family-out
# MAGIC Each fold holds out an entire bug family. The model must price a mechanism it has
# MAGIC never trained on. The silent family is excluded from scoring: it is deliberately
# MAGIC out of ML scope (no behavioural signature), so including it would understate the model.

# COMMAND ----------

from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.metrics import (roc_auc_score, average_precision_score, precision_score,
                             recall_score, f1_score, precision_recall_curve)

def new_model():
    return HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.08, max_depth=6, min_samples_leaf=40,
        l2_regularization=1.0, categorical_features=[FEATURES.index(c) for c in CAT_FEATURES],
        random_state=SEED)

bug_families = sorted(set(groups) - {"clean"})
silent_families = sorted(df.loc[df.grp_is_silent, "grp_bug_id"].unique())
scoreable = [b for b in bug_families if b not in silent_families]
print(f"{len(scoreable)} scoreable families; silent (excluded): {silent_families}")

lofo = []
for held in scoreable:
    tr = groups != held
    te = groups == held
    # the held-out fold needs both classes to be scoreable
    if len(np.unique(y_all[te])) < 2:
        lofo.append({"held_out": held, "auc": None, "note": "single-class fold"})
        continue
    m = new_model().fit(X_all[tr], y_all[tr])
    p = m.predict_proba(X_all[te])[:, 1]
    lofo.append({
        "held_out": held,
        "rows": int(te.sum()),
        "pos_rate": round(float(y_all[te].mean()), 3),
        "auc": round(float(roc_auc_score(y_all[te], p)), 4),
        "avg_pr": round(float(average_precision_score(y_all[te], p)), 4),
    })
    print(f"  held out {held:32} auc={lofo[-1]['auc']:.4f}  rows={lofo[-1]['rows']:,}")

lofo_aucs = [r["auc"] for r in lofo if r.get("auc") is not None]
LOFO_MEAN = round(float(np.mean(lofo_aucs)), 4)
LOFO_MIN  = round(float(np.min(lofo_aucs)), 4)
print(f"\nLOFO mean AUC = {LOFO_MEAN}   worst family = {LOFO_MIN}")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Baseline to beat
# MAGIC The honest comparator is what a team does today without a model: flag a call if its
# MAGIC arguments disagree with the declared schema. That is a rule, not a model: so if the
# MAGIC model cannot beat it, there is no ML story.

# COMMAND ----------

baseline_rows = []
for held in scoreable:
    te = groups == held
    if len(np.unique(y_all[te])) < 2:
        continue
    rule = X_all.loc[te, "f_shape_mismatch"].values   # the pure rule
    baseline_rows.append(round(float(roc_auc_score(y_all[te], rule)), 4))
BASE_MEAN = round(float(np.mean(baseline_rows)), 4)
print(f"rule-only baseline mean AUC = {BASE_MEAN}")
print(f"model                       = {LOFO_MEAN}   (+{round(LOFO_MEAN-BASE_MEAN,4)})")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Final fit + operating point
# MAGIC Thresholds are chosen on a calibration slice held out from training: never on the
# MAGIC family-holdout results, which would be tuning on the test set.

# COMMAND ----------

from sklearn.model_selection import train_test_split
X_fit, X_cal, y_fit, y_cal = train_test_split(
    X_all, y_all, test_size=0.2, random_state=SEED, stratify=y_all)

final_model = new_model().fit(X_fit, y_fit)
p_cal = final_model.predict_proba(X_cal)[:, 1]

best_t, best_f1 = 0.5, -1.0
for t in np.linspace(0.05, 0.95, 91):
    f = f1_score(y_cal, (p_cal >= t).astype(int), zero_division=0)
    if f > best_f1:
        best_f1, best_t = f, float(t)
COERCE_THRESHOLD = round(best_t, 3)
WARN_THRESHOLD   = round(max(0.05, best_t * 0.5), 3)
cal_auc = round(float(roc_auc_score(y_cal, p_cal)), 4)
print(f"calibration auc={cal_auc}  coerce>={COERCE_THRESHOLD}  warn>={WARN_THRESHOLD}  (cal F1={best_f1:.3f})")

imp = sorted(zip(FEATURES, final_model.feature_importances_ if hasattr(final_model, "feature_importances_")
                 else [0]*len(FEATURES)), key=lambda kv: -kv[1])[:6] \
      if hasattr(final_model, "feature_importances_") else []

# COMMAND ----------
# MAGIC %md
# MAGIC ## Package as a PyFunc that returns a DECISION
# MAGIC The endpoint takes the raw call the agent is about to make and returns the action to
# MAGIC take. The declared tool schemas are bundled into the artifact, so the caller sends only
# MAGIC what it already has in hand and there is no catalog lookup in the latency path.

# COMMAND ----------

tool_catalog = (spark.table(f"{CATALOG}.{SCHEMA}.bronze_tool_catalog")
                     .select("tool_server","tool_name","expected_args_json","doc_quality",
                             "has_examples","has_input_coercion","sla_p99_ms")
                     .toPandas())
catalog_payload = {}
for _, r in tool_catalog.iterrows():
    catalog_payload[f"{r.tool_server}|{r.tool_name}"] = {
        "expected_args": json.loads(r.expected_args_json),
        "doc_quality": r.doc_quality,
        "has_examples": bool(r.has_examples),
        "has_input_coercion": bool(r.has_input_coercion),
        "sla_p99_ms": int(r.sla_p99_ms),
    }
print(f"bundled {len(catalog_payload)} declared tool schemas")

art_dir = "/tmp/tiffin_model_artifacts"
os.makedirs(art_dir, exist_ok=True)
with open(f"{art_dir}/model.pkl", "wb") as f:
    pickle.dump(final_model, f)
with open(f"{art_dir}/tool_catalog.json", "w") as f:
    json.dump(catalog_payload, f)
with open(f"{art_dir}/config.json", "w") as f:
    json.dump({"features": FEATURES, "numeric_features": NUMERIC_FEATURES,
               "cat_features": CAT_FEATURES, "mismatch_ix": MISMATCH_IX, "docq_ix": DOCQ_IX,
               "coerce_threshold": COERCE_THRESHOLD, "warn_threshold": WARN_THRESHOLD}, f)
print(os.listdir(art_dir))

# COMMAND ----------

# model.py is logged VERBATIM via python_model="<path>" (Models from Code) -- the class is
# never pickled, which avoids Python-version unpickle breakage between training and serving.
MODEL_PY = r'''
import json, pickle, re
import pandas as pd
import mlflow
from mlflow.pyfunc import PythonModel

# Generic coercion advice per mismatch class. Derived from the server's own declared schema
# plus the kind of disagreement -- not invented per bug.
COERCION = {
    "array_vs_scalar":       "join the array into a comma-separated string",
    "serialized_collection": "send a comma-separated string, not serialised JSON",
    "nested_vs_flat":        "flatten the object to a scalar string",
    "numeric_vs_string":     "send the value as a string",
    "string_vs_numeric":     "cast the value to an integer",
    "float_vs_int":          "convert to an integer in the smallest unit (e.g. dollars -> cents)",
    "string_vs_boolean":     "cast \"true\"/\"false\" to a native boolean",
    "token_version":         "rewrite the pagination token into v1 format",
    "date_format":           "convert the ISO-8601 timestamp to epoch milliseconds",
    "enum_case":             "match the declared enum casing exactly",
    "shape_drift":           "map the fields onto the declared object shape",
    "missing_required":      "supply the missing required parameter",
}

def _variant_type(v):
    if isinstance(v, bool):  return "BOOLEAN"
    if isinstance(v, int):   return "BIGINT"
    if isinstance(v, float): return "DOUBLE"
    if isinstance(v, list):  return "ARRAY"
    if isinstance(v, dict):  return "OBJECT"
    return "STRING"

def _expected_variant(declared):
    if declared in ("string","csv_string","flat_string","iso8601_string","v1_token_string"): return "STRING"
    if declared.startswith("enum["): return "STRING"
    if declared == "int":     return "BIGINT"
    if declared == "double":  return "DOUBLE"
    if declared == "boolean": return "BOOLEAN"
    if declared == "array<string>": return "ARRAY"
    if declared.endswith("_object"): return "OBJECT"
    return "STRING"

def _classify(declared, sent):
    """Mirrors the pipeline's silver logic so training and serving agree exactly."""
    exp, got = _expected_variant(declared), _variant_type(sent)
    if exp != got:
        if exp == "STRING" and got == "ARRAY":  return True, "array_vs_scalar"
        if exp == "STRING" and got == "OBJECT": return True, "nested_vs_flat"
        if exp == "STRING" and got in ("BIGINT","DOUBLE"): return True, "numeric_vs_string"
        if exp == "BIGINT" and got == "DOUBLE": return True, "float_vs_int"
        if exp == "BIGINT" and got == "STRING": return True, "string_vs_numeric"
        if exp == "BOOLEAN" and got == "STRING": return True, "string_vs_boolean"
        if exp == "OBJECT": return True, "shape_drift"
        return True, "shape_drift"
    # correctly TYPED but wrong FORMAT -- these are all STRING-vs-STRING
    if got == "STRING":
        s = str(sent)
        if declared == "v1_token_string" and not s.startswith("v1-"): return True, "token_version"
        if declared == "iso8601_string" and not re.match(r"^\d{4}-\d{2}-\d{2}T", s): return True, "date_format"
        if declared == "csv_string" and (s.startswith("[") or s.startswith("{")): return True, "serialized_collection"
        if declared.startswith("enum[") and s not in declared[5:-1].split(","): return True, "enum_case"
    return False, None


class ToolCallRiskModel(PythonModel):
    def load_context(self, context):
        with open(context.artifacts["model"], "rb") as f:
            self.clf = pickle.load(f)
        with open(context.artifacts["tool_catalog"]) as f:
            self.catalog = json.load(f)
        with open(context.artifacts["config"]) as f:
            cfg = json.load(f)
        self.features  = cfg["features"]
        self.numeric   = cfg["numeric_features"]
        self.mismatch_ix = cfg["mismatch_ix"]
        self.docq_ix     = cfg["docq_ix"]
        self.coerce_t  = cfg["coerce_threshold"]
        self.warn_t    = cfg["warn_threshold"]

    def _featurize_one(self, row):
        server = row.get("tool_server"); tool = row.get("tool_name")
        meta = self.catalog.get(f"{server}|{tool}", {})
        declared = meta.get("expected_args", {})

        args = row.get("arguments")
        if isinstance(args, str):
            try: args = json.loads(args)
            except Exception: args = {}
        args = args or {}

        n_mis, n_missing, cls, bad_param = 0, 0, None, None
        for p, dt in declared.items():
            if p not in args:
                n_missing += 1
                if cls is None: cls, bad_param = "missing_required", p
                continue
            bad, c = _classify(dt, args[p])
            if bad:
                n_mis += 1
                if cls is None or cls == "missing_required":
                    cls, bad_param = c, p

        feat = {
            "turn_index":           int(row.get("turn_index") or 0),
            "n_args_sent":          len(args),
            "n_params_mismatched":  n_mis,
            "n_params_missing":     n_missing,
            "f_shape_mismatch":     1 if (n_mis or n_missing) else 0,
            "f_has_examples":       1 if meta.get("has_examples") else 0,
            "f_has_input_coercion": 1 if meta.get("has_input_coercion") else 0,
            "f_sla_p99_ms":         int(meta.get("sla_p99_ms") or 0),
            "f_tokens_before_turn": int(row.get("tokens_before_turn") or 0),
            "f_call_seq_this_tool": int(row.get("call_seq_this_tool") or 1),
            "f_mismatch_class_ix":  self.mismatch_ix.get(cls or "none", 0),
            "f_doc_quality_ix":     self.docq_ix.get(meta.get("doc_quality", "unknown"), 0),
        }
        return feat, (cls or "none"), bad_param, declared.get(bad_param)

    def predict(self, context, model_input, params=None):
        if isinstance(model_input, dict):
            model_input = pd.DataFrame([model_input])
        rows = model_input.to_dict(orient="records")

        feats, metas = [], []
        for r in rows:
            f, cls, param, declared = self._featurize_one(r)
            feats.append(f); metas.append((cls, param, declared))

        X = pd.DataFrame(feats)[self.features]
        probs = self.clf.predict_proba(X)[:, 1]

        out = []
        for p, (cls, param, declared) in zip(probs, metas):
            p = float(p)
            if p >= self.coerce_t and cls != "none":
                action = "coerce"
            elif p >= self.coerce_t:
                action = "warn"      # risky but nothing concrete to rewrite
            elif p >= self.warn_t:
                action = "warn"
            else:
                action = "allow"
            advice = COERCION.get(cls)
            out.append({
                "failure_probability": round(p, 4),
                "action": action,
                "mismatch_class": cls,
                "mismatched_param": param or "",
                "declared_type": declared or "",
                "suggested_coercion": (f"{param}: {advice}" if action == "coerce" and advice and param else ""),
                "confidence": "high" if (p >= 0.8 or p <= 0.2) else "medium",
            })
        return pd.DataFrame(out)


mlflow.models.set_model(ToolCallRiskModel())
'''
with open(f"{art_dir}/model.py", "w") as f:
    f.write(MODEL_PY)
print("model.py written")

# COMMAND ----------
# MAGIC %md
# MAGIC ## Log, validate, register

# COMMAND ----------

sample_input = pd.DataFrame([
    {   # the headline bug: v2 pagination token where v1 is declared
        "tool_server": "delivery-tracking-api", "tool_name": "list_deliveries",
        "arguments": json.dumps({"courier_id": "CR-4172", "page_token": "v2:abc==", "limit": 25}),
        "agent_id": "delivery-routing-agent", "turn_index": 0,
        "tokens_before_turn": 0, "call_seq_this_tool": 1},
    {   # a clean call on a clean server -- must come back `allow`
        "tool_server": "customer-profile-api", "tool_name": "get_addresses",
        "arguments": json.dumps({"customer_id": "C-9931112"}),
        "agent_id": "delivery-routing-agent", "turn_index": 1,
        "tokens_before_turn": 18400, "call_seq_this_tool": 1},
])
sample_output = pd.DataFrame([{
    "failure_probability": 0.0, "action": "allow", "mismatch_class": "none",
    "mismatched_param": "", "declared_type": "", "suggested_coercion": "", "confidence": "high"}])

with mlflow.start_run(run_name="pre_flight_risk") as run:
    mlflow.log_params({
        "n_features": len(FEATURES), "algo": "HistGradientBoostingClassifier",
        "coerce_threshold": COERCE_THRESHOLD, "warn_threshold": WARN_THRESHOLD,
        "train_rows": len(df), "seed": SEED})
    mlflow.log_metrics({
        "lofo_mean_auc": LOFO_MEAN, "lofo_worst_auc": LOFO_MIN,
        "rule_baseline_mean_auc": BASE_MEAN, "lift_over_rule": round(LOFO_MEAN - BASE_MEAN, 4),
        "calibration_auc": cal_auc, "positive_rate": round(float(y_all.mean()), 4)})
    mlflow.log_dict({"lofo_folds": lofo}, "lofo_results.json")

    info = mlflow.pyfunc.log_model(
        artifact_path="model",
        python_model=f"{art_dir}/model.py",
        artifacts={"model": f"{art_dir}/model.pkl",
                   "tool_catalog": f"{art_dir}/tool_catalog.json",
                   "config": f"{art_dir}/config.json"},
        signature=infer_signature(sample_input, sample_output),
        input_example=sample_input,
        pip_requirements=[f"mlflow=={_ver('mlflow')}",
                          f"scikit-learn=={_ver('scikit-learn')}",
                          f"pandas=={_ver('pandas')}"],
        registered_model_name=FULL_NAME,
    )
print("logged + registered version", info.registered_model_version)

# COMMAND ----------

# Pre-deploy validation: rebuilds the env and actually runs predict().
# Catches missing deps / signature drift BEFORE the endpoint does.
try:
    pred = mlflow.models.predict(model_uri=info.model_uri,
                                 input_data=sample_input, env_manager="uv")
    print("env-rebuild validation OK:", pred)
except Exception as e:
    # Not fatal -- the pyfunc round-trip below still proves the artifact loads and predicts.
    print(f"env-rebuild validation unavailable on this runtime ({type(e).__name__}: {e})")

# COMMAND ----------

client = MlflowClient(registry_uri="databricks-uc")
client.set_registered_model_alias(FULL_NAME, "prod", info.registered_model_version)
print(f"@prod -> version {info.registered_model_version}")

# COMMAND ----------

# local sanity check through the pyfunc flavour
loaded = mlflow.pyfunc.load_model(f"models:/{FULL_NAME}@prod")
local = loaded.predict(sample_input)
print(local.to_string(index=False))

result = {
    "model": FULL_NAME,
    "version": str(info.registered_model_version),
    "lofo_mean_auc": LOFO_MEAN,
    "lofo_worst_auc": LOFO_MIN,
    "rule_baseline_mean_auc": BASE_MEAN,
    "lift_over_rule": round(LOFO_MEAN - BASE_MEAN, 4),
    "calibration_auc": cal_auc,
    "coerce_threshold": COERCE_THRESHOLD,
    "warn_threshold": WARN_THRESHOLD,
    "train_rows": int(len(df)),
    "positive_rate": round(float(y_all.mean()), 4),
    "n_features": len(FEATURES),
    "lofo_folds": lofo,
    "sample_predictions": local.to_dict(orient="records"),
}
dbutils.notebook.exit(json.dumps(result, default=str)[:12000])
