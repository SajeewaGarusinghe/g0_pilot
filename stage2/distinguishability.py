#!/usr/bin/env python3
"""G0 stage 2, step 9a: distinguishability AUC per family on the stage 1 pilot's overlays.

  python stage2/distinguishability.py --out stage2/out/step9a-<date>

Proposal section 06: "An overlay that is trivially distinguishable from genuine records would
hand any defence an artefactual win." A basic gradient-boosted-tree classifier is trained on
record-level features to separate forged records from genuine ones, on development cases, and
tested on **held-out development templates**. The AUC is reported per family; a value well above
chance triggers overlay revision before any test run.

Three choices, stated because they decide what the number means:

1. **Positives** are the forged records themselves, recovered by diffing each overlay case
   against its clean twin (F1 adds log rows; F2 changes `statusCode`; F3 changes `duration`).
   The recovered counts are checked against the sealed `injected` count for every case.
2. **Negatives** are genuine records *of the same target service in the same post-fault window*,
   from the clean twin. Using every other service's records instead would let the classifier win
   on service identity, which is what the attack does rather than an artefact of the generator;
   this isolates "does a forged record look unlike a genuine record of the same kind?".
3. Two feature sets are reported. **Full** includes the field the family forges, which is the
   check as the proposal words it. **Ablated** drops that field, which answers a different and
   also useful question: does the overlay leave a *secondary* fingerprint - in timing, in which
   records it chose - beyond the change it intends to make?

Splitting is grouped by development template, so no template appears in both train and test.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CASES = os.path.join(REPO, "pilot_tasks", "pilot", "cases")
LABELS = os.path.join(REPO, "pilot_sealed", "labels.jsonl")
ANSWERS = os.path.join(REPO, "agent_tasks", "re2-ob.answers.jsonl")
SEED = 20261109          # the pilot's seed, reused for negative sampling
MAX_PER_CLASS_PER_CASE = 5000   # forged and genuine are sampled to the same size per case


CATEGORICAL = ["service", "method", "operation", "parent_service", "level", "container",
               "template"]


def encode_globally(df):
    """Factorise categoricals ONCE over the whole data set.

    Encoding the forged and genuine sets separately gives the same operation different codes in
    each group, which is a difference the classifier can learn and which has nothing to do with
    the overlay - an earlier run of this check reported a fingerprint that was only this.
    """
    for col in CATEGORICAL:
        if col in df.columns:
            df[col] = pd.factorize(df[col].astype(str))[0]
    return df


def span_features(df, inject, drop=None, trace_sizes=None):
    sid = df.spanID.astype(str)
    svc = df.serviceName.astype(str)
    p_svc = df.parentSpanID.astype(str).map(dict(zip(sid, svc)))
    f = pd.DataFrame({
        "duration_us": pd.to_numeric(df.duration, errors="coerce"),
        "log_duration": np.log10(pd.to_numeric(df.duration, errors="coerce").clip(lower=1)),
        "status_code": pd.to_numeric(df.statusCode, errors="coerce").fillna(-1),
        "offset_s": pd.to_numeric(df.startTime, errors="coerce") / 1e6 - inject,
        "service": svc.values, "method": df.methodName.astype(str).values,
        "operation": df.operationName.astype(str).values,
        "parent_service": p_svc.astype(str).values,
        "is_server": (p_svc.notna() & (p_svc != svc)).astype(int),
        # Trace size must come from the WHOLE case, not from the subset being featurised:
        # counting within the subset gives the forged rows one count and the genuine rows
        # another for the same trace, which is a difference the classifier can learn and which
        # says nothing about the overlay.
        "trace_size": df.traceID.astype(str).map(trace_sizes).values if trace_sizes is not None
        else np.nan,
    })
    return f.drop(columns=[c for c in (drop or []) if c in f.columns])


def log_features(df, inject, drop=None):
    f = pd.DataFrame({
        "offset_s": pd.to_numeric(df.timestamp, errors="coerce") / 1e9 - inject,
        "level": df.level.astype(str).values,
        "container": df.container_name.astype(str).values,
        "template": df.get("log_template", pd.Series([""] * len(df))).astype(str).values,
        "cluster_id": pd.to_numeric(df.get("cluster_id", pd.Series([np.nan] * len(df))),
                                    errors="coerce"),
        "msg_len": df.message.astype(str).str.len(),
        "has_error_field": df.get("error", pd.Series([np.nan] * len(df))).notna().astype(int),
    })
    return f.drop(columns=[c for c in (drop or []) if c in f.columns])


# the field each family forges directly; the ablated run drops it
ABLATE = {"F2": ["status_code"], "F3": ["duration_us", "log_duration"],
          "F1": ["template", "cluster_id", "msg_len"]}


def build(out_dir):
    labels = [json.loads(l) for l in open(LABELS)]
    by = {(r["orig"], r["family"]): r for r in labels}
    answers = {json.loads(l)["case_id"]: json.loads(l) for l in open(ANSWERS)}
    origs = sorted({r["orig"] for r in labels})
    rng = np.random.default_rng(SEED)
    frames = {"F1": [], "F2": [], "F3": []}

    for orig in origs:
        clean = by[(orig, "clean")]
        template = f"{answers[orig]['service']}_{answers[orig]['fault']}"
        with open(os.path.join(CASES, clean["case_id"], "task.json")) as f:
            inject = int(json.load(f)["inject_time"])
        ct = pd.read_csv(os.path.join(CASES, clean["case_id"], "traces.csv"), low_memory=False)
        trace_sizes = ct.groupby(ct.traceID.astype(str))["spanID"].size().to_dict()
        cl = None

        for fam in ("F1", "F2", "F3"):
            o = by[(orig, fam)]
            target = o["target"]
            n_genuine_err = ""
            if fam == "F1":
                if cl is None:
                    cl = pd.read_csv(os.path.join(CASES, clean["case_id"], "logs.csv"),
                                     low_memory=False)
                ol = pd.read_csv(os.path.join(CASES, o["case_id"], "logs.csv"), low_memory=False)
                # the overlay appends rows; the clean rows are a prefix by construction, so the
                # added rows are the tail. Verified against the sealed injected count below.
                pos = ol.iloc[len(cl):]
                neg_pool = cl[(cl.container_name.astype(str).str.startswith(target))
                              & ((pd.to_numeric(cl.timestamp, errors="coerce") / 1e9) >= inject)]
                # How many of those are genuine ERROR lines - i.e. is there anything for a forged
                # error line to imitate at all? Recorded per case; see the evidence file.
                n_genuine_err = int((neg_pool.level.astype(str).str.lower()
                                     .isin(["error", "warn", "warning"])).sum())
                feat = log_features
            else:
                ot = pd.read_csv(os.path.join(CASES, o["case_id"], "traces.csv"), low_memory=False)
                key = "statusCode" if fam == "F2" else "duration"
                m = ct[["spanID", key]].merge(ot[["spanID", key]], on="spanID",
                                              suffixes=("_c", "_o"))
                changed = set(m.loc[m[f"{key}_c"].fillna(-1) != m[f"{key}_o"].fillna(-1), "spanID"])
                pos = ot[ot.spanID.isin(changed)]
                # Negatives must come from the pool the overlay actually drew from: the target's
                # *server* spans in the post-fault window. Including its client spans would let
                # the classifier win on is_server - which is the overlay's scope, not a failure
                # of realism - and that alone inflated an earlier run of this check.
                c_sid = ct.spanID.astype(str)
                c_psvc = ct.parentSpanID.astype(str).map(dict(zip(c_sid, ct.serviceName.astype(str))))
                c_is_server = c_psvc.notna() & (c_psvc != ct.serviceName.astype(str))
                neg_pool = ct[(ct.serviceName.astype(str) == target) & c_is_server
                              & (~ct.spanID.isin(changed))
                              & ((pd.to_numeric(ct.startTime, errors="coerce") / 1e6) >= inject)]
                feat = span_features

            n_pos_all = len(pos)
            ok = n_pos_all == o["injected"]
            # Balance WITHIN each case. Capping only the genuine side leaves each case with its
            # own forged:genuine ratio, so any feature that encodes which case a record came
            # from - its service, its operation, its offset - scores above chance without
            # saying anything about the overlay. An earlier run of this check read 0.60 for
            # exactly that reason. Sampling both sides to the same size removes it.
            n = min(n_pos_all, len(neg_pool), MAX_PER_CLASS_PER_CASE)
            if n == 0:
                pos = pos.iloc[:0]
            else:
                if n_pos_all > n:
                    pos = pos.iloc[rng.choice(n_pos_all, n, replace=False)]
                if len(neg_pool) > n:
                    neg_pool = neg_pool.iloc[rng.choice(len(neg_pool), n, replace=False)]
            n_pos = len(pos)
            emit_row = {"case": orig, "family": fam, "template": template, "target": target,
                        "forged_recovered": n_pos_all, "forged_sealed": o["injected"], "forged_used": n_pos,
                        "recovery_matches_sealed": ok, "genuine_pool": len(neg_pool),
                        "genuine_error_lines": n_genuine_err if fam == "F1" else ""}
            with open(os.path.join(out_dir, "recovery.csv"), "a") as f:
                if f.tell() == 0:
                    f.write(",".join(emit_row) + "\n")
                f.write(",".join(str(v) for v in emit_row.values()) + "\n")
            if n_pos == 0 or len(neg_pool) == 0:
                continue
            kw = {"trace_sizes": trace_sizes} if fam != "F1" else {}
            X = pd.concat([feat(pos, inject, **kw), feat(neg_pool, inject, **kw)],
                          ignore_index=True)
            X["_y"] = [1] * n_pos + [0] * len(neg_pool)
            X["_template"] = template
            frames[fam].append(X)
    return {k: encode_globally(pd.concat(v, ignore_index=True))
            for k, v in frames.items() if v}


def evaluate(data, out_dir, folds=4):
    for fam, df in data.items():
        for mode in ("full", "ablated"):
            X = df.drop(columns=["_y", "_template"])
            if mode == "ablated":
                X = X.drop(columns=[c for c in ABLATE[fam] if c in X.columns])
            y, groups = df["_y"].to_numpy(), df["_template"].to_numpy()
            aucs = []
            for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
                if len(set(y[te])) < 2:
                    continue
                clf = HistGradientBoostingClassifier(random_state=SEED, max_iter=200)
                clf.fit(X.iloc[tr], y[tr])
                aucs.append(roc_auc_score(y[te], clf.predict_proba(X.iloc[te])[:, 1]))
            row = {"family": fam, "features": mode, "n_forged": int((y == 1).sum()),
                   "n_genuine": int((y == 0).sum()), "n_features": X.shape[1],
                   "folds": len(aucs), "auc_mean": round(float(np.mean(aucs)), 4),
                   "auc_min": round(float(np.min(aucs)), 4),
                   "auc_max": round(float(np.max(aucs)), 4),
                   "dropped": "|".join(ABLATE[fam]) if mode == "ablated" else ""}
            with open(os.path.join(out_dir, "auc.csv"), "a") as f:
                if f.tell() == 0:
                    f.write(",".join(row) + "\n")
                f.write(",".join(str(v) for v in row.values()) + "\n")
            print(f"  {fam:3} {mode:8} AUC {row['auc_mean']:.4f} "
                  f"[{row['auc_min']:.4f}, {row['auc_max']:.4f}] over {row['folds']} folds "
                  f"({row['n_forged']} forged vs {row['n_genuine']} genuine)", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "run.env.json"), "w") as f:
        json.dump({"seed": SEED, "max_per_class_per_case": MAX_PER_CLASS_PER_CASE,
                   "balanced_within_case": True,
                   "classifier": "sklearn HistGradientBoostingClassifier(max_iter=200)",
                   "split": "GroupKFold by development template",
                   "python": sys.version.split()[0], "pandas": pd.__version__}, f, indent=2)
    print("building record sets from the pilot's 144 variants ...", flush=True)
    data = build(a.out)
    print("distinguishability AUC, tested on held-out development templates:", flush=True)
    evaluate(data, a.out)
    print("done")


if __name__ == "__main__":
    main()
