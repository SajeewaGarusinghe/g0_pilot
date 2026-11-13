#!/usr/bin/env python3
"""G0 stage 2, build-guide P0 step 8: eligible targets, witness coverage, rule-coverage ceiling.

  python stage2/target_coverage.py --out stage2/out/step8-<date>

For each development case this evaluates every candidate service against the **full four-check
target rule** of proposal v9 section 12 (the stage 1 pilot used a simplified rule - known issue
K13), reports which shape each eligible target has, and measures what the rules could remove.

Target rule (v9 section 12). A target is a non-root-cause service in the task's candidate list,
within two call-graph hops of the root cause - either a **callee** of it, or a **sibling** that
shares a caller with it - that:
  (a) shows no BARO-level alarm on its own metrics in the clean case,
  (b) shows no rise in its server-span error rate,
  (c) is not an upstream caller of the root cause (callers inherit symptoms),
  (d) has server spans and log lines for an overlay to imitate.

Check (a) uses BARO's own RobustScorer, as RCAEval implements it (RCAEval/e2e/baro.py): fit a
RobustScaler on the pre-inject window and take the maximum absolute z-score on the post-inject
window, per metric column, maximised over the service's own columns. BARO ranks rather than
thresholds, so the alarm cut is NOT fitted here: the score is reported per service and the
eligible-target counts are given at a ladder of cuts.

Ceilings. Under T2 the rules can only remove a forged record that has a matched, eligible caller
span, so witness coverage caps H2b before any agent runs:
  * F2 (false status) and FS (status + duration on the same spans) - a forged non-OK status
    conflicts with the caller's recorded code whatever its size, so the ceiling is witness
    coverage on an R2-applicable route.
  * F3 (inflated duration) - R1 only contradicts an inflation that exceeds the caller's wait, so
    the ceiling also depends on the dose. Reported as a curve over dose multipliers.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from audit_traces import RE2_OB_ROOT, cases_re2_ob_dev, dev_templates, guard  # noqa: E402
from pairs import build_pairs  # noqa: E402

TASKS = os.path.join(REPO, "agent_tasks", "re2-ob")
ANSWERS = os.path.join(REPO, "agent_tasks", "re2-ob.answers.jsonl")
ALARM_CUTS = [2, 3, 5, 10]          # robust z-score cuts reported for check (a)
DOSES = [1.5, 2, 5, 10, 100]        # F3 inflation multipliers reported for the ceiling curve
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


def trace_name(service):
    """candidate_services and metrics say 'frontend'; traces say 'frontendservice'."""
    return {"frontend": "frontendservice", "frontend-external": "frontendservice"}.get(service, service)


def load_task(case_id):
    """The blind task view, restricted to the 36 development cases."""
    allowed_cases = {c for c, _d, _t in cases_re2_ob_dev(dev_templates()[0])}
    if case_id not in allowed_cases:
        sys.exit(f"REFUSED (data hygiene): {case_id} is not one of the 36 development cases")
    with open(os.path.join(TASKS, "cases", case_id, "task.json")) as f:
        return json.load(f)


def baro_scores(case_dir, inject_time, allowed):
    """BARO's RobustScorer per metric column: max |z| on the post-inject window, with the
    scaler fitted on the pre-inject window, returned as the max over each service's columns.

    The root cause is scored too, because "a BARO-level alarm" is a statement about where a
    service sits in BARO's ranking, and the root cause normally heads it.
    """
    # RE2's *primary* metric file is simple_metrics.csv - per-service gauges (cpu, mem, latency,
    # ...). The raw metrics.csv holds 418 cumulative counters, on which a RobustScaler fitted
    # before the fault reports a huge z after it simply because the counter kept rising, and
    # RCAEval's preprocess() does not difference them. BARO is scored on the simple file.
    path = guard("re2-ob-dev", os.path.join(case_dir, "simple_metrics.csv"), allowed)
    m = pd.read_csv(path)
    if "time" not in m.columns:
        return {}
    normal, anomal = m[m.time < inject_time], m[m.time >= inject_time]
    if normal.empty or anomal.empty:
        return {}
    out, skipped = {}, []
    for col in m.columns:
        if col == "time" or "_" not in col:
            continue
        svc = col.split("_", 1)[0]
        a = pd.to_numeric(normal[col], errors="coerce").to_numpy()
        b = pd.to_numeric(anomal[col], errors="coerce").to_numpy()
        a, b = a[~np.isnan(a)], b[~np.isnan(b)]
        if len(a) < 5 or len(b) < 1 or np.all(a == a[0]):
            continue
        # A column that is flat before the fault has a zero inter-quartile range, and scaling
        # by it yields a meaningless z (redis scored 131072 this way). BARO ranks such columns
        # anyway; for an alarm test they carry no information, so they are skipped and counted.
        if np.percentile(a, 75) - np.percentile(a, 25) == 0:
            skipped.append(col)
            continue
        try:
            z = RobustScaler().fit(a.reshape(-1, 1)).transform(b.reshape(-1, 1))[:, 0]
        except Exception:
            continue
        z = z[np.isfinite(z)]
        if not len(z):
            continue
        out[svc] = max(out.get(svc, 0.0), float(np.max(np.abs(z))))
    return out, skipped


def run_case(case_id, case_dir, out_dir, allowed):
    task = load_task(case_id)
    inject = int(task["inject_time"])
    with open(ANSWERS) as f:
        answer = {json.loads(l)["case_id"]: json.loads(l) for l in f}[case_id]
    root = trace_name(answer["service"])

    pairs = build_pairs("re2-ob-dev", case_dir)
    tpath = guard("re2-ob-dev", os.path.join(case_dir, "traces.csv"), allowed)
    tr = pd.read_csv(tpath, usecols=["spanID", "serviceName", "parentSpanID",
                                     "startTime", "duration", "statusCode"], low_memory=False)
    sid = tr.spanID.astype(str)
    svc_map = dict(zip(sid, tr.serviceName.astype(str)))
    p_svc = tr.parentSpanID.astype(str).map(svc_map)
    tr["is_server"] = p_svc.notna() & (p_svc != tr.serviceName.astype(str))
    tr["post"] = (tr.startTime // 1_000_000) >= inject

    edges = set(zip(pairs.client_service, pairs.server_service))
    callers_of = lambda s: {a for a, b in edges if b == s}
    callees_of = lambda s: {b for a, b in edges if a == s}

    lpath = guard("re2-ob-dev", os.path.join(case_dir, "logs.csv"), allowed)
    log_svcs = set(pd.read_csv(lpath, usecols=["container_name"]).container_name.astype(str).unique())
    alarms, flat_cols = baro_scores(case_dir, inject, allowed)

    pairs["post"] = (pairs.server_start // 1_000_000) >= inject
    n_eligible = {c: 0 for c in ALARM_CUTS}
    shapes_at_3 = []

    for cand in sorted({trace_name(c) for c in task["candidate_services"]}):
        is_root = cand == root
        shape = ("root" if is_root
                 else "callee" if cand in callees_of(root)
                 else "sibling" if (callers_of(root) & callers_of(cand))
                 else "other")
        upstream = cand in callers_of(root)

        own = tr[tr.serviceName.astype(str) == cand]
        srv_post = own[own.is_server & own.post]
        srv_pre = own[own.is_server & ~own.post]
        err = lambda d: float((d.statusCode.fillna(0) != 0).mean()) if len(d) else 0.0
        err_pre, err_post = err(srv_pre), err(srv_post)

        has_logs = any(str(c).startswith(cand.replace("service", "")) or str(c) == cand
                       for c in log_svcs)
        alarm_z = alarms.get(cand, alarms.get(cand.replace("service", ""), float("nan")))

        tp = pairs[(pairs.server_service == cand) & pairs.post]
        witness = len(tp) / len(srv_post) if len(srv_post) else 0.0
        applicable = tp[tp.role_tier.isin(["STRONG", "ROOT_CLIENT"])]
        witness_applicable = len(applicable) / len(srv_post) if len(srv_post) else 0.0

        row = {"case_id": case_id, "root_cause": root, "fault": answer["fault"],
               "candidate": cand, "is_root_cause": is_root,
               "shape": shape, "is_upstream_caller_of_root": upstream,
               "baro_robust_z": round(alarm_z, 3) if alarm_z == alarm_z else "",
               "server_spans_post": len(srv_post), "server_spans_pre": len(srv_pre),
               "err_rate_pre": round(err_pre, 6), "err_rate_post": round(err_post, 6),
               "err_rate_rose": err_post > err_pre, "has_log_lines": has_logs,
               "matched_pairs_post": len(tp), "rule_applicable_pairs_post": len(applicable),
               "witness_coverage": round(witness, 4),
               "witness_coverage_rule_applicable": round(witness_applicable, 4)}
        for cut in ALARM_CUTS:
            ok = (not is_root and shape in ("callee", "sibling") and not upstream
                  and (alarm_z != alarm_z or alarm_z < cut)
                  and not (err_post > err_pre) and has_logs and len(srv_post) > 0)
            row[f"eligible_at_z{cut}"] = ok
            if ok:
                n_eligible[cut] += 1
                if cut == 3:
                    shapes_at_3.append(shape)
        # F3 ceiling: an inflated span is contradicted only once it passes the caller's wait.
        # Every key is always present, so the CSV keeps one schema.
        slack = (applicable.client_duration - applicable.server_duration) if len(applicable) else None
        for d in DOSES:
            row[f"f3_ceiling_dose_{d}x"] = (
                round(float(((applicable.server_duration * d) > applicable.client_duration).mean())
                      * witness_applicable, 4) if len(applicable) else "")
        # F2 and FS do not depend on the dose: a forged non-OK status conflicts with the
        # caller's recorded code whatever its size, wherever the pair is rule-applicable.
        row["f2_fs_ceiling"] = round(witness_applicable, 4)
        row["r1_slack_p50_us"] = int(slack.median()) if len(applicable) else ""
        row["r1_slack_min_us"] = int(slack.min()) if len(applicable) else ""
        row["ratio_p50"] = (round(float((applicable.client_duration /
                                         applicable.server_duration.clip(lower=1)).median()), 2)
                            if len(applicable) else "")
        emit(out_dir, "candidates.csv", row)

    emit(out_dir, "case_eligibility.csv", {
        "case_id": case_id, "root_cause": root, "fault": answer["fault"],
        **{f"n_eligible_at_z{c}": n_eligible[c] for c in ALARM_CUTS},
        "shapes_at_z3": "|".join(sorted(shapes_at_3)) or "none",
        "flat_metric_cols_skipped": len(flat_cols),
        "n_callee_at_z3": shapes_at_3.count("callee"),
        "n_sibling_at_z3": shapes_at_3.count("sibling")})
    return (f"{case_id}: root {root} ({answer['fault']}), eligible@z3 {n_eligible[3]} "
            f"[{shapes_at_3.count('callee')} callee, {shapes_at_3.count('sibling')} sibling]")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    allowed, _ = dev_templates()
    cases = cases_re2_ob_dev(allowed)
    if a.only:
        cases = [c for c in cases if c[0] == a.only or c[1].endswith(a.only)]
    if a.limit:
        cases = cases[:a.limit]
    with open(os.path.join(a.out, "run.env.json"), "w") as f:
        json.dump({"n_cases": len(cases), "alarm_cuts": ALARM_CUTS, "f3_doses": DOSES,
                   "python": sys.version.split()[0], "pandas": pd.__version__}, f, indent=2)
    print(f"step 8 | {len(cases)} development cases -> {a.out}", flush=True)
    for i, (cid, cdir, _t) in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {run_case(cid, cdir, a.out, allowed)}", flush=True)
    for f in SINKS.values():
        f.close()
    print("done")


if __name__ == "__main__":
    main()
