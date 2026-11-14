#!/usr/bin/env bash
# Deploy the collector and Online Boutique v0.9.0, with tracing switched on.
#
# The application manifest is the upstream v0.9.0 release, fetched separately (see README);
# it ships with no tracing environment, so ENABLE_TRACING and COLLECTOR_SERVICE_ADDR are set
# here. v0.9.0's images carry the instrumentation versions the project already documented in
# steps 4-6 (otelgrpc v0.46.1, opentelemetry-instrumentation-grpc 0.41b0, @opentelemetry 0.45.1).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST=${MANIFEST:-/home/opc/trackb-upstream/kubernetes-manifests.yaml}
COLLECTOR_ADDR=${COLLECTOR_ADDR:-otel-collector.observability.svc.cluster.local:4317}

if ! docker ps >/dev/null 2>&1; then exec sg docker -c "$(printf '%q ' "$0" "$@")"; fi
test -f "$MANIFEST" || { echo "manifest not found: $MANIFEST" >&2; exit 1; }

echo "=== collector (OTLP in, OTLP/JSON file out)"
kubectl apply -f "$HERE/collector.yaml"
kubectl -n observability rollout status deploy/otel-collector --timeout=180s

echo "=== Online Boutique v0.9.0"
kubectl apply -f "$MANIFEST"

echo "=== switch tracing on"
kubectl set env deployment --all \
  ENABLE_TRACING=1 "COLLECTOR_SERVICE_ADDR=$COLLECTOR_ADDR"

# Each SDK must be told its own service.name. Without it the Go and Python SDKs fall back to
# "unknown_service:<binary>", which carries an underscore -- and RCAEval's converter rejects any
# service name containing one (RCAEval/convert/common.py: check_service), so the conversion fails
# outright with:
#   service name 'unknown_service:checkoutservice' must be non-empty and contain no '_'
# Verified on this cluster on 2026-11-14 before the names were set.
#
# The names deliberately match the ones RE2-OB's traces.csv uses, so Track B's converted traces
# are directly comparable with the public strata: note that the frontend is "frontendservice"
# there, while its Kubernetes deployment is called "frontend".
echo "=== give every SDK its own service.name"
for d in adservice cartservice checkoutservice currencyservice emailservice \
         paymentservice productcatalogservice recommendationservice shippingservice; do
  kubectl set env "deployment/$d" "OTEL_SERVICE_NAME=$d"
done
kubectl set env deployment/frontend OTEL_SERVICE_NAME=frontendservice
kubectl set env deployment/loadgenerator OTEL_SERVICE_NAME=loadgenerator

echo "=== wait for rollouts"
for d in $(kubectl get deploy -o name); do
  kubectl rollout status "$d" --timeout=300s
done
kubectl get pods -o wide
