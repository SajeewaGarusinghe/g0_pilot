#!/usr/bin/env python3
"""G0 stage 2, build-guide P0 step 7: prototype R1 and R2 on every clean matched pair.

  python stage2/rules_prototype.py --dataset re2-ob-dev --out stage2/out/step7-<date>
  python stage2/rules_prototype.py --dataset re3-tt     --out stage2/out/step7-<date>

R1 (proposal section 08) compares DURATIONS, not timestamps: a server span cannot last longer
than its caller's matched client span, + epsilon for timer resolution. Comparing durations is
what makes the rule immune to clock offsets between hosts, so a cross-host offset can never
manufacture a contradiction. Interval containment is computed too, but only as a *diagnostic*
for how much clock offset exists (input to the AF1 honest-fault family); it is not R1.

R2 compares the protocol status code the server reports against the code its caller recorded.

Epsilon is NOT fitted here. The proposal rejects calibrating to a false-alarm rate, so this
reports the violation count at a ladder of principled values (0 us, timer resolution, and
upward) and the full excess distribution, and leaves the choice to the written argument.

Clean development data cannot make a rule valid: it can only fail to refute it. Every
violation found here is to be explained by a written exclusion or its route removed.
"""
import argparse
import gc
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from pairs import build_pairs, cases_for, EPS_LADDER  # noqa: E402

SINKS = {}


def emit(out_dir, name, row):
    path = os.path.join(out_dir, name)
    new = name not in SINKS
    if new:
        SINKS[name] = open(path, "a", newline="")
    f = SINKS[name]
    if new and f.tell() == 0:
        f.write(",".join(str(k) for k in row) + "\n")
    vals = []
    for v in row.values():
        s = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
        if any(c in s for c in ',"\n'):
            s = '"' + s.replace('"', '""') + '"'
        vals.append(s)
    f.write(",".join(vals) + "\n")
    f.flush()


def q(series, p):
    return int(series.quantile(p)) if len(series) else ""


