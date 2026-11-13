#!/usr/bin/env python3
"""Aggregate the per-case facts from audit_traces.py into the tables that go into
09-results/g0/ (G0 stage 2, build-guide P0 steps 1-3).

  python stage2/summarize.py --out stage2/out/2026-11-13

Every share is reported pooled (span-weighted) AND as a per-case min/median/max, because
the development set is 12 templates from 2 root-cause services: a pooled share alone lets
one large case speak for the set (the same clustering limit as STATUS.md K14).
"""
import argparse
import os

import pandas as pd

pd.set_option("display.width", 200)


def load(out, name, **kw):
    p = os.path.join(out, name)
    return pd.read_csv(p, **kw) if os.path.exists(p) else pd.DataFrame()


def spread(series):
    """min / median / max of a per-case series, as a compact string."""
    if series.empty:
        return ""
    return f"{series.min():.4f} / {series.median():.4f} / {series.max():.4f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = a.out
    agg = os.path.join(out, "agg")
    os.makedirs(agg, exist_ok=True)

    cases = load(out, "schema_cases.csv")
    part = load(out, "span_partition.csv")
    cov = load(out, "role_coverage.csv")
    reasons = load(out, "role_reasons.csv")
    match = load(out, "matching_counts.csv")
    codes = load(out, "status_codes.csv", keep_default_na=False)
    paired = load(out, "paired_code_census.csv", keep_default_na=False)
    census = load(out, "service_role_census.csv")
    mism = load(out, "op_mismatch_taxonomy.csv")
    nochild = load(out, "client_spans_without_server_child.csv")
    reads = load(out, "read_log.csv")
    smo = load(out, "svc_method_op.csv")

    # ---------------- step 1: schema, per data set ----------------
    rows = []
    for ds, g in cases.groupby("dataset"):
        p = part[part.dataset == ds].pivot_table(index="case_id", columns="class",
                                                 values="n", aggfunc="sum").fillna(0)
        tot = p.sum(axis=1)
        rows.append({
            "dataset": ds, "n_cases": len(g), "spans_total": int(g.n_rows.sum()),
            "spans_per_case_min_med_max": f"{int(g.n_rows.min())} / {int(g.n_rows.median())} / {int(g.n_rows.max())}",
            "traces_total": int(g.n_traces.sum()),
            "services_min_max": f"{int(g.n_services.min())}-{int(g.n_services.max())}",
            "methods_min_max": f"{int(g.n_methods.min())}-{int(g.n_methods.max())}",
            "operations_min_max": f"{int(g.n_operations.min())}-{int(g.n_operations.max())}",
            "duplicate_span_ids": int(g.dup_span_ids.sum()),
            "service_name_with_underscore": "|".join(sorted({x for x in g.svc_name_with_underscore.dropna().astype(str) if x})) or "none",
            "missing_expected_cols": "|".join(sorted({x for x in g.missing_expected_cols.dropna().astype(str) if x})) or "none",
            "extra_cols": "|".join(sorted({x for x in g.extra_cols.dropna().astype(str) if x})) or "none",
            "duration_p50_min_max": f"{int(g.duration_p50.min())}-{int(g.duration_p50.max())}",
            "duration_max": int(g.duration_max.max()),
            "duration_unit_divisor_to_s": "|".join(sorted(g.duration_unit_divisor_to_s.astype(str).unique())),
            "startTime_over_millis_median": "|".join(sorted(g.startTime_over_startTimeMillis_median.astype(str).unique())),
            "statusCode_zero": int(g.sc_zero.sum()), "statusCode_nan": int(g.sc_nan.sum()),
            "statusCode_nonzero": int(g.sc_nonzero.sum()),
            "share_root": round(float(p.get("root", 0).sum() / tot.sum()), 4),
            "share_dangling_parent": round(float(p.get("dangling_parent", pd.Series(0, index=p.index)).sum() / tot.sum()), 4),
            "share_same_svc_parent": round(float(p.get("same_svc_parent", 0).sum() / tot.sum()), 4),
            "share_cross_svc_parent": round(float(p.get("cross_svc_parent", 0).sum() / tot.sum()), 4),
            "cross_share_per_case_min_med_max": spread((p.get("cross_svc_parent", 0) / tot).dropna()),
        })
    schema = pd.DataFrame(rows)
    schema.to_csv(os.path.join(agg, "pooled_schema.csv"), index=False)

    # ---------------- step 2: role coverage ----------------
    if not cov.empty:
        num = ["cross_edges", "meth_match", "meth_unavailable", "op_raw_match", "op_norm_match",
               "op_verb_only", "op_unavailable", "op_mismatch", "caller_same_svc", "caller_root",
               "caller_dangling", "caller_other_svc", "role_strong", "role_root_client",
               "role_weak_verb_only", "role_na"]
        pair = cov.groupby(["dataset", "parent_service", "child_service"])[num].sum().reset_index()
        for c in ["role_strong", "role_root_client", "role_weak_verb_only", "role_na",
                  "op_raw_match", "op_norm_match"]:
            pair[f"share_{c}"] = (pair[c] / pair.cross_edges).round(4)
        percase = cov.assign(share=cov.role_strong / cov.cross_edges).groupby(
            ["dataset", "parent_service", "child_service"])["share"].agg(
            ["min", "median", "max", "count"]).reset_index()
        percase.columns = ["dataset", "parent_service", "child_service",
                           "strong_share_case_min", "strong_share_case_med",
                           "strong_share_case_max", "n_cases_with_pair"]
        pair = pair.merge(percase, on=["dataset", "parent_service", "child_service"])
        pair.to_csv(os.path.join(agg, "pooled_role_coverage_by_pair.csv"), index=False)

        ds_roll = cov.groupby("dataset")[num].sum().reset_index()
        for c in ["role_strong", "role_root_client", "role_weak_verb_only", "role_na",
                  "op_raw_match", "op_norm_match", "meth_unavailable"]:
            ds_roll[f"share_{c}"] = (ds_roll[c] / ds_roll.cross_edges).round(4)
        bycase = cov.groupby(["dataset", "case_id"])[["cross_edges", "role_strong"]].sum()
        bycase["share"] = bycase.role_strong / bycase.cross_edges
        ds_roll["strong_share_per_case_min_med_max"] = ds_roll.dataset.map(
            {ds: spread(g["share"]) for ds, g in bycase.groupby(level=0)})
        ds_roll.to_csv(os.path.join(agg, "pooled_role_coverage_by_dataset.csv"), index=False)

    if not reasons.empty:
        r = reasons.groupby(["dataset", "reason"])["n"].sum().reset_index()
        tot = r[r.reason.isin(["ROLE_STRONG", "ROLE_ROOT_CLIENT", "ROLE_WEAK_VERB_ONLY",
                               "NA_ROLE_CALLER_DANGLING", "NA_ROLE_CALLER_OTHER_SVC",
                               "NA_ROLE_METHOD_DISAGREES", "NA_ROLE_OP_MISMATCH",
                               "NA_ROLE_OP_UNAVAILABLE"])].groupby("dataset")["n"].sum()
        r["share_of_cross_edges"] = (r.n / r.dataset.map(tot)).round(4)
        r.to_csv(os.path.join(agg, "pooled_role_reasons.csv"), index=False)

    if not mism.empty:
        m = mism.groupby(["dataset", "parent_op_form", "child_op_form", "op_tier",
                          "explained_by_normalisation"]).agg(
            n=("n", "sum"), example_parent_op=("example_parent_op", "first"),
            example_child_op=("example_child_op", "first")).reset_index()
        m.to_csv(os.path.join(agg, "pooled_op_mismatch.csv"), index=False)

    # ---------------- step 3: matching ----------------
    if not match.empty:
        m = match.groupby(["dataset", "code"])["n"].sum().reset_index()
        m["share"] = (m.n / m.groupby("dataset")["n"].transform("sum")).round(4)
        m.to_csv(os.path.join(agg, "pooled_matching_by_dataset.csv"), index=False)
        mp = match.groupby(["dataset", "parent_service", "child_service", "code"])["n"].sum().reset_index()
        mp.to_csv(os.path.join(agg, "pooled_matching_by_pair.csv"), index=False)

    if not paired.empty:
        paired["n"] = paired["n"].astype(int)
        p = paired.groupby(["dataset", "role_tier", "client_code", "server_code"])["n"].sum().reset_index()
        p["share"] = (p.n / p.groupby("dataset")["n"].transform("sum")).round(6)
        p.to_csv(os.path.join(agg, "pooled_paired_code_census.csv"), index=False)

    if not codes.empty:
        codes["n"] = codes["n"].astype(int)
        c = codes.groupby(["dataset", "code", "span_class", "is_client_span"])["n"].sum().reset_index()
        c.to_csv(os.path.join(agg, "pooled_status_codes.csv"), index=False)
        nz = c[~c.code.isin(["0", "NaN"])]
        nz.to_csv(os.path.join(agg, "pooled_status_codes_nonzero.csv"), index=False)

    if not census.empty:
        cs = census.groupby(["dataset", "serviceName"])[
            ["n_spans", "as_server_cross_child", "as_client_with_callee", "root",
             "dangling_parent", "same_svc_parent"]].sum().reset_index()
        cs["appears_as_server"] = cs.as_server_cross_child > 0
        cs["appears_as_client"] = cs.as_client_with_callee > 0
        cs.to_csv(os.path.join(agg, "pooled_service_role_census.csv"), index=False)

    if not nochild.empty:
        nc = nochild.groupby(["dataset", "serviceName", "call"])[
            ["n_client_spans", "n_with_cross_child"]].sum().reset_index()
        nc["share_with_server_child"] = (nc.n_with_cross_child / nc.n_client_spans).round(4)
        nc.sort_values(["dataset", "share_with_server_child", "n_client_spans"],
                       ascending=[True, True, False]).to_csv(
            os.path.join(agg, "pooled_client_spans_without_server_child.csv"), index=False)

    if not smo.empty:
        s = smo.groupby(["dataset", "serviceName", "methodName", "operationName"])["n"].sum().reset_index()
        s.to_csv(os.path.join(agg, "pooled_svc_method_op.csv"), index=False)
    cal = load(out, "call_role_census.csv")
    if not cal.empty:
        c = cal.groupby(["dataset", "serviceName", "call"])[
            ["n_spans", "as_server_cross_child", "as_client_with_callee", "neither"]].sum().reset_index()
        c.to_csv(os.path.join(agg, "pooled_call_role_census.csv"), index=False)
        # a call that never appears as a server span anywhere in the data set has no server
        # record at all: that callee emits no spans (R1/R2 can never reach it)
        rows = []
        for (ds, call), g in c.groupby(["dataset", "call"]):
            srv = int(g.as_server_cross_child.sum())
            rows.append({"dataset": ds, "call": call, "spans": int(g.n_spans.sum()),
                         "server_spans_anywhere": srv,
                         "client_spans_with_callee": int(g.as_client_with_callee.sum()),
                         "callee_emits_spans": srv > 0,
                         "services": "|".join(sorted(g.serviceName.unique()))})
        pd.DataFrame(rows).sort_values(["dataset", "callee_emits_spans", "spans"],
                                       ascending=[True, True, False]).to_csv(
            os.path.join(agg, "callee_instrumentation.csv"), index=False)

    if not reads.empty:
        rl = reads.groupby("dataset").agg(files=("path", "count"),
                                          bytes=("bytes", "sum")).reset_index()
        rl["templates_read"] = rl.dataset.map(
            {ds: len({p.split("/")[-3] for p in g.path}) for ds, g in reads.groupby("dataset")})
        rl.to_csv(os.path.join(agg, "read_log_summary.csv"), index=False)

    print("=== step 1 schema (per data set) ===")
    print(schema.T.to_string())
    if not cov.empty:
        print("\n=== step 2 role coverage (per data set) ===")
        print(pd.read_csv(os.path.join(agg, "pooled_role_coverage_by_dataset.csv")).T.to_string())
        print("\n=== step 2 reasons ===")
        print(pd.read_csv(os.path.join(agg, "pooled_role_reasons.csv")).to_string(index=False))
    if not match.empty:
        print("\n=== step 3 matching ===")
        print(pd.read_csv(os.path.join(agg, "pooled_matching_by_dataset.csv")).to_string(index=False))
    if not paired.empty:
        print("\n=== step 3 paired status codes (matched pairs) ===")
        print(pd.read_csv(os.path.join(agg, "pooled_paired_code_census.csv")).to_string(index=False))
    print(f"\naggregates -> {agg}")


if __name__ == "__main__":
    main()
