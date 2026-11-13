#!/usr/bin/env python3
"""Provenance manifest for the upstream sources read in G0 stage 2 steps 4-6.

Steps 4-6 are source reading, not data work, so the evidence is a quotation from a file
at a stated version. This records what was read, from where, and its SHA-256, so a reader
can fetch the same bytes. Run from the repo root:

    python stage2/sources/manifest.py /home/opc/stage2-sources > stage2/sources/manifest.csv
"""
import csv
import hashlib
import os
import sys

# local directory -> (what it is, upstream repo, version/tag, raw URL prefix)
ORIGINS = {
    "ob-v0.9.0": ("Online Boutique application source", "GoogleCloudPlatform/microservices-demo",
                  "v0.9.0 (released 2023-12-18; the release in force when RE2-OB was collected)",
                  "https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/v0.9.0/"),
    "otelgrpc-v0.46.1": ("Go gRPC instrumentation (frontend, checkoutservice)",
                         "open-telemetry/opentelemetry-go-contrib",
                         "instrumentation/google.golang.org/grpc/otelgrpc/v0.46.1 (pinned in both go.mod files)",
                         "https://raw.githubusercontent.com/open-telemetry/opentelemetry-go-contrib/instrumentation/google.golang.org/grpc/otelgrpc/v0.46.1/instrumentation/google.golang.org/grpc/otelgrpc/"),
    "otel-python-0.41b0": ("Python gRPC instrumentation (recommendationservice, emailservice)",
                           "open-telemetry/opentelemetry-python-contrib",
                           "v0.41b0 (pinned in requirements.txt)",
                           "https://raw.githubusercontent.com/open-telemetry/opentelemetry-python-contrib/v0.41b0/instrumentation/opentelemetry-instrumentation-grpc/src/opentelemetry/instrumentation/grpc/"),
    "otel-js-0.45.1": ("Node.js gRPC instrumentation (currencyservice, paymentservice)",
                       "open-telemetry/opentelemetry-js",
                       "experimental/v0.45.1 (@opentelemetry/instrumentation-grpc 0.45.1, pinned in package.json)",
                       "https://raw.githubusercontent.com/open-telemetry/opentelemetry-js/experimental/v0.45.1/experimental/packages/opentelemetry-instrumentation-grpc/src/grpc-js/"),
    "otel-semconv": ("OpenTelemetry semantic conventions", "open-telemetry (spec site and spec repo)",
                     "current site pages fetched 2026-11-13; rpc-v1.17.0.md is specification v1.17.0",
                     "see the url column"),
    "rcaeval": ("RCAEval documentation", "phamquiluan/RCAEval", "main, fetched 2026-11-13",
                "https://raw.githubusercontent.com/phamquiluan/RCAEval/main/docs/"),
}
SITE = {
    "grpc.html": "https://opentelemetry.io/docs/specs/semconv/rpc/grpc/",
    "grpc.txt": "https://opentelemetry.io/docs/specs/semconv/rpc/grpc/ (text extraction)",
    "http-spans.html": "https://opentelemetry.io/docs/specs/semconv/http/http-spans/",
    "http-spans.txt": "https://opentelemetry.io/docs/specs/semconv/http/http-spans/ (text extraction)",
    "rpc-v1.17.0.md": "https://raw.githubusercontent.com/open-telemetry/opentelemetry-specification/v1.17.0/specification/trace/semantic_conventions/rpc.md",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    root = sys.argv[1]
    w = csv.writer(sys.stdout)
    w.writerow(["what", "repo", "version", "file", "url", "bytes", "sha256"])
    for d in sorted(ORIGINS):
        what, repo, version, prefix = ORIGINS[d]
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            for name in sorted(files):
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, base)
                url = SITE.get(name, prefix + rel if prefix != "see the url column" else "")
                w.writerow([what, repo, version, rel, url, os.path.getsize(p), sha256(p)])


if __name__ == "__main__":
    main()
