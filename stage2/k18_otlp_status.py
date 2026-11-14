"""G0 stage 2, step 10, first check: does a gRPC status code survive rcaeval-convert? (K18)

Track B deploys Online Boutique on k3d behind a *current* OpenTelemetry collector and converts
the result with `rcaeval-convert`, then applies R2 to the converted traces. R2 compares protocol
status codes, so Track B rests on those codes surviving the conversion.

RCAEval 1.12.0's converter (`RCAEval/convert/otlp.py`) reads `rpc.grpc.status_code`, a numeric
attribute defined by OpenTelemetry specification v1.17.0. The current gRPC conventions removed it
and use `rpc.status_code`, a string ("OK", "UNAVAILABLE"), plus `error.type` when the call failed
(`09-results/g0/06-status-semantics.md`, from the conventions fetched 2026-11-13).

This builds OTLP/JSON exactly as the collector's file exporter writes it -- one
ExportTraceServiceRequest per line -- for the legacy and the current conventions, runs the real
`rcaeval-convert traces` command, and reports the `statusCode` each case produces. No cluster is
needed to answer the question, so it is answered before anything is installed.

Usage:
  python stage2/k18_otlp_status.py --out stage2/out/step10-<date>
"""
import argparse
import csv
import json
import os
import subprocess
import sys

# (name, span attributes, OTLP status, what the case represents, expected statusCode, why)
CASES = [
    ("legacy_ok", {"rpc.system": "grpc", "rpc.method": "GetProduct", "rpc.grpc.status_code": 0},
     None, "v1.17.0 instrumentation, successful call", 0,
     "branch 1 reads the numeric attribute"),
    ("legacy_unavailable", {"rpc.system": "grpc", "rpc.method": "GetProduct",
                            "rpc.grpc.status_code": 14},
     "STATUS_CODE_ERROR", "v1.17.0 instrumentation, UNAVAILABLE", 14,
     "branch 1 reads the numeric attribute; this is what RE2-OB holds"),
    ("current_ok", {"rpc.system.name": "grpc", "rpc.method": "GetProduct",
                    "rpc.status_code": "OK"},
     None, "current instrumentation, successful call", 0,
     "branch 1 misses; the span is not an error, so branch 3 returns 0 -- right by luck"),
    ("current_unavailable", {"rpc.system.name": "grpc", "rpc.method": "GetProduct",
                             "rpc.status_code": "UNAVAILABLE", "error.type": "UNAVAILABLE"},
     "STATUS_CODE_ERROR", "current instrumentation, UNAVAILABLE", 2,
     "branch 1 misses; no HTTP attribute, so branch 2 returns 2 (gRPC UNKNOWN)"),
    ("current_deadline_exceeded", {"rpc.system.name": "grpc", "rpc.method": "GetProduct",
                                   "rpc.status_code": "DEADLINE_EXCEEDED",
                                   "error.type": "DEADLINE_EXCEEDED"},
     "STATUS_CODE_ERROR", "current instrumentation, DEADLINE_EXCEEDED", 2,
     "collapses to the same 2 as UNAVAILABLE: the codes become indistinguishable"),
    ("current_not_found", {"rpc.system.name": "grpc", "rpc.method": "GetProduct",
                           "rpc.status_code": "NOT_FOUND"},
     None, "current instrumentation, NOT_FOUND (not in the SHOULD-be-errors list for a server span)",
     0, "branch 1 misses and the span is not marked an error, so a genuine failure records as 0"),
    ("current_http_500", {"http.request.method": "GET", "http.route": "/product",
                          "http.response.status_code": 500},
     "STATUS_CODE_ERROR", "current HTTP instrumentation, 500", 500,
     "branch 2 reads the HTTP status"),
    ("current_http_404_server", {"http.request.method": "GET", "http.route": "/product",
                                 "http.response.status_code": 404},
     None, "current HTTP instrumentation, server span, 404", 0,
     "a server span is not an error for 4xx, so the code is lost (step 6's NA_STATUS_LOSSY)"),
]


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def span(name, span_id, parent, attributes, status, start_ns, end_ns):
    out = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [attr(k, v) for k, v in attributes.items()],
    }
    if parent:
        out["parentSpanId"] = parent
    if status:
        out["status"] = {"code": status}
    return out


