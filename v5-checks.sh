#!/usr/bin/env bash
# Contrôles de la v5 sur le cluster. Chaque contrôle affiche OK ou ÉCHEC ; code de sortie 1 s'il y a un échec.
#
#   ./v5-checks.sh preflight    avant d'appliquer 04-network-policy-observability.yaml : les labels visés existent-ils ?
#   ./v5-checks.sh              tous les contrôles (secrets, TLS, réseau, RBAC, alertes) ; l'attaque simulée
#                               (section Q14) s'exécute si 07-tenant-simulation.yaml est appliqué
#   SCALE_TEST=1 ./v5-checks.sh ajoute le test « mise de l'IA à 0 réplique » (≈ 2 min) : l'alerte doit se lever
#   CI_MODE=1 ./v5-checks.sh    pour la CI (cluster Kind sans pile d'observabilité) : les contrôles qui
#                               demandent Prometheus ou Grafana sont ignorés au lieu d'échouer
set -u

PASS=0; FAIL=0
ok() { echo "  OK     $1"; PASS=$((PASS + 1)); }
ko() { echo "  ÉCHEC  $1"; FAIL=$((FAIL + 1)); }
section() { echo; echo "== $1"; }
summary() {
  echo; echo "Bilan : $PASS contrôle(s) réussi(s), $FAIL échec(s)."
  [ "$FAIL" -eq 0 ]
}

count_pods() { kubectl get pods -n "$1" -l "$2" --no-headers 2>/dev/null | wc -l | tr -d ' '; }
first_pod() { kubectl get pods -n "$1" -l "$2" -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null; }
pod_ip() { kubectl get pod -n "$1" "$2" -o 'jsonpath={.status.podIP}' 2>/dev/null; }

# ---------------------------------------------------------------- preflight
if [ "${1:-}" = "preflight" ]; then
  section "Avant 04-network-policy-observability.yaml : les pods visés portent-ils les labels attendus ?"
  for sel in app.kubernetes.io/name=prometheus app.kubernetes.io/name=alertmanager app.kubernetes.io/name=grafana; do
    n=$(count_pods default "$sel")
    if [ "$n" -ge 1 ]; then ok "namespace default : $n pod(s) avec $sel"; else ko "namespace default : aucun pod avec $sel (adapter 04-network-policy-observability.yaml)"; fi
  done
  n=$(count_pods kube-system k8s-app=kube-dns)
  if [ "$n" -ge 1 ]; then ok "kube-system : $n pod(s) DNS avec k8s-app=kube-dns (règles d'egress DNS)"; else ko "kube-system : aucun pod avec k8s-app=kube-dns (adapter 02-network-policy.yaml)"; fi
  w=$(kubectl get nodes -l 'workload in (ingestion,compute)' --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "$w" -ge 2 ]; then ok "2 workers portent le label workload"; else ko "labels workload manquants sur les nœuds"; fi
  summary; exit $?
fi

GEN=$(first_pod ingestion app=telecom-generator)
IA=$(first_pod ia app=telecom-ia)
PROM=$(first_pod default app.kubernetes.io/name=prometheus)
GRAF=$(first_pod default app.kubernetes.io/name=grafana)

# Interroger Prometheus depuis ce poste par « port-forward » : l'image de Prometheus n'a ni wget ni curl.
# Le port-forward ne passe pas par les NetworkPolicy ; il est lancé une fois et arrêté à la fin du script.
PF_PID=""; PF_PORT=""
prom_start() {
  PF_PORT=$((19000 + RANDOM % 900))
  kubectl port-forward -n default "$PROM" "$PF_PORT:9090" >/dev/null 2>&1 &
  PF_PID=$!
  trap 'kill "$PF_PID" 2>/dev/null' EXIT
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$PF_PORT/-/ready" && return 0
    sleep 1
  done
  return 1
}
prom_get() { curl -s -m 10 "http://127.0.0.1:$PF_PORT$1"; } # prom_get <chemin et requête>
if [ -n "$PROM" ]; then prom_start || echo "  (attention : port-forward vers Prometheus indisponible)"; fi

