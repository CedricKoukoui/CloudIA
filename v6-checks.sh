#!/usr/bin/env bash
# Contrôles de la v6 (passage à l'échelle) sur le cluster. Chaque contrôle affiche OK ou ÉCHEC ;
# code de sortie 1 s'il y a un échec. Les contrôles de la v5 restent ceux de v5-checks.sh.
#
#   ./v6-checks.sh preflight      avant 10-autoscaling.yaml : KEDA installé, labels et Service attendus ?
#   ./v6-checks.sh                registre, modèle unique dans tous les pods, réseau, autoscaler, RBAC, alertes
#   SELFHEAL_TEST=1 ./v6-checks.sh  ajoute : l'IA est mise à 0 réplique, KEDA doit la remettre à 1 (≈ 30 s)
#   CI_MODE=1 ./v6-checks.sh      pour la CI (cluster Kind sans Prometheus ni KEDA) : sections ignorées
#
# Le test de charge (crise × 100) est à part : v6-load-test.sh.
set -u

PASS=0; FAIL=0
ok() { echo "  OK     $1"; PASS=$((PASS + 1)); }
ko() { echo "  ÉCHEC  $1"; FAIL=$((FAIL + 1)); }
skip() { echo "  (ignoré : $1)"; }
section() { echo; echo "== $1"; }
summary() {
  echo; echo "Bilan : $PASS contrôle(s) réussi(s), $FAIL échec(s)."
  [ "$FAIL" -eq 0 ]
}
CI=${CI_MODE:-0}

first_pod() { kubectl get pods -n "$1" -l "$2" -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null; }
running_pods() { kubectl get pods -n "$1" -l "$2" --field-selector=status.phase=Running -o 'jsonpath={range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null; }

# ---------------------------------------------------------------- preflight
if [ "${1:-}" = "preflight" ]; then
  section "Avant 10-autoscaling.yaml"
  if kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then ok "CRD ScaledObject installée (KEDA)"; else ko "KEDA absent : helm upgrade --install keda kedacore/keda -n keda --create-namespace -f keda-values.yaml"; fi
  n=$(kubectl get pods -n keda -l app.kubernetes.io/name=keda-operator --no-headers 2>/dev/null | grep -c Running)
  if [ "$n" -ge 1 ]; then ok "pod keda-operator en marche, label app.kubernetes.io/name=keda-operator (règle de 04)"; else ko "aucun pod keda-operator Running avec le label attendu (adapter 04-network-policy-observability.yaml)"; fi
  if kubectl get svc -n default prometheus-operated >/dev/null 2>&1; then ok "Service prometheus-operated présent (adresse utilisée par 10-autoscaling.yaml)"; else ko "Service prometheus-operated absent : adapter serverAddress dans 10-autoscaling.yaml"; fi
  if kubectl get apiservice v1beta1.external.metrics.k8s.io >/dev/null 2>&1; then ok "API external.metrics.k8s.io enregistrée (serveur de métriques de KEDA)"; else ko "API external.metrics.k8s.io absente"; fi
  if kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1; then ok "CRD PodMonitor présente (pile v4)"; else ko "CRD PodMonitor absente : la pile Prometheus de la v4 est-elle installée ?"; fi
  summary; exit $?
fi

GEN=$(first_pod ingestion app=telecom-generator)
IA=$(first_pod ia app=telecom-ia)
PROM=$(first_pod default app.kubernetes.io/name=prometheus)
REGISTRY=mlflow.mlops.svc.cluster.local

# Tunnels depuis ce poste (kubectl port-forward ne passe pas par les NetworkPolicy) : Prometheus et MLflow
PIDS=""; FWD_PORT=""
trap 'for p in $PIDS; do kill "$p" 2>/dev/null; done' EXIT
forward() { # forward <namespace> <ressource> <port distant> <chemin de test> : port local dans FWD_PORT
  FWD_PORT=$((19000 + RANDOM % 900))
  kubectl port-forward -n "$1" "$2" "$FWD_PORT:$3" >/dev/null 2>&1 &
  PIDS="$PIDS $!"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -m 2 -o /dev/null "http://127.0.0.1:$FWD_PORT$4"; then return 0; fi
    sleep 1
  done
  FWD_PORT=""; return 1
}
PROM_PORT=""; MLFLOW_PORT=""
if [ -n "$PROM" ] && forward default "pod/$PROM" 9090 /-/ready; then PROM_PORT=$FWD_PORT; fi
prom() { curl -s -m 10 -G "http://127.0.0.1:$PROM_PORT/api/v1/query" --data-urlencode "query=$1"; }
prom_value() { # première valeur scalaire d'une requête Prometheus (vide si aucun résultat)
  prom "$1" | python3 -c 'import json,sys
r=json.load(sys.stdin)["data"]["result"]
print(r[0]["value"][1] if r else "")' 2>/dev/null
}

TCP_PY='import socket,sys
s=socket.socket(); s.settimeout(4)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); print("connected")
except Exception:
    print("blocked")'
