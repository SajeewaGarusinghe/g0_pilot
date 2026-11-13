"""G0 stage 2, step 11a, item B: who writes each recorded signal.

Proposal section 06 fixes what each fabrication family may change from "the writer of each
signal in the recorded systems", and says G0 stage 2 confirms that column against RCAEval's
collection setup and instrumentation source. This script does the confirming part that can be
measured rather than read: it proves, per column family and over all 36 development cases,
which series in `metrics.csv` each column of `simple_metrics.csv` *is*.

That matters because the proposal's table says the service-level latency and error-rate metrics
are "derived from D's spans at rebuild". If they are instead the Istio/Envoy sidecar's series
scraped by Prometheus, a compromised service cannot write them, and the overlays must leave them
alone instead of rebuilding them.

The hygiene guard and case enumeration are imported from `audit_traces.py` rather than rewritten.

Usage:
  python stage2/signal_writers.py --out stage2/out/step11a-<date>
  python stage2/signal_writers.py --out /tmp/x --guard-test        # must refuse and stop
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_traces import cases_re2_ob_dev, dev_templates, guard, sha256  # noqa: E402

# the hypothesis for each simple_metrics family: its source family in metrics.csv.
# every one of these is tested per service and per case, never assumed.
HYPOTHESIS = {
    "cpu": "container-cpu-usage-seconds-total",
    "mem": "container-memory-working-set-bytes",
    "socket": "container-sockets",
    "diskio": "container-blkio-device-usage-total",
    "latency-50": "istio-latency-50",
    "latency-90": "istio-latency-90",
    "error": "istio-error-total",
    "workload": "istio-request-total",
}

# who writes each family in metrics.csv, with the evidence for it
WRITERS = {
    "container-": ("kubelet / cAdvisor",
                   "cAdvisor's container_* counters and gauges, scraped by Prometheus; the "
                   "container's own process never writes them"),
    "node-": ("node exporter on the Kubernetes node",
              "node_* series keyed by the GKE node name (see metrics_postprocess.log); the "
              "node is outside every application writer domain"),
    "istio-": ("the Envoy sidecar in the pod, via Istio telemetry",
               "istio_requests_total / istio_request_duration_milliseconds, recorded by the "
               "sidecar proxy that terminates the connection, not by the service process"),
}

# one row per file the exported case gives the agent. `agent/export.py` maps
# simple_metrics.csv -> metrics.csv and metrics.csv -> metrics_raw.csv.
SIGNALS = [
    {
        "signal": "traces.csv", "holds": "every span of the incident window",
        "written_by": "each service's own process (OpenTelemetry SDK + gRPC instrumentation)",
        "evidence": "stage2/sources/manifest.csv: otelgrpc v0.46.1 (Go), "
                    "opentelemetry-instrumentation-grpc 0.41b0 (Python), "
                    "@opentelemetry/instrumentation-grpc 0.45.1 (Node); steps 4-6",
        "compromised_d_can_write": "yes, its own spans",
        "f1": "unchanged", "f2_f3_fs": "changed (D's server spans)",
        "note": "C's client spans for C->D are written by C's process, which is why R1 and R2 "
                "have a witness at all",
    },
    {
        "signal": "logs.csv", "holds": "application log lines, one row per line",
        "written_by": "each service's own process (stdout, collected from the container)",
        "evidence": "the lines are the services' own messages; cluster_info.json attributes "
                    "each mined template to the container that logged it",
        "compromised_d_can_write": "yes, its own lines",
        "f1": "added", "f2_f3_fs": "unchanged",
        "note": "RCAEval adds cluster_id and log_template columns with Drain3 "
                "(RCAEval/convert/logs.py, derive.py), so a forged line needs a template too",
    },
    {
        "signal": "logts.csv", "holds": "log lines per mined template per 15 s",
        "written_by": "RCAEval, derived from logs.csv",
        "evidence": "RCAEval/convert/derive.py: log_series()",
        "compromised_d_can_write": "only through its log lines",
        "f1": "rebuilt from the overlaid logs.csv", "f2_f3_fs": "unchanged",
        "note": "a forged line that matches no existing template creates a new column, which is "
                "itself detectable (step 9a, K22)",
    },
    {
        "signal": "tracets_err.csv", "holds": "error spans per operation per 15 s",
        "written_by": "RCAEval, derived from traces.csv",
        "evidence": "RCAEval/convert/derive.py: trace_series(), error = statusCode != 0",
        "compromised_d_can_write": "only through its spans",
        "f1": "unchanged", "f2_f3_fs": "rebuilt from the overlaid traces.csv",
        "note": "this is the only error series a span overlay can move",
    },
    {
        "signal": "tracets_lat.csv", "holds": "slow spans per operation per 15 s",
        "written_by": "RCAEval, derived from traces.csv",
        "evidence": "RCAEval/convert/derive.py: trace_series(), slow = duration > mean + 3 sd of "
                    "that operation's spans that ended before the failure started",
        "compromised_d_can_write": "only through its spans",
        "f1": "unchanged", "f2_f3_fs": "rebuilt from the overlaid traces.csv",
        "note": "this is the only latency series a span overlay can move",
    },
    {
        "signal": "metrics.csv as the agent sees it (= simple_metrics.csv)",
        "holds": "per-service cpu, mem, socket, diskio, latency-50/90, error, workload",
        "written_by": "kubelet/cAdvisor (cpu, mem, socket, diskio) and the Envoy sidecar via "
                      "Istio telemetry (latency-50/90, error, workload), scraped by Prometheus",
        "evidence": "column_writer_map.csv -- every column is identical, timestamp for "
                    "timestamp, to a container-* or istio-* series in metrics.csv",
        "compromised_d_can_write": "no",
        "f1": "unchanged", "f2_f3_fs": "unchanged",
        "note": "section 06's writer table calls the service-level latency and error metrics "
                "'derived from D's spans at rebuild'. They are not: they are the sidecar's own "
                "measurements, so the overlays must leave them alone",
    },
    {
        "signal": "metrics_raw.csv (= the 418-column metrics.csv)",
        "holds": "container-*, node-* and istio-* series for every service and node",
        "written_by": "kubelet/cAdvisor, node exporter, Envoy sidecar; scraped by Prometheus",
        "evidence": "metrics_postprocess.log names the query families and the GKE node pool; "
                    "RCAEval/convert/prometheus.py is the collection path",
        "compromised_d_can_write": "no",
        "f1": "unchanged", "f2_f3_fs": "unchanged",
        "note": "cumulative counters: step 8 had to score simple_metrics.csv, not this file",
    },
    {
        "signal": "inject_time.txt", "holds": "the failure start, unix seconds",
        "written_by": "the dataset authors' fault injector",
        "evidence": "used as the pre/post boundary by RCAEval/convert/derive.py and by this "
                    "project's step 7 and 8 scripts",
        "compromised_d_can_write": "no",
        "f1": "unchanged", "f2_f3_fs": "unchanged",
        "note": "a sealed label: it never reaches the agent (agent/export.py withholds it)",
    },
    {
        "signal": "cluster_info.json", "holds": "mined log template text -> container",
        "written_by": "RCAEval, derived from logs.csv with Drain3",
        "evidence": "RCAEval/convert/derive.py: log_series(); agent/export.py does not export it",
        "compromised_d_can_write": "only through its log lines",
        "f1": "rebuilt with the overlaid logs.csv", "f2_f3_fs": "unchanged",
        "note": "the template dictionary a forged F1 line must fit to look ordinary",
    },
    {
        "signal": "pod-node-*.csv", "holds": "pod -> node placement",
        "written_by": "the Kubernetes API server, at collection time",
        "evidence": "node names match the node-* series in metrics.csv "
                    "(gke-gke-cluster-default-pool-*)",
        "compromised_d_can_write": "no",
        "f1": "unchanged", "f2_f3_fs": "unchanged",
        "note": "RE2-OB only; not exported to the agent",
    },
]


def family(column):
    """('frontend', 'latency-50') from 'frontend_latency-50'."""
    service, _, name = column.partition("_")
    return service, name


def varies(series):
    """True when the series carries information: at least two distinct non-NaN values."""
    finite = series[~np.isnan(series)]
    return finite.size > 1 and float(np.nanstd(finite)) > 0.0


def writer_of(metrics_column):
    _, name = family(metrics_column)
    for prefix, (who, why) in WRITERS.items():
        if name.startswith(prefix):
            return who, why
    return "unidentified", ""


def compare_case(case_dir, dataset, allowed):
    """For one case: every simple_metrics column -> the metrics columns identical to it."""
    sm_path = guard(dataset, os.path.join(case_dir, "simple_metrics.csv"), allowed)
    mt_path = guard(dataset, os.path.join(case_dir, "metrics.csv"), allowed)
    sm = pd.read_csv(sm_path).set_index("time")
    mt = pd.read_csv(mt_path).set_index("time")
    common = sm.index.intersection(mt.index)
    sm, mt = sm.loc[common], mt.loc[common]
    by_service = {}
    for column in mt.columns:
        by_service.setdefault(family(column)[0], []).append(column)

    out = []
    for column in sm.columns:
        service, name = family(column)
        a = sm[column].to_numpy(dtype="float64")
        # a column that never moves matches every other column that never moves, so a match
        # against it identifies nothing. Those comparisons are counted apart.
        informative = varies(a)
        exact_any, exact_inf, best_corr, best_col = [], [], float("nan"), ""
        for candidate in by_service.get(service, []):
            b = mt[candidate].to_numpy(dtype="float64")
            both_nan = np.isnan(a) & np.isnan(b)
            close = np.isclose(a, b, rtol=1e-9, atol=0.0, equal_nan=True) | both_nan
            if close.all():
                exact_any.append(candidate)
                if informative and varies(b):
                    exact_inf.append(candidate)
            if varies(a) and varies(b):
                with np.errstate(invalid="ignore"):
                    r = pd.Series(a).corr(pd.Series(b))
                if r == r and (best_corr != best_corr or r > best_corr):
                    best_corr, best_col = r, candidate
        out.append({
            "service": service, "simple_metrics_column": name,
            "n_points": len(common),
            "column_varies": informative,
            "hypothesis": HYPOTHESIS.get(name, ""),
            "hypothesis_exact": HYPOTHESIS.get(name, "") in
                                [family(c)[1] for c in exact_any],
            "exact_matches_informative": "|".join(sorted(family(c)[1] for c in exact_inf)),
            "n_exact_informative": len(exact_inf),
            "exact_matches_any": "|".join(sorted(family(c)[1] for c in exact_any)),
            "n_exact_any": len(exact_any),
            "best_correlation": None if best_corr != best_corr else round(float(best_corr), 6),
            "best_correlated_column": family(best_col)[1] if best_col else "",
        })
    return out, {sm_path: os.path.getsize(sm_path), mt_path: os.path.getsize(mt_path)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--guard-test", action="store_true",
                    help="point the comparison at a test template; it must refuse")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    allowed, _ = dev_templates()

    if args.guard_test:
        print("guard test: asking for emailservice_cpu/1 (test stratum S1) ...")
        compare_case("/home/opc/g0_pilot/data/RE2/RE2-OB/emailservice_cpu/1",
                     "re2-ob-dev", allowed)
        sys.exit("GUARD TEST FAILED: the read was not refused")

    cases = cases_re2_ob_dev(allowed)
    if args.limit:
        cases = cases[: args.limit]
    rows, read_log = [], {}
    for i, (case_id, case_dir, template) in enumerate(cases, 1):
        got, files = compare_case(case_dir, "re2-ob-dev", allowed)
        for r in got:
            r.update(case_id=case_id, template=template)
        rows.extend(got)
        read_log.update(files)
        print(f"  [{i}/{len(cases)}] {case_id} {template}: {len(got)} columns", flush=True)

    per_case = pd.DataFrame(rows)
    per_case.to_csv(os.path.join(args.out, "column_writer_per_case.csv"), index=False)

    # one row per column family, over all cases
    fam = []
    for name, g in per_case.groupby("simple_metrics_column"):
        inf = g[g.column_varies]
        sources = sorted({s for row in inf.exact_matches_informative
                          for s in row.split("|") if s})
        who = sorted({writer_of("x_" + s)[0] for s in sources}) or ["unidentified"]
        unmatched = int((inf.n_exact_informative == 0).sum())
        fam.append({
            "simple_metrics_family": name,
            "services": g.service.nunique(),
            "cases": g.case_id.nunique(),
            "column_case_pairs": len(g),
            "informative_pairs": len(inf),
            "constant_pairs": len(g) - len(inf),
            "hypothesis": HYPOTHESIS.get(name, ""),
            "hypothesis_exact_rate_informative": round(inf.hypothesis_exact.mean(), 6)
                                                  if len(inf) else None,
            "source_series_informative": "|".join(sources),
            "n_source_series": len(sources),
            "informative_pairs_with_no_exact_source": unmatched,
            "written_by": "|".join(who),
            "writer_evidence": writer_of("x_" + sources[0])[1] if sources else "",
            "writer_resolved": len(who) == 1 and who != ["unidentified"] and unmatched == 0,
            "series_resolved": len(sources) == 1 and unmatched == 0,
        })
    fams = pd.DataFrame(fam).sort_values("simple_metrics_family")
    fams.to_csv(os.path.join(args.out, "column_writer_map.csv"), index=False)

    with open(os.path.join(args.out, "signal_writers.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SIGNALS[0].keys()))
        w.writeheader()
        w.writerows(SIGNALS)

    summary = {
        "dataset": "RE2-OB development cases only",
        "cases": len(cases),
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "families_writer_resolved": int(fams.writer_resolved.sum()),
        "families_series_resolved": int(fams.series_resolved.sum()),
        "families_total": int(len(fams)),
        "families_without_resolved_writer":
            fams[~fams.writer_resolved].simple_metrics_family.tolist(),
        "families_with_several_candidate_series":
            {r.simple_metrics_family: r.source_series_informative
             for r in fams[~fams.series_resolved].itertuples()},
        "sidecar_written_families": fams[fams.written_by.str.contains("Envoy")]
                                    .simple_metrics_family.tolist(),
        "platform_written_families": fams[fams.written_by.str.contains("cAdvisor")]
                                      .simple_metrics_family.tolist(),
        "files_read": len(read_log),
    }
    with open(os.path.join(args.out, "signal_writers_summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(args.out, "signal_writers_read_log.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "bytes", "sha256"])
        for path, size in sorted(read_log.items()):
            w.writerow([path, size, sha256(path)])

    print()
    pd.set_option("display.width", 200)
    print(fams[["simple_metrics_family", "services", "informative_pairs", "constant_pairs",
                "source_series_informative", "written_by", "writer_resolved",
                "series_resolved"]].to_string(index=False))
    print()
    print(json.dumps(summary, indent=2))
    print("ALL_DONE")


if __name__ == "__main__":
    main()