def run_case(dataset, case_id, case_dir, out_dir, max_detail):
    pairs = build_pairs(dataset, case_dir)
    if pairs is None or pairs.empty:
        return f"{case_id}: no pairs"

    # R1: excess = server duration - client duration. Positive means the server claims to have
    # taken longer than its caller waited, which is what R1 calls a contradiction (beyond eps).
    pairs["excess_us"] = pairs["server_duration"] - pairs["client_duration"]
    pairs["slack_us"] = -pairs["excess_us"]
    pairs["ratio"] = pairs["client_duration"] / pairs["server_duration"].clip(lower=1)

    for (route_from, route_to, tier), g in pairs.groupby(["client_service", "server_service", "role_tier"]):
        row = {"dataset": dataset, "case_id": case_id, "client_service": route_from,
               "server_service": route_to, "role_tier": tier, "n_pairs": len(g)}
        for e in EPS_LADDER:
            row[f"r1_violations_eps_{e}us"] = int((g.excess_us > e).sum())
        row.update({
            "excess_max_us": int(g.excess_us.max()), "excess_p999_us": q(g.excess_us, 0.999),
            "slack_min_us": int(g.slack_us.min()), "slack_p1_us": q(g.slack_us, 0.01),
            "slack_p10_us": q(g.slack_us, 0.10), "slack_p50_us": q(g.slack_us, 0.50),
            "slack_p90_us": q(g.slack_us, 0.90),
            # diagnostic only, NOT R1: does the client interval contain the server interval?
            # How much room R1 leaves a forger: the caller's duration divided by the server's.
            # An inflated server span stays *compatible* while it is under the caller's wait, so
            # this ratio is the multiplier an F3/FS overlay can use without being contradicted.
            # The pilot's F3 used 5x. (RQ3 input; step 8 reports it by target shape.)
            "ratio_p10": round(float(g.ratio.quantile(0.10)), 2),
            "ratio_p50": round(float(g.ratio.quantile(0.50)), 2),
            "ratio_p90": round(float(g.ratio.quantile(0.90)), 2),
            "pairs_ratio_ge_2": int((g.ratio >= 2).sum()),
            "pairs_ratio_ge_5": int((g.ratio >= 5).sum()),
            "pairs_ratio_ge_10": int((g.ratio >= 10).sum()),
            "server_starts_before_client": int((g.server_start < g.client_start).sum()),
            "server_ends_after_client": int(
                ((g.server_start + g.server_duration) > (g.client_start + g.client_duration)).sum()),
        })
        emit(out_dir, "r1_by_route.csv", row)

    viol = pairs[pairs.excess_us > 0]
    for _, r in viol.head(max_detail).iterrows():
        emit(out_dir, "r1_violations.csv", {
            "dataset": dataset, "case_id": case_id, "traceID": r.traceID,
            "client_service": r.client_service, "server_service": r.server_service,
            "call": r.call, "role_tier": r.role_tier,
            "client_duration_us": int(r.client_duration), "server_duration_us": int(r.server_duration),
            "excess_us": int(r.excess_us), "client_code": r.client_code, "server_code": r.server_code,
            "client_span": r.client_span, "server_span": r.server_span})

    # R2: compare the two protocol codes where both sides carry one.
    both = pairs[pairs.client_code.notna() & pairs.server_code.notna()]
    for (route_from, route_to, tier), g in pairs.groupby(["client_service", "server_service", "role_tier"]):
        gb = g[g.client_code.notna() & g.server_code.notna()]
        emit(out_dir, "r2_by_route.csv", {
            "dataset": dataset, "case_id": case_id, "client_service": route_from,
            "server_service": route_to, "role_tier": tier, "n_pairs": len(g),
            "n_both_codes_present": len(gb),
            "n_codes_equal": int((gb.client_code == gb.server_code).sum()),
            "n_codes_differ": int((gb.client_code != gb.server_code).sum()),
            "n_either_code_non_ok": int(((gb.client_code != 0) | (gb.server_code != 0)).sum())})

    dis = both[both.client_code != both.server_code]
    for _, r in dis.head(max_detail).iterrows():
        emit(out_dir, "r2_disagreements.csv", {
            "dataset": dataset, "case_id": case_id, "traceID": r.traceID,
            "client_service": r.client_service, "server_service": r.server_service,
            "call": r.call, "role_tier": r.role_tier,
            "client_code": int(r.client_code), "server_code": int(r.server_code),
            "client_duration_us": int(r.client_duration), "server_duration_us": int(r.server_duration),
            "slack_us": int(r.slack_us), "client_span": r.client_span, "server_span": r.server_span})

    tiers = pairs.role_tier.value_counts().to_dict()
    emit(out_dir, "case_totals.csv", {
        "dataset": dataset, "case_id": case_id, "n_pairs": len(pairs),
        "tier_STRONG": int(tiers.get("STRONG", 0)),
        "tier_ROOT_CLIENT": int(tiers.get("ROOT_CLIENT", 0)),
        "tier_WEAK_VERB_ONLY": int(tiers.get("WEAK_VERB_ONLY", 0)),
        "tier_NA_ROLE": int(tiers.get("NA_ROLE", 0)),
        "r1_violations_eps0": int(len(viol)),
        "r1_violations_detailed": int(min(len(viol), max_detail)),
        "n_both_codes_present": len(both),
        "r2_disagreements": int(len(dis)),
        "r2_disagreements_detailed": int(min(len(dis), max_detail)),
        "slack_min_us": int(pairs.slack_us.min()), "slack_p50_us": q(pairs.slack_us, 0.5)})

    s = (f"{case_id}: {len(pairs)} pairs, R1 violations(eps=0) {len(viol)}, "
         f"min slack {int(pairs.slack_us.min())}us, R2 disagreements {len(dis)}")
    del pairs, both, viol, dis
    gc.collect()
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, choices=["re2-ob-dev", "re3-tt"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--max-detail", type=int, default=500,
                    help="rows of per-violation detail kept per case (totals are always exact)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    cases = cases_for(a.dataset)
    if a.only:
        cases = [c for c in cases if c[0] == a.only or c[1].endswith(a.only)]
    if a.limit:
        cases = cases[:a.limit]

    env = {"python": sys.version.split()[0], "pandas": pd.__version__}
    with open(os.path.join(a.out, f"run_{a.dataset}.env.json"), "w") as f:
        json.dump({"dataset": a.dataset, "n_cases": len(cases), "env": env,
                   "eps_ladder_us": EPS_LADDER, "max_detail_per_case": a.max_detail}, f, indent=2)
    print(f"step 7 | {a.dataset}: {len(cases)} cases -> {a.out}", flush=True)
    for i, (cid, cdir, _t) in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {run_case(a.dataset, cid, cdir, a.out, a.max_detail)}", flush=True)
    for f in SINKS.values():
        f.close()
    print("done")


if __name__ == "__main__":
    main()
