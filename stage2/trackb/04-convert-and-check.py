"""Track B spike, the end-to-end check: does a real deployment's telemetry reach traces.csv
in a state R1 and R2 can use?

Reads the collector's OTLP/JSON, runs the real `rcaeval-convert traces`, then measures on the
converted table the same things steps 1-3 measured on RE2-OB, so the two are comparable:

  * which status-code convention the application actually emitted (this is K18's question, and
    the answer is a property of the instrumentation, not of the collector);
  * the schema: the 11 columns RCAEval's datasets have, and whether serviceName is converter-legal;
  * role inference on cross-service edges, with the same normalisation as `audit_traces.py`;
  * one-to-one attempt matching, and R1's enclosure on every matched pair;
  * the (client, server) code census R2 needs.

Usage:
  python stage2/trackb/04-convert-and-check.py --capture stage2/out/step10-<date>/capture
"""
import argparse
import collections
import json
import os
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from audit_traces import op_norm, op_tier  # noqa: E402  (the project's own definitions)

LEGACY_KEY = "rpc.grpc.status_code"
CURRENT_KEY = "rpc.status_code"


def scan_otlp(path):
    """Attribute keys, span and service counts, straight from what the collector wrote."""
    keys = collections.Counter()
    services, spans, requests = collections.Counter(), 0, 0
    codes = collections.Counter()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            requests += 1
            body = json.loads(line)
            for rs in body.get("resourceSpans", []):
                service = None
                for a in rs.get("resource", {}).get("attributes", []):
                    if a["key"] == "service.name":
                        service = a["value"].get("stringValue")
                for scope in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
                    for span in scope.get("spans", []):
                        spans += 1
                        services[service] += 1
                        for a in span.get("attributes", []):
                            keys[a["key"]] += 1
                            if a["key"] in (LEGACY_KEY, CURRENT_KEY):
                                v = a["value"]
                                codes[(a["key"], v.get("intValue", v.get("stringValue")))] += 1
    return {"requests": requests, "spans": spans, "services": dict(services),
            "attribute_keys": dict(keys.most_common()), "status_values": {f"{k}={v}": n for (k, v), n in codes.items()}}


def convert(otlp_path, case_dir):
    cmd = [sys.executable, "-m", "RCAEval.convert", "traces", otlp_path,
           "--out", case_dir, "--force"]
    print("$ " + " ".join(cmd), flush=True)
    run = subprocess.run(cmd, capture_output=True, text=True)
    print(run.stdout.strip() or "(no stdout)")
    if run.returncode != 0:
        print(run.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"rcaeval-convert failed with {run.returncode}")
    return os.path.join(case_dir, "traces.csv")


