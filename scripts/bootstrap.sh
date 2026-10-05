#!/usr/bin/env bash
# One-time cluster setup: kind cluster + image + Argo CD + the Argo CD Application.
# Usage:  ./scripts/bootstrap.sh https://github.com/<you>/fraud-gitops-lab.git
# Prerequisite: this repo is pushed to that URL on branch main (Argo CD pulls from GitHub, not your disk).
set -euo pipefail
REPO_URL="${1:?usage: bootstrap.sh <git repo url>}"
CLUSTER=gitops-lab
IMAGE=fraud-service:1.0
cd "$(dirname "$0")/.."

kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER"
kubectl config use-context "kind-$CLUSTER"

docker build -t "$IMAGE" .
kind load docker-image "$IMAGE" --name "$CLUSTER"

kubectl get ns argocd >/dev/null 2>&1 || kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=300s

sed "s#REPLACE_WITH_YOUR_REPO_URL#${REPO_URL}#" argocd/application.yaml | kubectl apply -f -

echo
echo "Argo CD admin password:"
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
echo
echo "UI:       kubectl -n argocd port-forward svc/argocd-server 8080:443   ->  https://localhost:8080  (user: admin)"
echo "Service:  kubectl -n fraud port-forward svc/fraud-service 9000:80"
echo "Watch:    kubectl -n argocd get application fraud-service -w"
