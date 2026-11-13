#!/usr/bin/env python3
"""G0 stage 2, build-guide P0 steps 1-3: trace schema audit, role inference, attempt matching.

One pass per case over traces.csv; three groups of per-case facts written as long-format
CSVs. stage2/summarize.py turns those into the pooled and per-case tables that go into
09-results/g0/ as 01-schema-audit.md, 02-role-inference.md and 03-attempt-matching.md.

  python stage2/audit_traces.py --dataset re2-ob-dev --out stage2/out/2026-11-13
  python stage2/audit_traces.py --dataset re3-tt     --out stage2/out/2026-11-13

Data hygiene (AGENTS.md section 6; build-guide test P0-T1). Only the 36 RE2-OB development
cases named in dev_cases.json and RE3-TT may be read. Every candidate path is checked against
the dev template list before it is opened, the run aborts on anything else, and read_log.csv
records the path, size and SHA-256 of every file actually opened.

This is a prototype: P3 rewrites the matcher with tests (build guide P0, "Do not").

Reason-code mapping used here (A3's AMB_DUPLICATE covers both a duplicate span id and
"more than one candidate partner"; this script splits the second case so retries/fan-out are
visible separately, and says so in the evidence files):
  AMB_DUPLICATE    duplicate spanID in the file
  AMB_REUSED_CTX   one client span, several cross-service children, all the same methodName
  AMB_NON_1TO1     one client span, several cross-service children, different methodNames
  NA_ROLE          1:1 candidate whose role checks fail
  MATCHED_1TO1     1:1 candidate whose role checks pass
"""
import argparse
import gc
import hashlib
import json
import os
import sys
from collections import Counter

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from g0_pilot import time_unit_divisor  # noqa: E402  (reused, build guide: reuse before rewrite)

RE2_OB_ROOT = os.path.join(REPO, "data", "RE2", "RE2-OB")
RE3_TT_ROOT = os.path.join(REPO, "data", "RE3", "RE3-TT")
DEV_CASES = os.path.join(REPO, "dev_cases.json")
ANSWERS = os.path.join(REPO, "agent_tasks", "re2-ob.answers.jsonl")
TRACE_COLS = ["time", "traceID", "spanID", "serviceName", "methodName", "operationName",
              "startTimeMillis", "startTime", "duration", "statusCode", "parentSpanID"]
DERIVED = ["tracets_err.csv", "tracets_lat.csv"]

SINKS = {}          # name -> open file handle, header written on first row


# ---------------------------------------------------------------- output plumbing
def emit(out_dir, name, row):
    """Append one dict as a CSV row; the first row of each file writes the header."""
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


