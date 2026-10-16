#!/usr/bin/env bash
# Recrée le cluster Kind avec Calico (CNI qui applique les NetworkPolicy en entrée ET en sortie).
# À lancer seulement si ./v5-test-egress.sh a échoué.
#
# ATTENTION : l'ancien cluster est SUPPRIMÉ. Sont perdus : les messages en attente dans RabbitMQ
# (volume), l'historique de Prometheus, les releases Helm. Les fichiers du dépôt ne sont pas touchés.
#
# Usage : ./v5-recreate-calico.sh [nom-de-l-ancien-cluster]     (défaut : cloudia-v3)
set -euo pipefail

OLD_CLUSTER="${1:-cloudia-v3}"
NEW_CLUSTER="cloudia-v5"
CALICO_URL="https://raw.githubusercontent.com/projectcalico/calico/v3.28.2/manifests/calico.yaml"

echo "Clusters Kind existants :"; kind get clusters || true
echo
echo "Ce script va supprimer le cluster « $OLD_CLUSTER » puis créer « $NEW_CLUSTER » avec Calico."
printf 'Tape « oui » pour continuer : '
read -r answer
[ "$answer" = "oui" ] || { echo "Annulé."; exit 1; }

if kind get clusters 2>/dev/null | grep -qx "$OLD_CLUSTER"; then kind delete cluster --name "$OLD_CLUSTER"; fi
if kind get clusters 2>/dev/null | grep -qx "$NEW_CLUSTER"; then kind delete cluster --name "$NEW_CLUSTER"; fi

kind create cluster --config kind-config-calico.yaml
kubectl config use-context "kind-$NEW_CLUSTER"

echo "Installation de Calico..."
kubectl apply -f "$CALICO_URL"
kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s
kubectl wait --for=condition=Ready nodes --all --timeout=300s
kubectl get nodes --show-labels | grep -c "workload=" | xargs echo "Nœuds portant le label workload :" || true

echo "Construction de l'image et chargement dans le cluster (elle contient le code TLS de la v5)..."
docker build -t telecom-ia:latest .
kind load docker-image telecom-ia:latest --name "$NEW_CLUSTER"

echo
echo "Cluster « $NEW_CLUSTER » prêt (Calico). Relance maintenant ./v5-test-egress.sh : il doit répondre OK,"
echo "puis suis la procédure de déploiement du README (section 14.6), à partir de l'étape 2."
