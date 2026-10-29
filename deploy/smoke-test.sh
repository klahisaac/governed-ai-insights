#!/usr/bin/env bash
# Smoke test for a cluster running deploy/overlays/<env> on the monitoring platform:
# the app answers and writes a validated summary, Prometheus scrapes it and loaded its
# alert rules, Jaeger received its traces, and Grafana has its dashboard and data source.
#
#   deploy/smoke-test.sh                          (needs kubectl pointed at the cluster)
#   SMOKE_WAREHOUSE=databricks deploy/smoke-test.sh  (also checks which warehouse the app reads)
set -euo pipefail
TOKEN=${SMOKE_TOKEN:-prototype-brand-lead}
PIDS=()
trap 'kill "${PIDS[@]}" 2>/dev/null || true' EXIT

forward() {  # namespace service local-port remote-port
  kubectl port-forward -n "$1" "svc/$2" "$3:$4" >/dev/null 2>&1 &
  PIDS+=($!)
  for _ in $(seq 30); do curl -s -o /dev/null "http://localhost:$3" && return; sleep 1; done
  echo "port-forward to $1/$2 did not come up"; exit 1
}

retry() {  # description seconds command...
  local description=$1 seconds=$2; shift 2
  for _ in $(seq $((seconds / 5))); do "$@" && { echo "ok: $description"; return; }; sleep 5; done
  echo "FAILED: $description"; exit 1
}

forward insight insight-platform 18000 80
forward monitoring kps-prometheus 19090 9090
forward observability jaeger 16686 16686
forward monitoring kube-prometheus-stack-grafana 13000 80

curl -sf http://localhost:18000/healthz >/dev/null && echo "ok: app is healthy"

if [ -n "${SMOKE_WAREHOUSE:-}" ]; then  # for example databricks on AKS, where the app reads the real warehouse
  curl -sf -H "Authorization: Bearer $TOKEN" http://localhost:18000/api/catalog \
    | python3 -c "import json, sys; warehouse = json.load(sys.stdin)['warehouse']; \
assert warehouse == '$SMOKE_WAREHOUSE', warehouse; print('ok: the app reads from $SMOKE_WAREHOUSE')"
fi

# A summary from the rule-based writer must pass the validator (no LLM in CI).
for metric in new_to_brand_patients eight_week_persistence; do
  curl -sf -H "Authorization: Bearer $TOKEN" "http://localhost:18000/api/metrics/$metric?narrator=template" \
    | python3 -c "import json, sys; insight = json.load(sys.stdin)['insight']; \
assert insight['status'] == 'accepted', insight['violations']; print('ok: $metric summary accepted')"
done

app_scraped() {
  curl -sf http://localhost:19090/api/v1/query --data-urlencode 'query=up{namespace="insight",job="insight-platform"} == 1' \
    | python3 -c "import json, sys; sys.exit(0 if json.load(sys.stdin)['data']['result'] else 1)"
}
rules_loaded() {
  curl -sf http://localhost:19090/api/v1/rules | grep -q '"InsightPlatformDown"'
}
traces_arrived() {
  curl -sf http://localhost:16686/api/services | grep -q '"insight-platform"'
}
GRAFANA_PASSWORD=$(kubectl get secret -n monitoring kube-prometheus-stack-grafana -o jsonpath='{.data.admin-password}' | base64 -d)
dashboard_loaded() {
  curl -sf -u "admin:$GRAFANA_PASSWORD" http://localhost:13000/api/dashboards/uid/insight-platform >/dev/null
}
jaeger_datasource() {
  curl -sf -u "admin:$GRAFANA_PASSWORD" http://localhost:13000/api/datasources/uid/jaeger >/dev/null
}

retry "Prometheus scrapes the app" 180 app_scraped
retry "Prometheus loaded the alert rules" 120 rules_loaded
retry "Jaeger received traces" 120 traces_arrived
retry "Grafana loaded the dashboard" 120 dashboard_loaded
retry "Grafana has the Jaeger data source" 120 jaeger_datasource