# Connexion TCP depuis un pod : « connected » si elle aboutit, « blocked » sinon (délai de 4 s)
TCP_PY='import socket,sys
s=socket.socket(); s.settimeout(4)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); print("connected")
except Exception:
    print("blocked")'
tcp() { # tcp <namespace> <pod> <hôte> <port>
  kubectl exec -n "$1" "$2" -- python -c "$TCP_PY" "$3" "$4" 2>/dev/null | tail -1
}
expect_tcp() { # expect_tcp <connected|blocked> <description> <namespace> <pod> <hôte> <port>
  local want="$1" desc="$2" got
  got=$(tcp "$3" "$4" "$5" "$6")
  if [ "$got" = "$want" ]; then ok "$desc"; else ko "$desc (obtenu : ${got:-aucune réponse})"; fi
}

# ---------------------------------------------------------------- secrets
section "Secrets : plus de mot de passe en clair dans les manifestes déployés"
for item in "ia:rabbitmq-credentials" "ia:ia-tls" "ingestion:rabbitmq-credentials" "ingestion:rabbitmq-tls" "ingestion:generator-tls" "default:grafana-admin"; do
  ns=${item%%:*}; name=${item##*:}
  if kubectl get secret -n "$ns" "$name" >/dev/null 2>&1; then ok "Secret $ns/$name présent"; else ko "Secret $ns/$name absent (lancer v5-secrets.sh)"; fi
done
for res in "ia deployment/telecom-ia-deployment" "ingestion deployment/telecom-generator-deployment" "ingestion statefulset/telecom-broker-rabbitmq"; do
  ns=${res%% *}; obj=${res##* }
  if kubectl get -n "$ns" "$obj" -o yaml 2>/dev/null | grep -qE 'user:password|value: "?password"?$'; then ko "$ns/$obj contient encore un mot de passe en clair"; else ok "$ns/$obj : aucun mot de passe en clair"; fi
done

# ---------------------------------------------------------------- pods
section "Pods : jeton d'API absent, processus non-root"
for pair in "ia:$IA" "ingestion:$GEN"; do
  ns=${pair%%:*}; pod=${pair##*:}
  if [ -z "$pod" ]; then ko "pod de $ns introuvable"; continue; fi
  if kubectl exec -n "$ns" "$pod" -- ls /var/run/secrets/kubernetes.io/serviceaccount >/dev/null 2>&1; then
    ko "$ns/$pod : un jeton de compte de service est monté"
  else
    ok "$ns/$pod : aucun jeton de compte de service monté"
  fi
  uid=$(kubectl exec -n "$ns" "$pod" -- id -u 2>/dev/null | tr -d '\r')
  if [ -n "$uid" ] && [ "$uid" != "0" ]; then ok "$ns/$pod : processus lancé avec l'uid $uid (pas root)"; else ko "$ns/$pod : processus root ou uid illisible"; fi
done

# Pod Security Standards : le namespace refuse les pods privilégiés (Q14 : restreindre les privilèges des pods)
for ns in ia ingestion; do
  lvl=$(kubectl get namespace "$ns" -o 'jsonpath={.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)
  if [ "$lvl" = "restricted" ]; then ok "namespace $ns : Pod Security « restricted » appliqué"; else ko "namespace $ns : Pod Security non appliqué (kubectl label namespace $ns pod-security.kubernetes.io/enforce=restricted --overwrite)"; fi
  out=$(kubectl run psa-test -n "$ns" --image=busybox:1.36 --restart=Never --dry-run=server \
    --overrides='{"spec":{"containers":[{"name":"psa-test","image":"busybox:1.36","securityContext":{"privileged":true}}]}}' -- sleep 1 2>&1 || true)
  if echo "$out" | grep -q "PodSecurity"; then ok "namespace $ns : un pod privilégié est refusé"; else ko "namespace $ns : un pod privilégié n'est pas refusé"; fi
done

# ---------------------------------------------------------------- TLS (Q3)
section "Q3 : AMQP chiffré, TLS mutuel"
RMQ=telecom-broker-rabbitmq.ingestion.svc.cluster.local
TLS_PY='import socket,ssl,sys
host=sys.argv[1]; with_cert=sys.argv[2]=="cert"
ctx=ssl.create_default_context(cafile="/etc/amqp-tls/ca.crt")
if with_cert:
    ctx.load_cert_chain("/etc/amqp-tls/tls.crt","/etc/amqp-tls/tls.key")
try:
    s=ctx.wrap_socket(socket.create_connection((host,5671),5),server_hostname=host)
    s.sendall(b"AMQP\x00\x00\x09\x01")
    print("accepted" if s.recv(16) else "refused")
except Exception:
    print("refused")'
if [ -n "$IA" ]; then
  r=$(kubectl exec -n ia "$IA" -- python -c "$TLS_PY" "$RMQ" cert 2>/dev/null | tail -1)
  if [ "$r" = "accepted" ]; then ok "avec certificat client : le broker répond"; else ko "avec certificat client : connexion refusée (obtenu : ${r:-rien})"; fi
  r=$(kubectl exec -n ia "$IA" -- python -c "$TLS_PY" "$RMQ" nocert 2>/dev/null | tail -1)
  if [ "$r" = "refused" ]; then ok "sans certificat client : le broker refuse (TLS mutuel exigé)"; else ko "sans certificat client : le broker accepte (obtenu : ${r:-rien})"; fi
  if kubectl logs -n ia "$IA" --tail=30 2>/dev/null | grep -qE 'Normal|ANOMALIE'; then ok "l'IA traite des messages à travers le canal chiffré"; else ko "aucun message traité récemment dans les journaux de l'IA"; fi
else
  ko "pod de l'IA introuvable"
fi

# ---------------------------------------------------------------- réseau (Q3, Q9)
section "Q3 / Q9 : isolation réseau"
if [ -n "$GEN" ] && [ -n "$IA" ]; then
  IA_IP=$(pod_ip ia "$IA"); PROM_IP=""; GRAF_IP=""
  if [ -n "$PROM" ]; then PROM_IP=$(pod_ip default "$PROM"); fi
  if [ -n "$GRAF" ]; then GRAF_IP=$(pod_ip default "$GRAF"); fi
  API_IP=$(kubectl get svc kubernetes -n default -o 'jsonpath={.spec.clusterIP}' 2>/dev/null)
  expect_tcp connected "générateur → RabbitMQ:5671 autorisé" ingestion "$GEN" "$RMQ" 5671
  expect_tcp blocked   "générateur → RabbitMQ:5672 (clair) bloqué" ingestion "$GEN" "$RMQ" 5672
  expect_tcp blocked   "générateur → IA:8000 bloqué (Q9)" ingestion "$GEN" "$IA_IP" 8000
  if [ -n "$PROM_IP" ] && [ -n "$GRAF_IP" ]; then
    expect_tcp blocked "générateur → Prometheus:9090 bloqué (Q9 : saturer Prometheus)" ingestion "$GEN" "$PROM_IP" 9090
    expect_tcp blocked "générateur → Grafana:3000 bloqué" ingestion "$GEN" "$GRAF_IP" 3000
  elif [ "${CI_MODE:-0}" = "1" ]; then
    echo "  (ignoré : pile d'observabilité absente de ce cluster)"
  else
    ko "pods Prometheus ou Grafana introuvables"
  fi
  expect_tcp blocked   "générateur → API Kubernetes:443 bloqué (egress, Q3)" ingestion "$GEN" "$API_IP" 443
  expect_tcp connected "IA → RabbitMQ:5671 autorisé" ia "$IA" "$RMQ" 5671
  expect_tcp blocked   "IA → API Kubernetes:443 bloqué (egress)" ia "$IA" "$API_IP" 443
  if [ -n "$PROM_IP" ]; then expect_tcp blocked "IA → Prometheus:9090 bloqué" ia "$IA" "$PROM_IP" 9090; fi
else
  ko "pods générateur ou IA introuvables"
fi
if [ -n "$PROM" ]; then
  q=$(prom_get '/api/v1/query?query=up%7Bnamespace%3D%22ia%22%7D')
  if echo "$q" | grep -q '"1"\]'; then ok "Prometheus collecte l'IA (up{namespace=\"ia\"} = 1) : seule source autorisée"; else ko "Prometheus ne collecte plus l'IA : $q"; fi
elif [ "${CI_MODE:-0}" = "1" ]; then
  echo "  (ignoré : pile d'observabilité absente de ce cluster)"
else
  ko "pod Prometheus introuvable"
fi

# ---------------------------------------------------------------- RBAC (Q14)
section "Q14 : RBAC (moindre privilège)"
can() { # can <yes|no> <description> <arguments de « kubectl auth can-i »>
  local want="$1" desc="$2" got; shift 2
  got=$(kubectl auth can-i "$@" 2>/dev/null)
  if [ "$got" = "$want" ]; then ok "$desc"; else ko "$desc (obtenu : ${got:-rien})"; fi
}
VIEWER="--as=lecteur --as-group=cloudia:viewers"
OPERATOR="--as=exploitant --as-group=cloudia:operators"
can yes "lecteur : voit les pods de ia"                         get pods -n ia $VIEWER
can no  "lecteur : ne peut pas modifier l'échelle de l'IA"      update deployments/scale -n ia $VIEWER
can no  "lecteur : ne lit aucun secret"                         get secrets -n ia $VIEWER
can yes "exploitant : peut modifier l'échelle de l'IA"          update deployments/scale -n ia $OPERATOR
can no  "exploitant : ne lit pas les secrets de ia"             get secrets -n ia $OPERATOR
can no  "exploitant : ne lit pas les secrets de ingestion"      get secrets -n ingestion $OPERATOR
can no  "exploitant : ne liste pas les secrets du cluster"      list secrets --all-namespaces $OPERATOR
can no  "exploitant : ne crée pas de ClusterRoleBinding"        create clusterrolebindings $OPERATOR
can no  "compte de l'IA : aucun droit"                          list pods -n ia --as=system:serviceaccount:ia:telecom-ia
can no  "compte du générateur : ne lit pas les secrets"         get secrets -n ingestion --as=system:serviceaccount:ingestion:telecom-generator
can no  "compte par défaut de ia : aucun droit"                 get secrets -n ia --as=system:serviceaccount:ia:default
if kubectl get clusterrolebinding -o 'jsonpath={range .items[*]}{.metadata.name}{" "}{.subjects[*].name}{"\n"}{end}' 2>/dev/null | grep -qE 'cloudia:|telecom-ia|telecom-generator'; then
  ko "un ClusterRoleBinding vise un sujet du projet"
else
  ok "aucun ClusterRoleBinding ne vise un sujet du projet"
fi

# ---------------------------------------------------------------- attaque simulée (Q14)
section "Q14 : attaque simulée depuis un pod d'une autre équipe (namespace tenant-ops)"
if kubectl get pod -n tenant-ops attacker >/dev/null 2>&1; then
  kubectl wait -n tenant-ops --for=condition=Ready pod/attacker --timeout=120s >/dev/null 2>&1
  SA=/var/run/secrets/kubernetes.io/serviceaccount
  # api <méthode> <chemin> [corps] : code HTTP de la réponse de l'API, avec le jeton du pod compromis
  api() {
    local body_arg=""
    if [ -n "${3:-}" ]; then body_arg="-H 'Content-Type: application/merge-patch+json' -d '$3'"; fi
    kubectl exec -n tenant-ops attacker -- sh -c "curl -s -m 8 -o /dev/null -w '%{http_code}' --cacert $SA/ca.crt \
      -H \"Authorization: Bearer \$(cat $SA/token)\" -X $1 $body_arg \
      https://kubernetes.default.svc$2" 2>/dev/null
  }
  expect_code() { # expect_code <code> <description> <méthode> <chemin> [corps]
    local want="$1" desc="$2" got; got=$(api "$3" "$4" "${5:-}")
    if [ "$got" = "$want" ]; then ok "$desc (HTTP $got)"; else ko "$desc (HTTP ${got:-aucune réponse}, attendu $want)"; fi
  }
  expect_code 200 "témoin : le jeton est valide et l'API est joignable"      GET /api
  expect_code 403 "lister les secrets du projet (ia) refusé"                 GET /api/v1/namespaces/ia/secrets
  expect_code 403 "lister les secrets de ingestion (identifiants RabbitMQ) refusé" GET /api/v1/namespaces/ingestion/secrets
  expect_code 403 "lister les secrets de tout le cluster refusé"             GET /api/v1/secrets
  expect_code 403 "mettre l'IA à 0 réplique refusé"                          PATCH /apis/apps/v1/namespaces/ia/deployments/telecom-ia-deployment/scale '{"spec":{"replicas":0}}'
  r=$(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.spec.replicas}' 2>/dev/null)
  if [ "$r" = "1" ]; then ok "l'IA a toujours 1 réplique"; else ko "l'IA a $r réplique(s) !"; fi
  c=$(kubectl exec -n tenant-ops attacker -- sh -c "curl -s -m 5 --connect-timeout 4 -o /dev/null -k https://$RMQ:5671; echo \$?" 2>/dev/null | tail -1)
  case "$c" in 6|7|28) ok "pod d'une autre équipe → RabbitMQ:5671 bloqué par le réseau (curl code $c)";; *) ko "pod d'une autre équipe → RabbitMQ:5671 : la connexion s'établit (curl code ${c:-?})";; esac
else
  echo "  (ignoré : 07-tenant-simulation.yaml non appliqué ; pour cette section : kubectl apply -f 07-tenant-simulation.yaml)"
fi

# ---------------------------------------------------------------- alertes
section "Alertes (Alertmanager, kube-state-metrics, node-exporter)"
if [ -n "$PROM" ]; then
  rules=$(prom_get '/api/v1/rules')
  for name in IaScaledToZero IaUnavailable BrokerUnavailable IaMetricsMissing PrometheusStorageNearRetentionLimit NodeDiskAlmostFull; do
    if echo "$rules" | grep -q "\"$name\""; then ok "règle $name chargée"; else ko "règle $name absente (06-alert-rules.yaml appliqué ?)"; fi
  done
  if [ "${SCALE_TEST:-0}" = "1" ]; then
    echo "  Test Q14 : mise de l'IA à 0 réplique (en administrateur), attente de l'alerte (jusqu'à 3 min)..."
    kubectl scale deployment/telecom-ia-deployment -n ia --replicas=0 >/dev/null
    fired=no
    for i in $(seq 1 18); do
      sleep 10
      a=$(prom_get '/api/v1/query?query=ALERTS%7Balertname%3D%22IaScaledToZero%22%2Calertstate%3D%22firing%22%7D')
      if echo "$a" | grep -q IaScaledToZero; then fired=yes; break; fi
    done
    kubectl scale deployment/telecom-ia-deployment -n ia --replicas=1 >/dev/null
    kubectl rollout status deployment/telecom-ia-deployment -n ia --timeout=180s >/dev/null 2>&1
    if [ "$fired" = "yes" ]; then ok "l'alerte IaScaledToZero s'est levée ; l'IA est remise à 1 réplique"; else ko "l'alerte IaScaledToZero ne s'est pas levée en 3 min (l'IA a été remise à 1 réplique)"; fi
  else
    echo "  (test de mise à 0 réplique non joué ; pour le lancer : SCALE_TEST=1 ./v5-checks.sh)"
  fi
elif [ "${CI_MODE:-0}" = "1" ]; then
  echo "  (ignoré : pile d'observabilité absente de ce cluster)"
else
  ko "pod Prometheus introuvable"
fi

summary
