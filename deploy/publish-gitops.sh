#!/usr/bin/env bash
# Copy deploy/k8s into the GitOps repo in the lab Gitea; Argo CD syncs it from there.
# The app repo stays the source; the GitOps repo only ever holds rendered-from-here config.
#
#   deploy/publish-gitops.sh "bump image to 0.1.1"
set -euo pipefail
MESSAGE=${1:-"Update insight-platform config"}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
GITEA=$(kubectl get pod -n gitea -l app.kubernetes.io/name=gitea -o jsonpath='{.items[0].status.podIP}')
PASSWORD=$(kubectl get deploy -n gitea gitea -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="configure-gitea")].env[?(@.name=="GITEA_ADMIN_PASSWORD")].value}')
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

git clone -q "http://gitea_admin:$PASSWORD@$GITEA:3000/gitea_admin/insight-gitops.git" "$WORK/repo"
cd "$WORK/repo"
git checkout -q -B main
rm -rf apps/insight-platform && mkdir -p apps/insight-platform
cp "$ROOT"/deploy/k8s/* apps/insight-platform/
git add -A
if git diff --cached --quiet; then
  echo "nothing to publish"
  exit 0
fi
git -c user.name="$(git -C "$ROOT" config user.name)" -c user.email="$(git -C "$ROOT" config user.email)" \
  commit -q -m "$MESSAGE"
git push -q origin main
git log --oneline -1