def analyse(traces_path):
    df = pd.read_csv(traces_path, dtype={"traceID": str, "spanID": str, "parentSpanID": str})
    out = {"rows": len(df), "columns": list(df.columns),
           "distinct_traces": df.traceID.nunique(), "distinct_spans": df.spanID.nunique(),
           "duplicate_span_ids": int(len(df) - df.spanID.nunique()),
           "services": sorted(df.serviceName.dropna().unique().tolist()),
           "service_names_with_underscore": sorted(
               s for s in df.serviceName.dropna().unique() if "_" in s),
           "statusCode_distribution": {str(k): int(v) for k, v in
                                       df.statusCode.value_counts(dropna=False).items()}}

    svc = dict(zip(df.spanID, df.serviceName))
    meth = dict(zip(df.spanID, df.methodName))
    op = dict(zip(df.spanID, df.operationName))
    dur = dict(zip(df.spanID, df.duration))
    code = dict(zip(df.spanID, df.statusCode))

    orphan = same = cross = 0
    strong = named = 0
    tiers = collections.Counter()
    pairs = []
    for sid, parent in zip(df.spanID, df.parentSpanID):
        if not isinstance(parent, str) or parent in ("", "nan"):
            orphan += 1
            continue
        if parent not in svc:
            orphan += 1
            continue
        if svc[parent] == svc[sid]:
            same += 1
            continue
        cross += 1
        # the project's own role test: method equality (or unavailable) plus operation agreement
        m_ok = (meth.get(parent) == meth.get(sid)) or pd.isna(meth.get(sid))
        tier = op_tier(op.get(parent), op.get(sid))
        tiers[tier] += 1
        # the tiers audit_traces.op_tier returns for a pair that names the same operation;
        # "normalised" was a wrong guess at this name on the first run and silently counted
        # every prefix-normalised match as a role failure, halving the reported coverage
        if m_ok and tier in ("exact", "norm_equal", "unavailable"):
            named += 1
            # role check (b): the candidate client span has a same-service parent of its own
            gp = df.loc[df.spanID == parent, "parentSpanID"]
            gp = gp.iloc[0] if len(gp) else None
            if isinstance(gp, str) and gp in svc and svc[gp] == svc[parent]:
                strong += 1
            pairs.append((parent, sid))
    out["op_tier_census"] = {k: v for k, v in sorted(tiers.items())}
    out.update({"orphans": orphan, "same_service_edges": same, "cross_service_edges": cross,
                "partition_sums_to_rows": orphan + same + cross == len(df),
                "role_named": named, "role_strong": strong,
                "role_named_share": round(named / cross, 6) if cross else None})

    # one-to-one matching: a client span with exactly one server child on the same call
    by_parent = collections.Counter(p for p, _ in pairs)
    out["amb_non_1to1"] = sum(1 for p, n in by_parent.items() if n > 1)
    out["matched_pairs"] = len(pairs)

    # R1: server duration must not exceed the client's
    viol, margins = 0, []
    census = collections.Counter()
    for client, server in pairs:
        slack = dur[client] - dur[server]
        margins.append(slack)
        if slack < 0:
            viol += 1
        census[(code.get(client), code.get(server))] += 1
    out["r1_violations_eps0"] = viol
    out["r1_slack_min_us"] = int(min(margins)) if margins else None
    out["r1_slack_median_us"] = int(pd.Series(margins).median()) if margins else None
    out["paired_code_census"] = {f"client={c}/server={s}": n for (c, s), n in census.items()}
    return out, df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", required=True, help="the 03-capture.sh output directory")
    args = ap.parse_args()
    otlp = os.path.join(args.capture, "otlp-traces.jsonl")
    if not os.path.exists(otlp):
        raise SystemExit(f"no collector output at {otlp}")

    print("=== what the collector wrote")
    raw = scan_otlp(otlp)
    print(f"  {raw['requests']} export requests, {raw['spans']} spans, "
          f"{len(raw['services'])} services")
    print(f"  {LEGACY_KEY} present on {raw['attribute_keys'].get(LEGACY_KEY, 0)} spans")
    print(f"  {CURRENT_KEY} present on {raw['attribute_keys'].get(CURRENT_KEY, 0)} spans")
    print(f"  status values: {raw['status_values']}")

    print("\n=== conversion")
    traces_path = convert(otlp, os.path.join(args.capture, "case"))

    print("\n=== the converted table")
    result, df = analyse(traces_path)
    for k in ("rows", "distinct_traces", "duplicate_span_ids", "services",
              "service_names_with_underscore", "statusCode_distribution", "orphans",
              "same_service_edges", "cross_service_edges", "partition_sums_to_rows",
              "role_named", "role_strong", "role_named_share", "matched_pairs",
              "amb_non_1to1", "r1_violations_eps0", "r1_slack_min_us", "r1_slack_median_us",
              "paired_code_census"):
        print(f"  {k}: {result[k]}")

    verdict = {
        "k18_convention_emitted": (LEGACY_KEY if raw["attribute_keys"].get(LEGACY_KEY)
                                   else CURRENT_KEY if raw["attribute_keys"].get(CURRENT_KEY)
                                   else "neither"),
        "schema_matches_rcaeval": result["columns"] == [
            "time", "traceID", "spanID", "serviceName", "methodName", "operationName",
            "startTimeMillis", "startTime", "duration", "statusCode", "parentSpanID"],
        "roles_inferable": bool(result["role_named_share"] and result["role_named_share"] > 0.5),
        "r1_has_clean_violations": result["r1_violations_eps0"] > 0,
    }
    print(f"\n=== verdict\n{json.dumps(verdict, indent=2)}")

    with open(os.path.join(args.capture, "trackb_check.json"), "w") as f:
        json.dump({"otlp": raw, "converted": result, "verdict": verdict}, f, indent=2)
    print(f"wrote {os.path.join(args.capture, 'trackb_check.json')}")
    print("ALL_DONE")


if __name__ == "__main__":
    main()
