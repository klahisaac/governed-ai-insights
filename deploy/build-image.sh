#!/usr/bin/env bash
# Build the image inside the lab cluster with Kaniko (no Docker on this machine) and load it
# into the node's containerd, where the Deployment finds it (imagePullPolicy: IfNotPresent).
# Single-node lab only; a real cluster pushes to a registry instead.
#
#   deploy/build-image.sh 0.1.0
set -euo pipefail
VERSION=${1:?usage: deploy/build-image.sh <version>}
IMAGE=ai-insight-platform:$VERSION
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mktemp -d)
chmod 777 "$OUT"
POD=insight-build-$(date +%s)
NODE=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')

kubectl apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: default
spec:
  nodeName: $NODE
  restartPolicy: Never
  containers:
    - name: kaniko
      image: gcr.io/kaniko-project/executor:v1.23.2
      args:
        - --context=dir:///workspace
        - --dockerfile=/workspace/Dockerfile
        - --destination=$IMAGE
        - --build-arg=VERSION=$VERSION
        - --no-push
        - --tar-path=/out/image.tar
      volumeMounts:
        - { name: context, mountPath: /workspace, readOnly: true }
        - { name: out, mountPath: /out }
  volumes:
    - name: context
      hostPath: { path: $ROOT, type: Directory }
    - name: out
      hostPath: { path: $OUT, type: Directory }
YAML

kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n default --timeout=15m \
  || { kubectl logs -n default "$POD" | tail -30; kubectl delete pod -n default "$POD" --wait=false; exit 1; }
kubectl delete pod -n default "$POD" --wait=false
sudo ctr -n k8s.io images import "$OUT/image.tar"
sudo rm -rf "$OUT"
sudo ctr -n k8s.io images ls -q | grep "ai-insight-platform:$VERSION"