def sha256(path, blocks=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(blocks)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- hygiene guard
def dev_templates():
    with open(DEV_CASES) as f:
        dev = json.load(f)
    return {f"{s}_{fault}" for s, fault in dev["templates"]}, dev


def guard(dataset, path, allowed_templates):
    """Abort before opening anything that is not dev data. Build-guide test P0-T1."""
    real = os.path.realpath(path)
    forbidden = ("RE2-TT", "RE3-OB", "RE3-SS", "RE2-SS", "RE1-")
    for bad in forbidden:
        if bad in real:
            sys.exit(f"REFUSED (data hygiene): {real} belongs to a sealed or unused data set")
    if dataset == "re2-ob-dev":
        if not real.startswith(os.path.realpath(RE2_OB_ROOT) + os.sep):
            sys.exit(f"REFUSED (data hygiene): {real} is outside {RE2_OB_ROOT}")
        template = real[len(os.path.realpath(RE2_OB_ROOT)) + 1:].split(os.sep)[0]
        if template not in allowed_templates:
            sys.exit(f"REFUSED (data hygiene): template {template!r} is not one of the 12 "
                     f"development templates in dev_cases.json (it is test stratum S1)")
    elif dataset == "re3-tt":
        if not real.startswith(os.path.realpath(RE3_TT_ROOT) + os.sep):
            sys.exit(f"REFUSED (data hygiene): {real} is outside {RE3_TT_ROOT}")
    else:
        sys.exit(f"unknown dataset {dataset}")
    return real


# ---------------------------------------------------------------- case enumeration
def cases_re2_ob_dev(allowed):
    _, dev = dev_templates()
    src = {}
    with open(ANSWERS) as f:
        for line in f:
            a = json.loads(line)
            src[a["case_id"]] = a
    out = []
    for cid in dev["cases"]:
        a = src[cid]
        template = f"{a['service']}_{a['fault']}"
        if template not in allowed:
            sys.exit(f"REFUSED: {cid} -> {template} is not a development template")
        out.append((cid, os.path.join(RE2_OB_ROOT, a["source"]), template))
    if len(out) != 36:
        sys.exit(f"expected 36 development cases, got {len(out)}")
    return out


def cases_re3_tt(_allowed):
    if not os.path.isdir(RE3_TT_ROOT):
        sys.exit(f"{RE3_TT_ROOT} not found - download it first (see stage2/README.md)")
    out = []
    for template in sorted(os.listdir(RE3_TT_ROOT)):
        tdir = os.path.join(RE3_TT_ROOT, template)
        if not os.path.isdir(tdir):
            continue
        for rep in sorted(os.listdir(tdir)):
            cdir = os.path.join(tdir, rep)
            if os.path.isdir(cdir) and os.path.exists(os.path.join(cdir, "traces.csv")):
                out.append((f"{template}/{rep}", cdir, template))
    return out


# ---------------------------------------------------------------- helpers
def op_form(op):
    if not isinstance(op, str) or not op:
        return "missing"
    if op.startswith("/"):
        return "slash_prefix"
    if op.startswith("grpc."):
        return "grpc_prefix"
    return "bare"


def op_norm(s):
    """Normalisation derived from the data: on Online Boutique the callee-side operation name
    carries a leading '/' or a 'grpc.' prefix depending on the instrumentation language."""
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if s.startswith("/"):
        s = s[1:]
    if s.startswith("grpc."):
        s = s[5:]
    return s


HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def empty(x):
    return (not isinstance(x, str)) or x.strip() == "" or x.strip().lower() == "nan"


def op_tier(parent_op, child_op):
    """How strongly a parent/child operation pair identifies the same call.

    exact / norm_equal     the two name the same operation (Online Boutique gRPC)
    verb_only_consistent   the client span carries only the HTTP verb and the server span's
                           route starts with it (Train Ticket): the pair is consistent but
                           does not identify the route, so it cannot carry a route allow-list
    mismatch / unavailable
    """
    if empty(parent_op) or empty(child_op):
        return "unavailable"
    if parent_op == child_op:
        return "exact"
    if op_norm(parent_op) == op_norm(child_op):
        return "norm_equal"
    p = parent_op.strip()
    if p in HTTP_VERBS and child_op.strip().startswith(p + " "):
        return "verb_only_consistent"
    return "mismatch"


# ---------------------------------------------------------------- one case
def audit_case(dataset, case_id, case_dir, template, out_dir, allowed):
    tpath = guard(dataset, os.path.join(case_dir, "traces.csv"), allowed)
    size = os.path.getsize(tpath)
    emit(out_dir, "read_log.csv", {"dataset": dataset, "case_id": case_id, "file": "traces.csv",
                                   "path": tpath, "bytes": size, "sha256": sha256(tpath)})

    header = list(pd.read_csv(tpath, nrows=0).columns)
    cols = [c for c in TRACE_COLS if c in header]
    missing = [c for c in TRACE_COLS if c not in header]
    extra = [c for c in header if c not in TRACE_COLS]
    df = pd.read_csv(tpath, usecols=cols, low_memory=False)
    n = len(df)

    for c in header:
        emit(out_dir, "schema_columns.csv", {
            "dataset": dataset, "case_id": case_id, "column": c,
            "in_expected_schema": c in TRACE_COLS,
            "dtype": str(df[c].dtype) if c in df.columns else "not_read",
            "n_null": int(df[c].isna().sum()) if c in df.columns else ""})

    for f in DERIVED:
        p = os.path.join(case_dir, f)
        if os.path.exists(p):
            guard(dataset, p, allowed)
            with open(p) as fh:
                head = fh.readline().strip()
            emit(out_dir, "derived_file_headers.csv",
                 {"dataset": dataset, "case_id": case_id, "file": f, "header": head})

    sid = df["spanID"].astype(str)
    svc = df["serviceName"].astype(str)
    meth = df["methodName"].astype(str) if "methodName" in df else pd.Series([""] * n)
    op = df["operationName"].astype(str) if "operationName" in df else pd.Series([""] * n)
    pid = df["parentSpanID"].astype(str) if "parentSpanID" in df else pd.Series(["nan"] * n)

    svc_map = dict(zip(sid, svc))
    meth_map = dict(zip(sid, meth))
    op_map = dict(zip(sid, op))
    par_map = dict(zip(sid, pid))
    dup_ids = set(sid[sid.duplicated()])

    has_pid = pid.notna() & (pid != "nan") & (pid != "")
    p_svc = pid.map(svc_map)
    root = ~has_pid
    dangling = has_pid & p_svc.isna()
    same_svc = p_svc.notna() & (p_svc == svc)
    cross = p_svc.notna() & (p_svc != svc)

    # --- step 1: schema ------------------------------------------------------
    sc = df["statusCode"] if "statusCode" in df else pd.Series([None] * n)
    dur = pd.to_numeric(df["duration"], errors="coerce") if "duration" in df else pd.Series([float("nan")] * n)
    start_ratio = ""
    if "startTime" in df and "startTimeMillis" in df:
        a = pd.to_numeric(df["startTime"], errors="coerce")
        b = pd.to_numeric(df["startTimeMillis"], errors="coerce")
        start_ratio = round(float((a / b.replace(0, pd.NA)).median()), 4)
    cross_parents = set(pid[cross])
    is_client = sid.isin(cross_parents)

    emit(out_dir, "schema_cases.csv", {
        "dataset": dataset, "case_id": case_id, "template": template,
        "bytes": size, "n_rows": n,
        "n_traces": int(df["traceID"].nunique()) if "traceID" in df else "",
        "n_spans_distinct": int(sid.nunique()), "dup_span_ids": len(dup_ids),
        "n_services": int(svc.nunique()), "n_methods": int(meth.nunique()),
        "n_operations": int(op.nunique()),
        "missing_expected_cols": "|".join(missing), "extra_cols": "|".join(extra),
        "svc_name_with_underscore": "|".join(sorted({s for s in svc.unique() if "_" in s})),
        "duration_min": int(dur.min()) if dur.notna().any() else "",
        "duration_p50": int(dur.median()) if dur.notna().any() else "",
        "duration_p95": int(dur.quantile(0.95)) if dur.notna().any() else "",
        "duration_max": int(dur.max()) if dur.notna().any() else "",
        "duration_unit_divisor_to_s": time_unit_divisor(dur),
        "startTime_over_startTimeMillis_median": start_ratio,
        "sc_zero": int((sc == 0).sum()), "sc_nan": int(sc.isna().sum()),
        "sc_nonzero": int((sc.fillna(0) != 0).sum()),
        "root_spans": int(root.sum()), "dangling_parent": int(dangling.sum()),
        "same_svc_parent": int(same_svc.sum()), "cross_svc_parent": int(cross.sum()),
        "client_spans_with_matched_callee": int(is_client.sum())})

    cls = pd.Series("root", index=df.index)
    cls[dangling] = "dangling_parent"
    cls[same_svc] = "same_svc_parent"
    cls[cross] = "cross_svc_parent"
    code_lbl = sc.astype("string").fillna("NaN").str.replace(r"\.0$", "", regex=True)
    tmp = pd.DataFrame({"code": code_lbl, "class": cls, "is_client": is_client})
    for (code, c, isc), cnt in tmp.groupby(["code", "class", "is_client"]).size().items():
        emit(out_dir, "status_codes.csv", {"dataset": dataset, "case_id": case_id, "code": code,
                                           "span_class": c, "is_client_span": isc, "n": int(cnt)})

    for (s, m, o), cnt in pd.DataFrame({"s": svc, "m": meth, "o": op}).groupby(["s", "m", "o"]).size().items():
        emit(out_dir, "svc_method_op.csv", {"dataset": dataset, "case_id": case_id,
                                            "serviceName": s, "methodName": m,
                                            "operationName": o, "n": int(cnt)})

    forms = op.map(op_form)
    for (s, fm), cnt in pd.DataFrame({"s": svc, "f": forms}).groupby(["s", "f"]).size().items():
        emit(out_dir, "operation_forms.csv", {"dataset": dataset, "case_id": case_id,
                                              "serviceName": s, "op_form": fm, "n": int(cnt)})

    census = pd.DataFrame({"svc": svc, "cls": cls, "is_client": is_client})
    for s, g in census.groupby("svc"):
        emit(out_dir, "service_role_census.csv", {
            "dataset": dataset, "case_id": case_id, "serviceName": s, "n_spans": len(g),
            "as_server_cross_child": int((g.cls == "cross_svc_parent").sum()),
            "as_client_with_callee": int(g.is_client.sum()),
            "root": int((g.cls == "root").sum()),
            "dangling_parent": int((g.cls == "dangling_parent").sum()),
            "same_svc_parent": int((g.cls == "same_svc_parent").sum())})

    for c, cnt in cls.value_counts().items():
        emit(out_dir, "span_partition.csv", {"dataset": dataset, "case_id": case_id,
                                             "class": c, "n": int(cnt)})

    # --- step 2: role inference ---------------------------------------------
    ce = pd.DataFrame({
        "child_svc": svc[cross], "parent_svc": p_svc[cross],
        "child_meth": meth[cross], "parent_meth": pid[cross].map(meth_map),
        "child_op": op[cross], "parent_op": pid[cross].map(op_map),
        "parent_id": pid[cross], "child_sc": sc[cross],
        "parent_sc": pid[cross].map(dict(zip(sid, sc))),
    })
    gp = ce["parent_id"].map(par_map)
    ce["gp_svc"] = gp.map(svc_map)
    # Guide check (b): the candidate client span should itself have a same-service parent (the
    # caller's own server span). Four outcomes, kept apart because they mean different things:
    #   caller_same      check (b) holds
    #   caller_root      the client span is a trace root, so (b) cannot be evaluated either way
    #   caller_dangling  its parent id is absent from the file (sampling)
    #   caller_other     its parent is in a third service - the only genuinely suspicious case
    gp_empty = gp.map(empty)
    ce["caller_root"] = gp_empty
    ce["caller_dangling"] = (~gp_empty) & ce["gp_svc"].isna()
    ce["caller_same_svc"] = ce["gp_svc"] == ce["parent_svc"]
    ce["caller_other_svc"] = ce["gp_svc"].notna() & (ce["gp_svc"] != ce["parent_svc"])
    ce["caller_has_same_svc_parent"] = ce["caller_same_svc"]
    ce["meth_unavailable"] = ce["parent_meth"].map(empty) | ce["child_meth"].map(empty)
    ce["meth_match"] = (~ce["meth_unavailable"]) & (ce["parent_meth"] == ce["child_meth"])
    ce["op_raw_match"] = ce["parent_op"] == ce["child_op"]
    ce["op_tier"] = [op_tier(a, b) for a, b in zip(ce["parent_op"], ce["child_op"])]
    ce["op_norm_match"] = ce["op_tier"].isin(["exact", "norm_equal"])
    # STRONG: the pair identifies one operation on both sides (method + operation agree).
    # WEAK_VERB_ONLY: roles are inferable and the pair is consistent, but the client span
    # names only the HTTP verb, so the route is not identified (Train Ticket).
    ce["role_tier"] = "NA_ROLE"
    named = ce["op_norm_match"] & (ce["meth_match"] | ce["meth_unavailable"])
    strong = named & ce["caller_same_svc"]
    rootc = named & ce["caller_root"]
    weak = (ce["op_tier"] == "verb_only_consistent") & \
        (ce["caller_same_svc"] | ce["caller_root"]) & (~strong) & (~rootc)
    ce.loc[strong, "role_tier"] = "STRONG"
    ce.loc[rootc, "role_tier"] = "ROOT_CLIENT"
    ce.loc[weak, "role_tier"] = "WEAK_VERB_ONLY"
    ce["role_ok"] = ce["role_tier"] == "STRONG"

    for (ps, cs), g in ce.groupby(["parent_svc", "child_svc"]):
        emit(out_dir, "role_coverage.csv", {
            "dataset": dataset, "case_id": case_id, "parent_service": ps, "child_service": cs,
            "cross_edges": len(g),
            "meth_match": int(g.meth_match.sum()),
            "meth_unavailable": int(g.meth_unavailable.sum()),
            "op_raw_match": int(g.op_raw_match.sum()), "op_norm_match": int(g.op_norm_match.sum()),
            "op_verb_only": int((g.op_tier == "verb_only_consistent").sum()),
            "op_unavailable": int((g.op_tier == "unavailable").sum()),
            "op_mismatch": int((g.op_tier == "mismatch").sum()),
            "caller_same_svc": int(g.caller_same_svc.sum()),
            "caller_root": int(g.caller_root.sum()),
            "caller_dangling": int(g.caller_dangling.sum()),
            "caller_other_svc": int(g.caller_other_svc.sum()),
            "role_strong": int((g.role_tier == "STRONG").sum()),
            "role_root_client": int((g.role_tier == "ROOT_CLIENT").sum()),
            "role_weak_verb_only": int((g.role_tier == "WEAK_VERB_ONLY").sum()),
            "role_na": int((g.role_tier == "NA_ROLE").sum())})

    reasons = Counter()
    reasons["ROLE_STRONG"] = int((ce.role_tier == "STRONG").sum())
    reasons["ROLE_ROOT_CLIENT"] = int((ce.role_tier == "ROOT_CLIENT").sum())
    reasons["ROLE_WEAK_VERB_ONLY"] = int((ce.role_tier == "WEAK_VERB_ONLY").sum())
    na = ce[ce.role_tier == "NA_ROLE"]
    reasons["NA_ROLE_CALLER_DANGLING"] = int(na.caller_dangling.sum())
    reasons["NA_ROLE_CALLER_OTHER_SVC"] = int(na.caller_other_svc.sum())
    rest = na[na.caller_same_svc | na.caller_root]
    reasons["NA_ROLE_METHOD_DISAGREES"] = int(((~rest.meth_unavailable) & ~rest.meth_match).sum())
    reasons["NA_ROLE_OP_MISMATCH"] = int((rest.op_tier == "mismatch").sum())
    reasons["NA_ROLE_OP_UNAVAILABLE"] = int((rest.op_tier == "unavailable").sum())
    reasons["METHOD_COLUMN_UNAVAILABLE"] = int(ce.meth_unavailable.sum())
    for r, cnt in reasons.items():
        emit(out_dir, "role_reasons.csv", {"dataset": dataset, "case_id": case_id,
                                           "reason": r, "n": int(cnt)})

    mm = ce[~ce.op_raw_match].copy()
    if len(mm):
        mm["p_form"] = mm["parent_op"].map(op_form)
        mm["c_form"] = mm["child_op"].map(op_form)
        for (pf, cf, tier), g in mm.groupby(["p_form", "c_form", "op_tier"]):
            emit(out_dir, "op_mismatch_taxonomy.csv", {
                "dataset": dataset, "case_id": case_id, "parent_op_form": pf,
                "child_op_form": cf, "op_tier": tier,
                "explained_by_normalisation": tier in ("exact", "norm_equal"), "n": len(g),
                "example_parent_op": g["parent_op"].iloc[0],
                "example_child_op": g["child_op"].iloc[0]})

    # --- step 3: attempt matching ------------------------------------------
    nkids = ce.groupby("parent_id").size()
    nmeth = ce.groupby("parent_id")["child_meth"].nunique()
    ce["n_cross_children"] = ce["parent_id"].map(nkids)
    ce["n_child_methods"] = ce["parent_id"].map(nmeth)
    ce["dup_parent"] = ce["parent_id"].isin(dup_ids)

    def code_of(r):
        if r.dup_parent:
            return "AMB_DUPLICATE"
        if r.n_cross_children > 1:
            return "AMB_REUSED_CTX" if r.n_child_methods == 1 else "AMB_NON_1TO1"
        if r.role_tier == "STRONG":
            return "MATCHED_1TO1"
        if r.role_tier == "ROOT_CLIENT":
            return "MATCHED_1TO1_ROOT_CLIENT"
        return "MATCHED_1TO1_WEAK_ROLE" if r.role_tier == "WEAK_VERB_ONLY" else "NA_ROLE"

    ce["match_code"] = ce.apply(code_of, axis=1)
    for (ps, cs, code), cnt in ce.groupby(["parent_svc", "child_svc", "match_code"]).size().items():
        emit(out_dir, "matching_counts.csv", {"dataset": dataset, "case_id": case_id,
                                              "parent_service": ps, "child_service": cs,
                                              "code": code, "n": int(cnt)})
    has_cross_child = sid.isin(cross_parents)
    # the two gaps, kept apart and in the right direction
    emit(out_dir, "witness_gaps.csv", {
        "dataset": dataset, "case_id": case_id,
        "server_candidates_cross": int(cross.sum()),
        "no_witness_dangling_parent": int(dangling.sum()),
        "role_undecidable_same_svc_parent": int(same_svc.sum()),
        "root_spans": int(root.sum())})

    # Callee not instrumented: a candidate CLIENT span with no cross-service child. A client
    # span is one that is not itself a cross-service child (not a server span) and carries an
    # RPC name - a methodName where that column exists, or an HTTP verb as its operation.
    client_cand = cls.isin(["same_svc_parent", "root"]) & (
        (~meth.map(empty)) | op.isin(HTTP_VERBS))
    out_cand = pd.DataFrame({"svc": svc[client_cand], "meth": meth[client_cand],
                             "op": op[client_cand], "cross": has_cross_child[client_cand]})
    out_cand["call"] = out_cand["meth"].where(~out_cand["meth"].map(empty), out_cand["op"])
    for (s, m), g in out_cand.groupby(["svc", "call"]):
        if int(g.cross.sum()) == len(g):
            continue
        emit(out_dir, "client_spans_without_server_child.csv", {
            "dataset": dataset, "case_id": case_id, "serviceName": s, "call": m,
            "n_client_spans": len(g), "n_with_cross_child": int(g.cross.sum())})

    # Role-aware call census: for each (service, call) say how often it appears as a server
    # span (a cross-service child) and how often as a client span with a matched callee.
    # A call that never appears as a server span anywhere means that callee emits no spans.
    call_lbl = meth.where(~meth.map(empty), op)
    cal = pd.DataFrame({"svc": svc, "call": call_lbl, "cls": cls,
                        "has_cross_child": has_cross_child})
    for (sv, cl), g in cal.groupby(["svc", "call"]):
        emit(out_dir, "call_role_census.csv", {
            "dataset": dataset, "case_id": case_id, "serviceName": sv, "call": cl,
            "n_spans": len(g),
            "as_server_cross_child": int((g.cls == "cross_svc_parent").sum()),
            "as_client_with_callee": int(g.has_cross_child.sum()),
            "neither": int(((g.cls != "cross_svc_parent") & ~g.has_cross_child).sum())})

    pc = ce.copy()
    pc["client_code"] = pc["parent_sc"].astype("string").fillna("NaN").str.replace(r"\.0$", "", regex=True)
    pc["server_code"] = pc["child_sc"].astype("string").fillna("NaN").str.replace(r"\.0$", "", regex=True)
    for (ok, cc, sc_), cnt in pc.groupby(["role_tier", "client_code", "server_code"]).size().items():
        emit(out_dir, "paired_code_census.csv", {
            "dataset": dataset, "case_id": case_id, "role_tier": ok,
            "client_code": cc, "server_code": sc_, "n": int(cnt)})

    summary = (f"{case_id}: {n} rows, cross {int(cross.sum())}, "
               f"root_client {int((ce.role_tier == 'ROOT_CLIENT').sum())}, "
               f"role strong {int((ce.role_tier == 'STRONG').sum())}, "
               f"weak {int((ce.role_tier == 'WEAK_VERB_ONLY').sum())}, "
               f"matched1to1 {int((ce.match_code == 'MATCHED_1TO1').sum())}, "
               f"sc_nonzero {int((sc.fillna(0) != 0).sum())}")
    del df, ce, pc, tmp, census, out_cand, svc_map, meth_map, op_map, par_map
    gc.collect()
    return summary


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, choices=["re2-ob-dev", "re3-tt"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="audit only the first N cases")
    ap.add_argument("--only", default="", help="audit only this case id / template-rep")
    ap.add_argument("--force-path", default="", help="hygiene-guard test: try to audit this path")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    allowed, _ = dev_templates()

    if a.force_path:
        guard(a.dataset, os.path.join(a.force_path, "traces.csv"), allowed)
        sys.exit("guard did not refuse - this is a bug")

    cases = cases_re2_ob_dev(allowed) if a.dataset == "re2-ob-dev" else cases_re3_tt(allowed)
    if a.only:
        cases = [c for c in cases if c[0] == a.only or c[1].endswith(a.only)]
    if a.limit:
        cases = cases[:a.limit]

    env = {"python": sys.version.split()[0], "pandas": pd.__version__}
    try:
        import RCAEval
        env["RCAEval"] = getattr(RCAEval, "__version__", "1.12.0")
    except Exception:
        pass
    print(f"dataset {a.dataset}: {len(cases)} cases -> {a.out}   env {env}", flush=True)
    with open(os.path.join(a.out, f"run_{a.dataset}.env.json"), "w") as f:
        json.dump({"dataset": a.dataset, "n_cases": len(cases), "env": env,
                   "allowed_templates": sorted(allowed)}, f, indent=2)

    for i, (cid, cdir, template) in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {audit_case(a.dataset, cid, cdir, template, a.out, allowed)}",
              flush=True)
    for f in SINKS.values():
        f.close()
    print("done")


if __name__ == "__main__":
    main()
