"""G0 stage 2, step 11a, item A: which routes the development data covers.

Proposal Appendix B.9 asks for "routes covered by development data", measured on RE2-OB dev,
feeding the R1 allow-list and the question of whether any template should move to development.
Steps 2, 5, 7 and 8 answered it at **service-pair** granularity (9 Online Boutique pairs). The
allow-list is written per route, and a route is a method, so this collates those outputs down to
one row per (caller, callee, method) and says what each one supports.

It reads only aggregates already published under `09-results/g0/` plus `demo.proto` v0.9.0 from
the step 4-6 source tree. No case file is opened, so there is nothing for the hygiene guard to
refuse: the inputs are the dev-only aggregates themselves.

Usage:
  python stage2/route_coverage.py \
      --evidence /home/opc/Msc-project/09-results/g0 \
      --proto /home/opc/stage2-sources/ob-v0.9.0/protos/demo.proto \
      --out stage2/out/step11a-<date>
"""
import argparse
import json
import os
import re

import pandas as pd

# the grpc service in an operation name -> the Kubernetes service that serves it.
# Derived from the data: every operation name is `[grpc.|/]<package>.<Service>/<Method>`.
NOT_AN_APP_ROUTE = {
    "grpc.health.v1.Health": ("(health probe)", "gRPC health probe, not an application RPC"),
    "opentelemetry.proto.collector.trace.v1.TraceService": (
        "(otel collector)", "the span exporter's own OTLP call, not an application RPC"),
}


def k8s_service(grpc_service):
    """'hipstershop.PaymentService' -> 'paymentservice'."""
    if grpc_service in NOT_AN_APP_ROUTE:
        return NOT_AN_APP_ROUTE[grpc_service][0]
    name = grpc_service.rsplit(".", 1)[-1]
    return name.lower()


def split_operation(op):
    """('hipstershop.PaymentService', 'Charge') from any of the three recorded forms."""
    if not isinstance(op, str) or "/" not in op:
        return None, None
    body = op[1:] if op.startswith("/") else op
    # the three recorded forms differ by a `grpc.` prefix the Python instrumentation adds;
    # strip it only when what remains is still a fully qualified grpc service
    if body.startswith("grpc.") and not body.startswith("grpc.health.v1."):
        body = body[len("grpc."):]
    service, _, method = body.rpartition("/")
    return service, method


