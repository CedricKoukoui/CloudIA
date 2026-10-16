#!/usr/bin/env bash
# Crée les certificats TLS et les Secrets Kubernetes de la v5 (Q3 : TLS mutuel, Q14 : mots de passe hors des manifestes).
#
# À lancer APRÈS 01-namespaces-quotas.yaml (les namespaces doivent exister) et AVANT le déploiement des
# manifestes et du « helm upgrade » (le Secret de Grafana doit exister).
#
# Ce que le script crée :
#   - une autorité de certification (CA) auto-signée du projet, un certificat pour RabbitMQ (serveur),
#     un pour l'IA et un pour le générateur (clients) ;
#   - namespace ingestion : rabbitmq-credentials, rabbitmq-tls, generator-tls
#   - namespace ia        : rabbitmq-credentials, ia-tls
#   - namespace default   : grafana-admin
#
# Les clés privées restent dans $TLS_DIR (défaut ~/.cloudia-v5-tls), HORS du dépôt : elles ne doivent
# jamais être commitées. Le script peut être relancé : il réutilise les certificats et les mots de
# passe déjà créés. Pour imposer un mot de passe : RABBITMQ_PASSWORD=... GRAFANA_PASSWORD=... ./v5-secrets.sh
set -euo pipefail

TLS_DIR="${TLS_DIR:-$HOME/.cloudia-v5-tls}"
RMQ_HOST="telecom-broker-rabbitmq.ingestion.svc.cluster.local"
RMQ_USER="${RABBITMQ_USER:-user}"

secret_value() { # secret_value <namespace> <secret> <clé> : valeur décodée, vide si le Secret n'existe pas
  kubectl get secret -n "$1" "$2" -o "jsonpath={.data.$3}" 2>/dev/null | base64 --decode 2>/dev/null || true
}
apply_secret() { # apply_secret <arguments de « kubectl create secret ... »>
  kubectl create secret "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

for ns in ia ingestion default; do
  kubectl get namespace "$ns" >/dev/null 2>&1 || { echo "Namespace « $ns » absent : applique d'abord 01-namespaces-quotas.yaml."; exit 1; }
done

# ---------- Mots de passe : fournis, sinon déjà présents dans le cluster, sinon générés ----------
RMQ_PASS="${RABBITMQ_PASSWORD:-$(secret_value ingestion rabbitmq-credentials password)}"
RMQ_PASS="${RMQ_PASS:-$(openssl rand -hex 16)}"
GRAFANA_PASS="${GRAFANA_PASSWORD:-$(secret_value default grafana-admin admin-password)}"
GRAFANA_PASS="${GRAFANA_PASS:-$(openssl rand -hex 12)}"

# ---------- Certificats (créés une seule fois) ----------
umask 077
mkdir -p "$TLS_DIR"
cd "$TLS_DIR"
if [ ! -f ca.crt ]; then
  echo "Création de l'autorité de certification et des certificats dans $TLS_DIR ..."
  cat > ca.cnf <<CNF
[req]
distinguished_name=dn
x509_extensions=v3
prompt=no
[dn]
CN=cloudia-ca
[v3]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
CNF
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 3650 -config ca.cnf 2>/dev/null

  issue() { # issue <nom> <serverAuth|clientAuth> <extension SAN éventuelle>
    cat > "$1.ext" <<EXT
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=$2
authorityKeyIdentifier=keyid
$3
EXT
    openssl req -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=$1" 2>/dev/null
    openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$1.crt" -days 825 -extfile "$1.ext" 2>/dev/null
    rm -f "$1.csr" "$1.ext"
  }
  issue rabbitmq serverAuth "subjectAltName=DNS:telecom-broker-rabbitmq,DNS:telecom-broker-rabbitmq.ingestion,DNS:telecom-broker-rabbitmq.ingestion.svc,DNS:$RMQ_HOST,DNS:localhost"
  issue ia clientAuth ""
  issue generator clientAuth ""
  rm -f ca.cnf ca.srl
fi
cd - >/dev/null

# ---------- Secrets ----------
apply_secret generic rabbitmq-credentials -n ingestion --from-literal=username="$RMQ_USER" --from-literal=password="$RMQ_PASS"
apply_secret generic rabbitmq-credentials -n ia        --from-literal=username="$RMQ_USER" --from-literal=password="$RMQ_PASS"
apply_secret generic rabbitmq-tls -n ingestion --from-file=ca.crt="$TLS_DIR/ca.crt" --from-file=tls.crt="$TLS_DIR/rabbitmq.crt" --from-file=tls.key="$TLS_DIR/rabbitmq.key"
apply_secret generic generator-tls -n ingestion --from-file=ca.crt="$TLS_DIR/ca.crt" --from-file=tls.crt="$TLS_DIR/generator.crt" --from-file=tls.key="$TLS_DIR/generator.key"
apply_secret generic ia-tls -n ia              --from-file=ca.crt="$TLS_DIR/ca.crt" --from-file=tls.crt="$TLS_DIR/ia.crt" --from-file=tls.key="$TLS_DIR/ia.key"
apply_secret generic grafana-admin -n default  --from-literal=admin-user=admin --from-literal=admin-password="$GRAFANA_PASS"
echo "Secrets créés ou mis à jour."

# ---------- Compte RabbitMQ déjà existant (volume de la v3) : changer son mot de passe dans le broker ----------
# RabbitMQ ne lit RABBITMQ_DEFAULT_PASS qu'à la création du compte : sur un volume existant, le compte
# garde l'ancien mot de passe (« password » en v3) tant qu'on ne le change pas.
POD=telecom-broker-rabbitmq-0
if [ "$(kubectl get pod -n ingestion "$POD" -o 'jsonpath={.status.phase}' 2>/dev/null || true)" = "Running" ]; then
  if kubectl exec -n ingestion "$POD" -c rabbitmq -- rabbitmqctl change_password "$RMQ_USER" "$RMQ_PASS" >/dev/null 2>&1; then
    echo "Mot de passe du compte « $RMQ_USER » mis à jour dans le broker."
  else
    echo "ATTENTION : mot de passe non changé dans le broker. À faire à la main, une fois le pod Ready :"
    echo "  kubectl exec -n ingestion $POD -c rabbitmq -- rabbitmqctl change_password $RMQ_USER \"\$(kubectl get secret -n ingestion rabbitmq-credentials -o jsonpath='{.data.password}' | base64 --decode)\""
  fi
else
  echo "Broker pas encore démarré : le compte sera créé avec ce mot de passe au premier démarrage du volume."
  echo "(Si le volume existe déjà, relance ce script une fois le broker en marche.)"
fi

echo
echo "Grafana : utilisateur admin / mot de passe $GRAFANA_PASS"
echo "RabbitMQ (console 15672 via port-forward) : utilisateur $RMQ_USER, mot de passe :"
echo "  kubectl get secret -n ingestion rabbitmq-credentials -o jsonpath='{.data.password}' | base64 --decode; echo"
echo "Certificats et clés privées : $TLS_DIR (à ne JAMAIS commiter)."