expect_tcp() { # expect_tcp <connected|blocked> <description> <namespace> <pod> <hôte> <port>
  local got
  got=$(kubectl exec -n "$3" "$4" -- python -c "$TCP_PY" "$5" "$6" 2>/dev/null | tail -1)
  if [ "$got" = "$1" ]; then ok "$2"; else ko "$2 (obtenu : ${got:-aucune réponse})"; fi
}

# ---------------------------------------------------------------- registre
section "Registre de modèles (MLflow, namespace mlops)"
ML=$(first_pod mlops app=mlflow)
ready=$(kubectl get pod -n mlops "$ML" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
if [ "$ready" = "True" ]; then ok "pod MLflow prêt ($ML)"; else ko "pod MLflow absent ou pas prêt"; fi
pvc=$(kubectl get pvc -n mlops mlflow-data -o 'jsonpath={.status.phase}' 2>/dev/null)
if [ "$pvc" = "Bound" ]; then ok "volume du registre (mlflow-data) attaché"; else ko "volume mlflow-data : ${pvc:-absent}"; fi
job=$(kubectl get jobs -n mlops -l app=model-trainer --sort-by=.metadata.creationTimestamp -o 'jsonpath={range .items[*]}{.metadata.name} {.status.succeeded}{"\n"}{end}' 2>/dev/null | tail -1)
if [ -n "$job" ]; then
  if [ "${job##* }" = "1" ]; then ok "dernier Job d'entraînement réussi (${job%% *})"; else ko "dernier Job d'entraînement non réussi (${job%% *}) : kubectl logs -n mlops job/${job%% *}"; fi
else
  echo "  (aucun Job d'entraînement visible : supprimé après 24 h, ou jamais lancé)"
fi
ALIAS_VERSION=""
if [ -n "$ML" ] && forward mlops svc/mlflow 5000 /health; then
  MLFLOW_PORT=$FWD_PORT
  ALIAS_JSON=$(curl -s -m 10 "http://127.0.0.1:$MLFLOW_PORT/api/2.0/mlflow/registered-models/alias?name=telecom-anomaly&alias=production")
  ALIAS_VERSION=$(echo "$ALIAS_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["model_version"]["version"])' 2>/dev/null)
  GATE=$(echo "$ALIAS_JSON" | python3 -c 'import json,sys
t={x["key"]:x["value"] for x in json.load(sys.stdin)["model_version"].get("tags",[])}
print(t.get("quality_gate",""), t.get("detection_rate",""), t.get("false_positive_rate",""))' 2>/dev/null)
  if [ -n "$ALIAS_VERSION" ]; then ok "alias « production » -> version $ALIAS_VERSION"; else ko "aucune version promue (alias production absent) : kubectl create -f 09-train-job.yaml"; fi
  read -r g_status g_det g_fp <<< "$GATE"
  if [ "${g_status:-}" = "passed" ]; then ok "la version en production a passé la porte de qualité (détection ${g_det:-?}, faux positifs ${g_fp:-?})"; else ko "porte de qualité non confirmée pour la version en production (${GATE:-tags absents})"; fi
else
  ko "registre MLflow injoignable par port-forward"
fi

# ---------------------------------------------------------------- un seul modèle dans tous les pods
section "Cohérence : tous les pods de l'IA servent le même modèle, aucun ne s'entraîne"
PODS=$(running_pods ia app=telecom-ia)
if [ -z "$PODS" ]; then
  ko "aucun pod de l'IA en marche"
else
  n=0; versions=""; trained=0; notready=0
  for p in $PODS; do
    n=$((n + 1))
    line=$(kubectl logs -n ia "$p" 2>/dev/null | grep -m1 'Modèle version .* chargé')
    v=$(echo "$line" | sed -n 's/.*Modèle version \([^ ]*\) chargé (empreinte \([0-9a-f]*\).*/\1:\2/p')
    versions="$versions ${v:-inconnue}"
    if kubectl logs -n ia "$p" 2>/dev/null | grep -q 'Entraînement du modèle IA'; then trained=$((trained + 1)); fi
    r=$(kubectl get pod -n ia "$p" -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}')
    [ "$r" = "True" ] || notready=$((notready + 1))
  done
  distinct=$(echo "$versions" | tr ' ' '\n' | grep -v '^$' | sort -u)
  if [ "$(echo "$distinct" | wc -l | tr -d ' ')" = "1" ] && [ "$distinct" != "inconnue" ]; then
    ok "$n pod(s), une seule version:empreinte du modèle : $distinct"
  else
    ko "$n pod(s), versions différentes ou illisibles :$(echo "$distinct" | tr '\n' ' ')"
  fi
  if [ -n "$ALIAS_VERSION" ]; then
    if [ "${distinct%%:*}" = "$ALIAS_VERSION" ]; then ok "c'est la version en production ($ALIAS_VERSION)"
    else ko "les pods servent ${distinct%%:*}, la production est $ALIAS_VERSION : kubectl rollout restart deployment/telecom-ia-deployment -n ia"; fi
  fi
  if [ "$trained" -eq 0 ]; then ok "aucun pod n'a entraîné de modèle localement"; else ko "$trained pod(s) ont entraîné un modèle localement (ancienne image ?)"; fi
  if [ "$notready" -eq 0 ]; then ok "tous les pods de l'IA sont Ready (modèle chargé, consommation en place)"; else ko "$notready pod(s) de l'IA pas Ready"; fi
fi
if [ -n "$PROM_PORT" ]; then
  d=$(prom_value 'count(count by (model_version, model_sha256) (telecom_model_info{namespace="ia"}))')
  c=$(prom_value 'count(telecom_model_info{namespace="ia"})')
  if [ "$d" = "1" ]; then ok "Prometheus : 1 seul couple version/empreinte parmi $c pod(s) collecté(s)"; else ko "Prometheus : ${d:-0} versions distinctes du modèle (telecom_model_info)"; fi
elif [ "$CI" = "1" ]; then skip "Prometheus absent de ce cluster"; else ko "Prometheus injoignable"; fi

# ---------------------------------------------------------------- réseau
section "Réseau (v6) : registre et métriques de la file"
if [ -n "$IA" ] && [ -n "$GEN" ]; then
  # Le Service de RabbitMQ n'expose pas 15692 : on vise l'IP du pod, sinon le test serait bloqué de toute façon
  RMQ_IP=$(kubectl get pod -n ingestion telecom-broker-rabbitmq-0 -o 'jsonpath={.status.podIP}' 2>/dev/null)
  expect_tcp connected "IA -> registre MLflow:5000 autorisé" ia "$IA" "$REGISTRY" 5000
  expect_tcp blocked   "générateur -> registre MLflow:5000 bloqué (pas de faux modèle depuis le pod exposé)" ingestion "$GEN" "$REGISTRY" 5000
  expect_tcp blocked   "générateur -> métriques RabbitMQ:15692 bloqué" ingestion "$GEN" "$RMQ_IP" 15692
  expect_tcp blocked   "IA -> métriques RabbitMQ:15692 bloqué" ia "$IA" "$RMQ_IP" 15692
else
  ko "pods générateur ou IA introuvables"
fi
if [ -n "$PROM_PORT" ]; then
  q=$(prom_value 'sum(rabbitmq_detailed_queue_messages_ready{namespace="ingestion",queue="telecom_traffic"})')
  if [ -n "$q" ]; then ok "Prometheus collecte la longueur de la file (messages en attente : $q)"; else ko "rabbitmq_detailed_queue_messages_ready absent de Prometheus (PodMonitor, port 15692, règle réseau 9 ?)"; fi
elif [ "$CI" = "1" ]; then skip "Prometheus absent de ce cluster"; fi

# ---------------------------------------------------------------- autoscaler
section "Autoscaler (KEDA + HPA)"
if [ "$CI" = "1" ] && ! kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then
  skip "KEDA absent de ce cluster"
else
  st=$(kubectl get scaledobject -n ia telecom-ia-scaler -o 'jsonpath={.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
  if [ "$st" = "True" ]; then ok "ScaledObject telecom-ia-scaler prêt (KEDA lit Prometheus)"; else ko "ScaledObject pas prêt (${st:-absent}) : kubectl describe scaledobject -n ia telecom-ia-scaler"; fi
  fb=$(kubectl get scaledobject -n ia telecom-ia-scaler -o 'jsonpath={.status.conditions[?(@.type=="Fallback")].status}' 2>/dev/null)
  if [ "$fb" != "True" ]; then ok "pas de repli en cours (les deux requêtes Prometheus répondent)"; else ko "repli actif : KEDA n'obtient plus les métriques"; fi
  HPA=keda-hpa-telecom-ia-scaler
  mm=$(kubectl get hpa -n ia "$HPA" -o 'jsonpath={.spec.minReplicas} {.spec.maxReplicas}' 2>/dev/null)
  if [ "$mm" = "1 5" ]; then ok "HPA $HPA : de 1 à 5 pods"; else ko "HPA $HPA : min/max = ${mm:-absent}"; fi
  cur=$(kubectl get hpa -n ia "$HPA" -o 'jsonpath={range .status.currentMetrics[*]}{.external.metric.name}={.external.current.averageValue} {end}' 2>/dev/null)
  if [ -n "$cur" ]; then ok "valeurs lues par l'HPA : $cur"; else ko "l'HPA ne lit aucune métrique externe"; fi
  rep=$(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.spec.replicas}/{.status.readyReplicas}' 2>/dev/null)
  echo "         pods de l'IA demandés/prêts : $rep"
fi
pods=$(kubectl get resourcequota -n ia ia-quota -o 'jsonpath={.spec.hard.pods}' 2>/dev/null)
if [ "$pods" = "6" ]; then ok "quota du namespace ia : 6 pods au plus (5 + 1 de surplus pendant une mise à jour)"; else ko "quota du namespace ia : pods = ${pods:-non défini}"; fi

# ---------------------------------------------------------------- RBAC
section "RBAC (v6)"
can() { # can <yes|no> <description> <arguments de « kubectl auth can-i »>
  local want="$1" desc="$2" got; shift 2
  got=$(kubectl auth can-i "$@" 2>/dev/null)
  if [ "$got" = "$want" ]; then ok "$desc"; else ko "$desc (obtenu : ${got:-rien})"; fi
}
VIEWER="--as=lecteur --as-group=cloudia:viewers"
OPERATOR="--as=exploitant --as-group=cloudia:operators"
# shellcheck disable=SC2086
{
can yes "lecteur : voit l'HPA de l'IA"                              get horizontalpodautoscalers -n ia $VIEWER
can yes "lecteur : voit les pods du registre"                       list pods -n mlops $VIEWER
can no  "exploitant : ne modifie pas la règle de l'autoscaler"      patch scaledobjects.keda.sh -n ia $OPERATOR
can no  "exploitant : ne lance pas d'entraînement"                  create jobs -n mlops $OPERATOR
can no  "exploitant : ne lit aucun secret de mlops"                 get secrets -n mlops $OPERATOR
can no  "compte de l'entraînement : aucun droit"                    list pods -n mlops --as=system:serviceaccount:mlops:model-trainer
can no  "compte du registre : aucun droit"                          list pods -n mlops --as=system:serviceaccount:mlops:mlflow
}

# ---------------------------------------------------------------- alertes
section "Alertes (v6)"
if [ -n "$PROM_PORT" ]; then
  rules=$(curl -s -m 10 "http://127.0.0.1:$PROM_PORT/api/v1/rules")
  for name in AutoscalingQueueMetricMissing IaBacklogHigh IaAtMaxReplicas IaReplicasNotReady IaModelVersionMismatch; do
    if echo "$rules" | grep -q "\"$name\""; then ok "règle $name chargée"; else ko "règle $name absente (06-alert-rules.yaml appliqué ?)"; fi
  done
elif [ "$CI" = "1" ]; then skip "Prometheus absent de ce cluster"; else ko "Prometheus injoignable"; fi

# ---------------------------------------------------------------- auto-réparation
if [ "${SELFHEAL_TEST:-0}" = "1" ]; then
  section "Auto-réparation : l'IA mise à 0 réplique (en administrateur, scénario Q14)"
  kubectl scale deployment/telecom-ia-deployment -n ia --replicas=0 >/dev/null
  healed=no
  for i in $(seq 1 12); do
    sleep 5
    r=$(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.spec.replicas}')
    if [ "${r:-0}" -ge 1 ]; then healed=yes; echo "         remise à $r réplique(s) après environ $((i * 5)) s"; break; fi
  done
  if [ "$healed" = "yes" ]; then
    ok "KEDA a remis l'IA au minimum sans intervention"
  else
    ko "l'IA est restée à 0 réplique pendant 60 s (remise à 1 à la main)"
    kubectl scale deployment/telecom-ia-deployment -n ia --replicas=1 >/dev/null
  fi
  kubectl rollout status deployment/telecom-ia-deployment -n ia --timeout=180s >/dev/null 2>&1
fi

summary
