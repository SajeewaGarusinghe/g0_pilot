#!/usr/bin/env bash
# Let the load generator run, then take the collector's OTLP/JSON out of the cluster.
set -euo pipefail
OUT=${OUT:?set OUT to the output directory}
WINDOW=${WINDOW:-360}          # seconds of traffic to capture

if ! docker ps >/dev/null 2>&1; then exec sg docker -c "OUT=$OUT WINDOW=$WINDOW $(printf '%q ' "$0" "$@")"; fi
mkdir -p "$OUT"

echo "=== waiting for every rollout to settle first"
for d in $(kubectl get deploy -o name); do
  kubectl rollout status "$d" --timeout=600s
done
kubectl -n observability rollout status deploy/otel-collector --timeout=300s

# The exporter's file is append-only, so a file that predates the application's current
# generation holds spans from the previous one. That matters here: a single span whose
# service.name still carries the SDK's "unknown_service:<exe>" fallback makes the whole
# conversion fail on the underscore. Restarting the collector gives a fresh, empty volume,
# so the capture covers exactly this window.
echo "=== restarting the collector for a clean file"
kubectl -n observability rollout restart deploy/otel-collector
kubectl -n observability rollout status deploy/otel-collector --timeout=300s

echo "=== recording the deployment for the record"
kubectl get pods -o wide > "$OUT/pods.txt"
kubectl get nodes -o wide > "$OUT/nodes.txt"
kubectl -n observability get deploy otel-collector \
  -o jsonpath='{.spec.template.spec.containers[0].image}' > "$OUT/collector-image.txt"
kubectl get deploy frontend -o jsonpath='{.spec.template.spec.containers[0].image}' \
  > "$OUT/frontend-image.txt"

echo "=== capturing ${WINDOW}s of traffic"
sleep "$WINDOW"

POD=$(kubectl -n observability get pod -l app=otel-collector -o jsonpath='{.items[0].metadata.name}')
echo "=== copying the collector's file out of $POD"
# read it through the busybox sidecar: the collector image is distroless, so it has neither a
# shell for `kubectl exec` nor the tar that `kubectl cp` needs. `cat` into a local file does.
kubectl -n observability exec "$POD" -c filereader -- \
  sh -c 'ls -la /data; wc -l /data/otlp-traces.jsonl' | tee "$OUT/collector-file-listing.txt"
kubectl -n observability exec "$POD" -c filereader -- \
  cat /data/otlp-traces.jsonl > "$OUT/otlp-traces.jsonl"
kubectl -n observability logs "$POD" -c otel-collector --tail=40 > "$OUT/collector.log"

ls -la "$OUT/otlp-traces.jsonl"
wc -l "$OUT/otlp-traces.jsonl"
