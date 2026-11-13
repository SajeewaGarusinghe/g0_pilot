#!/usr/bin/env python3
"""Matched client/server pairs from an RCAEval traces.csv, for G0 stage 2 step 7.

The pairing is the one `audit_traces.py` established and measured in steps 2-3: a cross-service
parent/child edge is a candidate client/server pair, the role tier says how well the guide's two
role checks resolve, and a pair is one-to-one when the client span has exactly one cross-service
child. `audit_traces.py` is deliberately left untouched because its outputs are already published
in 09-results; this module repeats that logic for step 7 and `--check` asserts the two agree.
P3 unifies them when it rewrites the matcher with tests.

Data hygiene is enforced by `audit_traces.guard()`, which this module calls: dev templates and
RE3-TT only, checked before any file is opened.
"""
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_traces import (RE2_OB_ROOT, cases_re2_ob_dev, cases_re3_tt, dev_templates,  # noqa: E402
                          empty, guard, op_tier)

# epsilon is not fitted; this is a ladder of principled values to report the count at.
# 0 = the rule as written; 1 us = the recorded timer resolution (durations are integer us);
# the rest show how far the bound would have to be loosened to admit anything.
EPS_LADDER = [0, 1, 10, 100, 1000, 10000, 100000]

COLS = ["traceID", "spanID", "serviceName", "methodName", "operationName",
        "startTime", "duration", "statusCode", "parentSpanID"]


def cases_for(dataset):
    allowed, _ = dev_templates()
    return cases_re2_ob_dev(allowed) if dataset == "re2-ob-dev" else cases_re3_tt(allowed)


def build_pairs(dataset, case_dir):
    """One row per candidate client/server pair, with both sides' duration, code and role tier."""
    allowed, _ = dev_templates()
    path = guard(dataset, os.path.join(case_dir, "traces.csv"), allowed)
    header = list(pd.read_csv(path, nrows=0).columns)
    df = pd.read_csv(path, usecols=[c for c in COLS if c in header], low_memory=False)
    n = len(df)

    sid = df["spanID"].astype(str)
    svc = df["serviceName"].astype(str)
    meth = (df["methodName"].astype("string").fillna("")
            if "methodName" in df else pd.Series([""] * n, dtype="string"))
    op = (df["operationName"].astype("string").fillna("")
          if "operationName" in df else pd.Series([""] * n, dtype="string"))
    pid = df["parentSpanID"].astype(str)
    sc = df["statusCode"] if "statusCode" in df else pd.Series([None] * n)

    svc_map = dict(zip(sid, svc))
    meth_map, op_map = dict(zip(sid, meth)), dict(zip(sid, op))
    par_map, dur_map = dict(zip(sid, pid)), dict(zip(sid, df["duration"]))
    start_map, sc_map = dict(zip(sid, df["startTime"])), dict(zip(sid, sc))

    p_svc = pid.map(svc_map)
    cross = p_svc.notna() & (p_svc != svc)
    if not cross.any():
        return pd.DataFrame()

    ce = pd.DataFrame({
        "traceID": df.loc[cross, "traceID"],
        "server_span": sid[cross], "client_span": pid[cross],
        "server_service": svc[cross], "client_service": p_svc[cross],
        "server_duration": df.loc[cross, "duration"], "server_start": df.loc[cross, "startTime"],
        "server_code": sc[cross], "call": meth[cross].where(~meth[cross].map(empty), op[cross]),
    })
    ce["client_duration"] = ce.client_span.map(dur_map)
    ce["client_start"] = ce.client_span.map(start_map)
    ce["client_code"] = ce.client_span.map(sc_map)

    # role tier, exactly as step 2 defines it
    c_meth, c_op = ce.client_span.map(meth_map), ce.client_span.map(op_map)
    s_meth, s_op = ce.server_span.map(meth_map), ce.server_span.map(op_map)
    meth_unavail = c_meth.map(empty) | s_meth.map(empty)
    meth_match = (~meth_unavail) & (c_meth == s_meth)
    tier_op = pd.Series([op_tier(a, b) for a, b in zip(c_op, s_op)], index=ce.index)
    gp = ce.client_span.map(par_map)
    gp_svc = gp.map(svc_map)
    caller_root = gp.map(empty)
    caller_same = gp_svc == ce.client_service
    named = tier_op.isin(["exact", "norm_equal"]) & (meth_match | meth_unavail)
    ce["role_tier"] = "NA_ROLE"
    ce.loc[named & caller_same, "role_tier"] = "STRONG"
    ce.loc[named & caller_root, "role_tier"] = "ROOT_CLIENT"
    ce.loc[(tier_op == "verb_only_consistent") & (caller_same | caller_root)
           & ~named, "role_tier"] = "WEAK_VERB_ONLY"

    # one-to-one only: a client span with more than one cross-service child is ambiguous (step 3
    # found none of these, but the guard stays so the rule is never applied to an ambiguous join)
    nkids = ce.groupby("client_span").size()
    dup_ids = set(sid[sid.duplicated()])
    ce = ce[(ce.client_span.map(nkids) == 1) & (~ce.client_span.isin(dup_ids))]
    return ce.reset_index(drop=True)


def check(dataset, published_csv):
    """Assert this module reproduces the pair and role-tier counts audit_traces.py published."""
    pub = pd.read_csv(published_csv)
    pub = pub[pub.dataset == dataset]
    want = {"STRONG": int(pub.role_strong.sum()), "ROOT_CLIENT": int(pub.role_root_client.sum()),
            "WEAK_VERB_ONLY": int(pub.role_weak_verb_only.sum())}
    got = {k: 0 for k in want}
    for cid, cdir, _t in cases_for(dataset):
        p = build_pairs(dataset, cdir)
        for k in got:
            got[k] += int((p.role_tier == k).sum())
        print(f"  {cid}: {len(p)} one-to-one pairs", flush=True)
    print(f"published {want}\nrebuilt   {got}")
    ok = all(got[k] == want[k] for k in want)
    print("MATCH" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    # python stage2/pairs.py --check re2-ob-dev <published role_coverage.csv>
    if len(sys.argv) == 4 and sys.argv[1] == "--check":
        sys.exit(check(sys.argv[2], sys.argv[3]))
    print(__doc__)