def request_for(case_index, name, attributes, status):
    """One ExportTraceServiceRequest holding a client span and its server span,
    as the collector's file exporter would write it."""
    base = 1_762_000_000_000_000_000 + case_index * 1_000_000_000
    # hex ids that contain letters: an all-digit id is read back as an integer by
    # pandas and loses its leading zero, which silently breaks the join below
    client_id = f"c{case_index:02d}e11111111111b"
    server_id = f"s{case_index:02d}e22222222222b"
    resource = lambda service, spans: {
        "resource": {"attributes": [attr("service.name", service)]},
        "scopeSpans": [{"scope": {"name": "k18-fixture"}, "spans": spans}],
    }
    client = span(name, client_id, None, attributes, status, base, base + 5_000_000)
    server = span(name, server_id, client_id, attributes, status,
                  base + 1_000_000, base + 4_000_000)
    return {"resourceSpans": [resource("frontend", [client]),
                              resource("productcatalogservice", [server])]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    otlp_path = os.path.join(args.out, "k18_fixture.otlp.jsonl")
    with open(otlp_path, "w") as f:
        for i, (name, attributes, status, _, _, _) in enumerate(CASES, start=1):
            f.write(json.dumps(request_for(i, name, attributes, status)) + "\n")
    print(f"wrote {otlp_path}: {len(CASES)} cases, 2 spans each")

    case_dir = os.path.join(args.out, "converted")
    cmd = [sys.executable, "-m", "RCAEval.convert", "traces", otlp_path,
           "--out", case_dir, "--force"]
    print("$ " + " ".join(cmd))
    run = subprocess.run(cmd, capture_output=True, text=True)
    print(run.stdout.strip() or "(no stdout)")
    if run.returncode != 0:
        print(run.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"rcaeval-convert failed with {run.returncode}")

    import pandas as pd
    traces = pd.read_csv(os.path.join(case_dir, "traces.csv"),
                         dtype={"traceID": str, "spanID": str, "parentSpanID": str})
    print(f"converted {len(traces)} spans\n")

    rows, failures = [], 0
    for i, (name, attributes, status, meaning, expected, why) in enumerate(CASES, start=1):
        got = traces[traces.spanID == f"s{i:02d}e22222222222b"]
        actual = got.statusCode.iloc[0] if len(got) else None
        actual = None if actual is None or pd.isna(actual) else int(actual)
        client = traces[traces.spanID == f"c{i:02d}e11111111111b"]
        client_code = client.statusCode.iloc[0] if len(client) else None
        client_code = None if client_code is None or pd.isna(client_code) else int(client_code)
        ok = actual == expected
        failures += 0 if ok else 1
        rows.append({
            "case": name, "represents": meaning,
            "attributes_written": "; ".join(f"{k}={v}" for k, v in attributes.items()),
            "otlp_span_status": status or "UNSET",
            "server_statusCode": actual, "client_statusCode": client_code,
            "expected": expected, "as_predicted": ok, "why": why,
        })
        print(f"  {'ok ' if ok else 'DIFF'} {name:26s} server={actual!s:5s} client={client_code!s:5s} "
              f"(expected {expected})")

    path = os.path.join(args.out, "k18_status_survival.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    legacy = {r["case"]: r["server_statusCode"] for r in rows if r["case"].startswith("legacy")}
    current = {r["case"]: r["server_statusCode"] for r in rows if r["case"].startswith("current")}
    print(f"\nlegacy conventions:  {legacy}")
    print(f"current conventions: {current}")
    distinct_failures = {rows[3]["server_statusCode"], rows[4]["server_statusCode"]}
    print(f"\nK18 verdict:")
    print(f"  gRPC code survives under v1.17.0 attributes: "
          f"{'yes' if rows[1]['server_statusCode'] == 14 else 'no'}")
    print(f"  gRPC code survives under current attributes:  "
          f"{'yes' if rows[3]['server_statusCode'] == 14 else 'no'}")
    print(f"  UNAVAILABLE and DEADLINE_EXCEEDED remain distinct: "
          f"{'yes' if len(distinct_failures) == 2 else 'no, both become ' + str(distinct_failures.pop())}")
    print(f"  a non-error-classified failure (NOT_FOUND) records as: "
          f"{rows[5]['server_statusCode']}")
    # ---- feasibility probe: what a corrected mapping would have to do.
    # This does NOT patch RCAEval. It measures whether the information needed to recover the
    # code is present in the current-convention spans, so step 12 can choose between pinning
    # the instrumentation and extending the converter. The names are the gRPC status codes.
    GRPC_CODES = {"OK": 0, "CANCELLED": 1, "UNKNOWN": 2, "INVALID_ARGUMENT": 3,
                  "DEADLINE_EXCEEDED": 4, "NOT_FOUND": 5, "ALREADY_EXISTS": 6,
                  "PERMISSION_DENIED": 7, "RESOURCE_EXHAUSTED": 8, "FAILED_PRECONDITION": 9,
                  "ABORTED": 10, "OUT_OF_RANGE": 11, "UNIMPLEMENTED": 12, "INTERNAL": 13,
                  "UNAVAILABLE": 14, "DATA_LOSS": 15, "UNAUTHENTICATED": 16}
    probe = []
    for name, attributes, status, meaning, expected, why in CASES:
        if "rpc.status_code" not in attributes:
            continue
        recovered = GRPC_CODES.get(attributes["rpc.status_code"])
        converter = next(r["server_statusCode"] for r in rows if r["case"] == name)
        probe.append({"case": name, "rpc.status_code": attributes["rpc.status_code"],
                      "converter_gives": converter, "a_fix_would_give": recovered,
                      "recoverable": recovered is not None})
    probe_path = os.path.join(args.out, "k18_fix_feasibility.csv")
    with open(probe_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(probe[0].keys()))
        w.writeheader()
        w.writerows(probe)
    print("\nfeasibility probe (not a patch): reading rpc.status_code and mapping the name")
    for r in probe:
        print(f"  {r['case']:26s} {r['rpc.status_code']:18s} converter={r['converter_gives']!s:4s} "
              f"-> would be {r['a_fix_would_give']}")
    print(f"  every case recoverable from the span alone: "
          f"{'yes' if all(r['recoverable'] for r in probe) else 'no'}")
    print(f"wrote {probe_path}")

    print(f"\nwrote {path}")
    print("ALL_DONE" if failures == 0 else f"{failures} case(s) differed from the prediction")


if __name__ == "__main__":
    main()
