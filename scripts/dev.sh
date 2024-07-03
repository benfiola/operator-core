#!/bin/sh 
set -e

for command in minikube; do
    if ! command -v "${command}" > /dev/null; then
        2>&1 echo "error: ${command} not installed"
        exit 1
    fi
done

if [ ! -d "/workspaces/operator-core" ]; then
  2>&1 echo "error: must be run from devcontainer"
  exit 1
fi

echo "delete minikube cluster if exists"
minikube delete || true

echo "create minikube cluster"
minikube start --force

echo "deploy test custom resources"
kubectl apply -f ./manifests/crds.yaml

echo "deploy example resources"
kubectl apply -f ./manifests/example-resources.yaml