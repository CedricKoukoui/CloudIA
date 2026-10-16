#!/usr/bin/env bash
# Étape 0 de la v5 : le CNI du cluster applique-t-il le filtrage SORTANT (egress) ?
#
# Une NetworkPolicy n'est qu'une déclaration : c'est le plugin réseau (CNI) qui bloque les paquets.
# Ce test crée deux pods jetables, interdit toute sortie au client, et vérifie que la connexion
# vers l'autre pod échoue réellement. Il ne touche à aucune ressource du projet.
#
# Usage : ./v5-test-egress.sh        (code de sortie 0 = l'egress est appliqué, 1 = il ne l'est pas)
set -u

NS=np-probe
cleanup() { kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1; }
trap cleanup EXIT

kubectl delete namespace "$NS" --ignore-not-found --wait=true >/dev/null 2>&1
kubectl create namespace "$NS" >/dev/null || exit 2

echo "Création d'un serveur et d'un client jetables..."
kubectl -n "$NS" run server --image=busybox:1.36 --labels=role=server --restart=Never -- \
  sh -c 'mkdir -p /www && echo ok > /www/index.html && httpd -f -p 8080 -h /www' >/dev/null
kubectl -n "$NS" run client --image=busybox:1.36 --labels=role=client --restart=Never -- sleep 600 >/dev/null
kubectl -n "$NS" wait --for=condition=Ready pod/server pod/client --timeout=120s >/dev/null || {
  echo "Les pods de test ne démarrent pas (image busybox non téléchargeable ?)."; exit 2; }

SERVER_IP=$(kubectl -n "$NS" get pod server -o jsonpath='{.status.podIP}')
probe() { kubectl -n "$NS" exec client -- wget -q -T 3 -O- "http://$SERVER_IP:8080" 2>/dev/null; }

if [ "$(probe)" != "ok" ]; then
  echo "Test impossible : le client n'atteint pas le serveur même sans règle."; exit 2
fi
echo "1. Sans règle : le client joint le serveur ........ OK"

kubectl -n "$NS" apply -f - >/dev/null <<POL
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all-egress-client
spec:
  podSelector:
    matchLabels:
      role: client
  policyTypes:
  - Egress
POL

blocked=no
for i in 1 2 3 4 5 6 7 8; do
  sleep 3
  if [ "$(probe)" != "ok" ]; then blocked=yes; break; fi
done

if [ "$blocked" = "yes" ]; then
  echo "2. Règle « aucune sortie » : la connexion est bloquée ..... OK"
  echo
  echo "RÉSULTAT : le CNI applique le filtrage sortant. Tu peux continuer avec la v5 sur ce cluster."
  exit 0
fi
echo "2. Règle « aucune sortie » : la connexion PASSE encore .... ÉCHEC"
echo
echo "RÉSULTAT : le CNI de ce cluster n'applique PAS le filtrage sortant."
echo "Prochaine étape (option A choisie) : ./v5-recreate-calico.sh  (recrée le cluster avec Calico)."
exit 1
