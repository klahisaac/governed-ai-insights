#!/usr/bin/env bash
# Azure resources for the staging and production environments. Safe to run again.
#
#   container registry   one, shared: staging pushes, production pulls the same digest
#   AKS clusters         one per environment, Entra sign-in with Azure RBAC, no local accounts,
#                        OIDC issuer and Workload Identity on
#   app identities       one user-assigned managed identity per environment, federated with that
#                        cluster's service account insight/insight-platform, allowed to call the
#                        Foundry deployment (Databricks grants follow)
#   deploy identities    one per environment for GitHub Actions, federated with the repository's
#                        GitHub environment of the same name (no secrets)
#   app routing          production only: the managed NGINX ingress (one replica), whose public
#                        IP deploy/cloudflare points insight.isaacon.dev at
#
# The GitHub subjects use the repository's immutable ids (GitHub issues them that way), so a
# deleted and recreated repository needs this script run again.
#
#   deploy/azure/provision.sh
set -euo pipefail

LOCATION=${LOCATION:-eastus}
GROUP=${GROUP:-rg-governed-ai-insights}
REGISTRY=${REGISTRY:-acrgovernedaiinsights}
NODE_SIZE=${NODE_SIZE:-Standard_D2s_v4}  # 2 vCPU, 8 GiB; v5 is not offered to this subscription in eastus
REPOSITORY=${REPOSITORY:-klahisaac/governed-ai-insights}
FOUNDRY_ID=${FOUNDRY_ID:-$(az cognitiveservices account show -g rg-insight-demo -n foundry-insight-demo --query id -o tsv)}
ENVIRONMENTS=(staging production)

OWNER_ID=$(gh api "repos/$REPOSITORY" --jq .owner.id)
REPOSITORY_ID=$(gh api "repos/$REPOSITORY" --jq .id)
GITHUB_SUBJECT_PREFIX="repo:${REPOSITORY%%/*}@$OWNER_ID/${REPOSITORY##*/}@$REPOSITORY_ID"
ME=$(az ad signed-in-user show --query id -o tsv)

az provider register -n Microsoft.ContainerRegistry --wait
az group create -n "$GROUP" -l "$LOCATION" -o none
az acr create -g "$GROUP" -n "$REGISTRY" --sku Basic --admin-enabled false -o none
REGISTRY_ID=$(az acr show -g "$GROUP" -n "$REGISTRY" --query id -o tsv)

federate() {  # identity-name credential-name issuer subject
  if ! az identity federated-credential show -g "$GROUP" --identity-name "$1" -n "$2" -o none 2>/dev/null; then
    az identity federated-credential create -g "$GROUP" --identity-name "$1" -n "$2" \
      --issuer "$3" --subject "$4" --audiences api://AzureADTokenExchange -o none
  fi
}

grant() {  # principal-id role scope
  az role assignment create --assignee-object-id "$1" --assignee-principal-type "${4:-ServicePrincipal}" \
    --role "$2" --scope "$3" -o none
}

for environment in "${ENVIRONMENTS[@]}"; do
  cluster=aks-governed-ai-insights-$environment
  if ! az aks show -g "$GROUP" -n "$cluster" -o none 2>/dev/null; then
    # One small node per cluster: the subscription's regional quota is 4 vCPUs.
    az aks create -g "$GROUP" -n "$cluster" -l "$LOCATION" --tier free \
      --node-count 1 --node-vm-size "$NODE_SIZE" \
      --network-plugin azure --network-plugin-mode overlay \
      --enable-aad --enable-azure-rbac --disable-local-accounts \
      --enable-oidc-issuer --enable-workload-identity \
      --attach-acr "$REGISTRY_ID" --auto-upgrade-channel patch \
      --no-ssh-key --tags "environment=$environment" -o none
  fi
  if [ "$environment" = production ] &&
     [ "$(az aks show -g "$GROUP" -n "$cluster" --query ingressProfile.webAppRouting.enabled -o tsv)" != true ]; then
    az aks approuting enable -g "$GROUP" -n "$cluster" -o none
  fi
  cluster_id=$(az aks show -g "$GROUP" -n "$cluster" --query id -o tsv)
  issuer=$(az aks show -g "$GROUP" -n "$cluster" --query oidcIssuerProfile.issuerUrl -o tsv)
  grant "$ME" "Azure Kubernetes Service RBAC Cluster Admin" "$cluster_id" User
  if [ "$environment" = production ]; then
    # The controller asks 500m a pod and the one node has room for one, not the default two.
    az aks get-credentials -g "$GROUP" -n "$cluster" --overwrite-existing -o none
    kubelogin convert-kubeconfig -l azurecli
    kubectl patch nginxingresscontroller default --type merge -p '{"spec":{"scaling":{"minReplicas":1}}}'
  fi

  app=id-governed-ai-insights-app-$environment
  az identity create -g "$GROUP" -n "$app" -l "$LOCATION" -o none
  federate "$app" aks-service-account "$issuer" system:serviceaccount:insight:insight-platform
  grant "$(az identity show -g "$GROUP" -n "$app" --query principalId -o tsv)" "Cognitive Services OpenAI User" "$FOUNDRY_ID"

  deployer=id-governed-ai-insights-deploy-$environment
  az identity create -g "$GROUP" -n "$deployer" -l "$LOCATION" -o none
  federate "$deployer" github-environment "https://token.actions.githubusercontent.com" \
    "$GITHUB_SUBJECT_PREFIX:environment:$environment"
  deployer_principal=$(az identity show -g "$GROUP" -n "$deployer" --query principalId -o tsv)
  grant "$deployer_principal" "Azure Kubernetes Service Cluster User Role" "$cluster_id"
  # The deploy installs the monitoring charts too, which bring cluster-wide CRDs and RBAC.
  grant "$deployer_principal" "Azure Kubernetes Service RBAC Cluster Admin" "$cluster_id"
  if [ "$environment" = staging ]; then
    grant "$deployer_principal" AcrPush "$REGISTRY_ID"   # staging builds and pushes the image
  else
    grant "$deployer_principal" AcrPull "$REGISTRY_ID"   # production only checks the digest exists
  fi
done

echo
for environment in "${ENVIRONMENTS[@]}"; do
  printf '%-10s app identity %s, deploy identity %s\n' "$environment" \
    "$(az identity show -g "$GROUP" -n "id-governed-ai-insights-app-$environment" --query clientId -o tsv)" \
    "$(az identity show -g "$GROUP" -n "id-governed-ai-insights-deploy-$environment" --query clientId -o tsv)"
done
