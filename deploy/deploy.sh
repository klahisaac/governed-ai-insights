#!/usr/bin/env bash
# Install the monitoring platform and the app on the cluster kubectl points at. The same script
# runs in pull request CI (kind) and for staging and production (AKS), so what CI checks is what
# the environments get: the same charts at the versions in deploy/platform/charts.env, and the
# same base manifests through the environment's overlay.
#
#   deploy/deploy.sh <overlay> <image>
#   deploy/deploy.sh ci ai-insight-platform:ci
#   deploy/deploy.sh staging acrgovernedaiinsights.azurecr.io/ai-insight-platform@sha256:...
set -euo pipefail
OVERLAY=${1:?usage: deploy/deploy.sh <overlay> <image>}
IMAGE=${2:?usage: deploy/deploy.sh <overlay> <image>}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
source "$ROOT/deploy/platform/charts.env"

values=(-f "$ROOT/deploy/platform/kube-prometheus-stack.values.yaml")
[ -f "$ROOT/deploy/platform/$OVERLAY/kube-prometheus-stack.values.yaml" ] \
  && values+=(-f "$ROOT/deploy/platform/$OVERLAY/kube-prometheus-stack.values.yaml")

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add jaegertracing https://jaegertracing.github.io/helm-charts >/dev/null
helm repo update >/dev/null
helm upgrade --install jaeger jaegertracing/jaeger --version "$JAEGER_CHART_VERSION" \
  -n observability --create-namespace --wait --timeout 5m
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --version "$KUBE_PROMETHEUS_STACK_VERSION" -n monitoring --create-namespace "${values[@]}" \
  --wait --timeout 10m
kubectl apply --server-side -f "$ROOT/deploy/platform/grafana-datasource-jaeger.yaml"

# The overlay names the image repository; the deploy decides the exact image (a digest on AKS).
kubectl kustomize "$ROOT/deploy/overlays/$OVERLAY" \
  | sed -E "s#image: [^ ]*ai-insight-platform[:@][^ ]+#image: $IMAGE#" \
  | kubectl apply --server-side -f -
kubectl rollout status deployment/insight-platform -n insight --timeout 5m