def parse_proto(path):
    """{(grpc service, rpc): declaration} for every rpc in demo.proto."""
    text = open(path).read()
    text = re.sub(r"//[^\n]*", "", text)
    package = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    package = package.group(1) if package else ""
    rpcs = {}
    for m in re.finditer(r"service\s+(\w+)\s*\{(.*?)\n\}", text, re.S):
        service, body = m.group(1), m.group(2)
        for r in re.finditer(r"rpc\s+(\w+)\s*\(([^)]*)\)\s*returns\s*\(([^)]*)\)", body):
            rpcs[(f"{package}.{service}" if package else service, r.group(1))] = {
                "request": r.group(2).strip(), "response": r.group(3).strip()}
    return rpcs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True, help="Msc-project/09-results/g0")
    ap.add_argument("--proto", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    ev = args.evidence

    audit = os.path.join(ev, "trace-audit", "2026-11-13")
    smo = pd.read_csv(os.path.join(audit, "svc_method_op.csv"))
    calls = pd.read_csv(os.path.join(audit, "client_spans_without_server_child.csv"))
    roles = pd.read_csv(os.path.join(audit, "role_coverage.csv"))
    r1 = pd.read_csv(os.path.join(ev, "rule-prototype", "2026-11-13", "r1_by_route.csv"))
    r2 = pd.read_csv(os.path.join(ev, "rule-prototype", "2026-11-13", "r2_by_route.csv"))
    cands = pd.read_csv(os.path.join(ev, "target-coverage", "2026-11-13", "candidates.csv"))
    proto = parse_proto(args.proto)

    smo = smo[smo.dataset == "re2-ob-dev"]
    calls = calls[calls.dataset == "re2-ob-dev"]
    roles = roles[roles.dataset == "re2-ob-dev"]
    r1 = r1[r1.dataset == "re2-ob-dev"]
    r2 = r2[r2.dataset == "re2-ob-dev"]

    # (caller, method) -> the grpc service it called, from the client spans' operation names
    callee_of = {}
    for _, row in smo.iterrows():
        service, method = split_operation(row.operationName)
        if service and method:
            callee_of.setdefault((row.serviceName, row.methodName), set()).add(service)

    # client-side census per (caller, method), pooled over the 36 dev cases
    client = (calls.groupby(["serviceName", "call"])[["n_client_spans", "n_with_cross_child"]]
              .sum().reset_index()
              .rename(columns={"serviceName": "caller", "call": "method"}))
    client["cases_seen"] = (calls.groupby(["serviceName", "call"])["case_id"]
                            .nunique().reset_index(drop=True))

    # service-pair facts from steps 2, 7 and 8
    pair_tier = (roles.groupby(["parent_service", "child_service"])
                 [["cross_edges", "role_strong", "role_root_client", "role_weak_verb_only", "role_na"]]
                 .sum())
    r1_pair = r1.groupby(["client_service", "server_service"]).agg(
        pairs=("n_pairs", "sum"), r1_violations_eps0=("r1_violations_eps_0us", "sum"),
        slack_min_us=("slack_min_us", "min"), role_tier=("role_tier", lambda s: "|".join(sorted(set(s)))))
    r2_cols = [c for c in r2.columns if c.startswith("r2_") or c in ("n_pairs",)]
    r2_pair = r2.groupby(["client_service", "server_service"])[r2_cols].sum()

    elig = cands[~cands.is_root_cause].groupby("candidate").agg(
        dev_candidacies=("candidate", "size"),
        eligible_at_z3=("eligible_at_z3", "sum"),
        witness_coverage_min=("witness_coverage", "min"))

    rows = []
    for _, c in client.iterrows():
        services = sorted(callee_of.get((c.caller, c.method), {"(unknown)"}))
        for grpc_service in services:
            callee = k8s_service(grpc_service)
            declared = (grpc_service, c.method) in proto
            note = NOT_AN_APP_ROUTE.get(grpc_service, (None, ""))[1]
            pair = (c.caller, callee)
            has_witness = c.n_with_cross_child > 0
            tier = r1_pair.loc[pair, "role_tier"] if pair in r1_pair.index else ""
            row = {
                "caller": c.caller, "callee": callee, "method": c.method,
                "grpc_service": grpc_service,
                "declared_in_demo_proto": declared,
                "client_spans_dev": int(c.n_client_spans),
                "client_spans_with_server_child": int(c.n_with_cross_child),
                "witness_share": round(c.n_with_cross_child / c.n_client_spans, 6)
                                 if c.n_client_spans else 0.0,
                "cases_seen": int(c.cases_seen),
                "role_tier": tier,
                "pair_cross_edges": int(pair_tier.loc[pair, "cross_edges"])
                                    if pair in pair_tier.index else 0,
                "pair_role_na": int(pair_tier.loc[pair, "role_na"])
                                if pair in pair_tier.index else 0,
                "pair_matched_pairs": int(r1_pair.loc[pair, "pairs"]) if pair in r1_pair.index else 0,
                "r1_allowlisted": pair in r1_pair.index,
                "r1_clean_violations_eps0": int(r1_pair.loc[pair, "r1_violations_eps0"])
                                            if pair in r1_pair.index else None,
                "r1_slack_min_us": r1_pair.loc[pair, "slack_min_us"] if pair in r1_pair.index else None,
                "r2_applicable": pair in r2_pair.index,
                "callee_dev_candidacies": int(elig.loc[callee, "dev_candidacies"])
                                          if callee in elig.index else 0,
                "callee_eligible_at_z3": int(elig.loc[callee, "eligible_at_z3"])
                                         if callee in elig.index else 0,
                "note": note,
            }
            if note:
                row["verdict"] = "not-an-app-route"
            elif not has_witness:
                row["verdict"] = "no-witness"
                row["note"] = (f"{c.n_client_spans} client spans in dev data and not one server "
                               f"child: {callee} emits no server span, so no rule can check a "
                               f"claim about it")
            elif not row["r1_allowlisted"]:
                row["verdict"] = "observed-but-not-checkable"
            else:
                row["verdict"] = "covered"
                if row["role_tier"] == "ROOT_CLIENT":
                    row["note"] = "role check (b) relies on the trace-root relaxation (K15)"
            rows.append(row)

    # routes declared in demo.proto that no client span in the dev data exercises
    observed = {(r["grpc_service"], r["method"]) for r in rows}
    for (grpc_service, method) in sorted(proto):
        if (grpc_service, method) not in observed:
            rows.append({
                "caller": "(none observed)", "callee": k8s_service(grpc_service), "method": method,
                "grpc_service": grpc_service, "declared_in_demo_proto": True,
                "client_spans_dev": 0, "client_spans_with_server_child": 0, "witness_share": 0.0,
                "cases_seen": 0, "role_tier": "", "pair_cross_edges": 0, "pair_role_na": 0,
                "pair_matched_pairs": 0, "r1_allowlisted": False, "r1_clean_violations_eps0": None,
                "r1_slack_min_us": None, "r2_applicable": False, "callee_dev_candidacies": 0,
                "callee_eligible_at_z3": 0, "verdict": "declared-never-observed",
                "note": "declared in demo.proto v0.9.0; the load generator never drives it in the "
                        "development data, so no rule is checked on it",
            })

    out = pd.DataFrame(rows).sort_values(["verdict", "caller", "callee", "method"])
    out.to_csv(os.path.join(args.out, "routes_dev_coverage.csv"), index=False)

    # which root-cause service each RE2-OB template names, and whether its inbound routes
    # are covered. Template NAMES only -- no case is opened.
    dev_services = {"checkoutservice", "currencyservice"}
    s1_services = {"emailservice", "productcatalogservice", "recommendationservice"}
    faults = ["cpu", "delay", "disk", "loss", "mem", "socket"]
    app = out[~out.verdict.isin(["not-an-app-route"])]
    trows = []
    for service in sorted(dev_services | s1_services):
        inbound = app[app.callee == service]
        covered = inbound[inbound.verdict == "covered"]
        for fault in faults:
            trows.append({
                "template": f"{service}_{fault}", "root_cause_service": service, "fault": fault,
                "stratum": "development" if service in dev_services else "S1 (test)",
                "inbound_routes": len(inbound),
                "inbound_routes_covered": len(covered),
                "inbound_methods_covered": "|".join(sorted(set(covered.method))),
                "inbound_methods_without_witness": "|".join(
                    sorted(set(inbound[inbound.verdict == "no-witness"].method))),
                "covered_by_dev_data": len(covered) > 0,
            })
    tmap = pd.DataFrame(trows)
    tmap.to_csv(os.path.join(args.out, "template_route_map.csv"), index=False)

    summary = {
        "routes_total": len(out),
        "by_verdict": out.verdict.value_counts().to_dict(),
        "app_routes": int(len(app)),
        "app_routes_covered": int((app.verdict == "covered").sum()),
        "callees_without_any_server_span": sorted(
            set(app[app.verdict == "no-witness"].callee) - set(app[app.verdict == "covered"].callee)),
        "client_spans_no_witness": int(app[app.verdict == "no-witness"].client_spans_dev.sum()),
        "client_spans_covered": int(app[app.verdict == "covered"].client_spans_dev.sum()),
        "s1_templates_covered": int(tmap[(tmap.stratum == "S1 (test)") & tmap.covered_by_dev_data].shape[0]),
        "s1_templates_total": int((tmap.stratum == "S1 (test)").sum()),
        "proto_rpcs_declared": len(proto),
        "proto_rpcs_never_observed": int((out.verdict == "declared-never-observed").sum()),
    }
    with open(os.path.join(args.out, "route_coverage_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(out.groupby("verdict")[["client_spans_dev"]].agg(["size", "sum"]).to_string())
    print()
    print(out[out.verdict.isin(["covered", "no-witness"])][
        ["caller", "callee", "method", "client_spans_dev", "witness_share", "role_tier", "verdict"]
    ].to_string(index=False))
    print()
    print(json.dumps(summary, indent=2))
    print("ALL_DONE")


if __name__ == "__main__":
    main()
