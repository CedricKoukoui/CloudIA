#!/usr/bin/env bash
# Test de passage à l'échelle (v6) : le trafic des antennes est multiplié par 100 en quelques secondes.
#
# Déroulé :
#   1. état de départ (débit normal : RATE=1 message/s) ;
#   2. CRISE : le générateur passe à CRISIS_RATE messages/s (100 par défaut, soit × 100) pendant
#      CRISIS_SECONDS secondes ; on relève toutes les INTERVAL secondes le retard de la file, le débit
#      traité, le nombre de pods demandés/prêts et le nombre de versions du modèle en service ;
#   3. RETOUR AU CALME : RATE=1, on observe la réduction du nombre de pods pendant CALM_SECONDS ;
#   4. bilan : délai de la première montée en charge, pic de pods, pic de retard, capacité mesurée
#      d'un pod (à reporter dans le seuil « throughput » de 10-autoscaling.yaml), versions du modèle.
#
# Les relevés sont aussi écrits dans un fichier CSV (pour la section 13 du README).
#
# Usage :
#   ./v6-load-test.sh
#   CRISIS_RATE=200 CRISIS_SECONDS=300 ./v6-load-test.sh
#
# Deux métriques déclenchent la montée en charge (10-autoscaling.yaml) : le débit traité (1 pod par
# tranche de 50 messages/s, donc 2 pods dès 100 messages/s) et le retard de la file (1 pod par
# tranche de 100 messages en attente, quand les pods saturent). La capacité d'un pod n'est mesurable
# que s'il sature : si aucun retard n'apparaît, augmenter CRISIS_RATE (limite : le générateur, 400m CPU).
# Le générateur est remis à RATE=1 à la fin, même en cas d'interruption (Ctrl+C).
set -u

CRISIS_RATE=${CRISIS_RATE:-100}
CRISIS_SECONDS=${CRISIS_SECONDS:-240}
CALM_SECONDS=${CALM_SECONDS:-300}
INTERVAL=${INTERVAL:-10}
CSV=${CSV:-v6-load-test-$(date +%Y%m%d-%H%M%S).csv}

PROM=$(kubectl get pods -n default -l app.kubernetes.io/name=prometheus -o 'jsonpath={.items[0].metadata.name}' 2>/dev/null)
[ -n "$PROM" ] || { echo "Pod Prometheus introuvable."; exit 2; }
kubectl get scaledobject -n ia telecom-ia-scaler >/dev/null 2>&1 || { echo "ScaledObject absent : appliquer 10-autoscaling.yaml."; exit 2; }

PORT=$((19000 + RANDOM % 900))
kubectl port-forward -n default "pod/$PROM" "$PORT:9090" >/dev/null 2>&1 &
PF_PID=$!

restore() {
  echo; echo "Remise du générateur au débit normal (RATE=1)..."
  kubectl set env deployment/telecom-generator-deployment -n ingestion RATE=1 >/dev/null
  kill "$PF_PID" 2>/dev/null
}
trap restore EXIT
trap 'exit 130' INT TERM

for _ in 1 2 3 4 5 6 7 8 9 10; do curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/-/ready" && break; sleep 1; done

q() { # q <requête PromQL> : valeur arrondie, « - » si aucun résultat
  curl -s -m 10 -G "http://127.0.0.1:$PORT/api/v1/query" --data-urlencode "query=$1" | python3 -c 'import json,sys
try:
    r=json.load(sys.stdin)["data"]["result"]
    print(round(float(r[0]["value"][1]), 1) if r else "-")
except Exception:
    print("-")'
}

T0=$(date +%s)
PEAK_PODS=0; PEAK_BACKLOG=0; FIRST_SCALE=""; MAX_VERSIONS=0; PEAK_PER_POD=0; PHASE=""
echo "t_s,phase,backlog,processed_per_s,replicas_spec,replicas_ready,model_versions" > "$CSV"

sample() {
  local t backlog rate spec ready versions per_pod
  t=$(( $(date +%s) - T0 ))
  backlog=$(q 'sum(rabbitmq_detailed_queue_messages_ready{namespace="ingestion",queue="telecom_traffic"})')
  rate=$(q 'sum(rate(telecom_processed_packets_total{namespace="ia"}[1m]))')
  versions=$(q 'count(count by (model_version) (telecom_model_info{namespace="ia"}))')
  spec=$(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.spec.replicas}' 2>/dev/null)
  ready=$(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.status.readyReplicas}' 2>/dev/null)
  ready=${ready:-0}
  printf "%5ss  %-8s  retard %8s   traités/s %7s   pods %s/%s   versions du modèle %s\n" "$t" "$PHASE" "$backlog" "$rate" "${spec:-?}" "$ready" "$versions"
  echo "$t,$PHASE,$backlog,$rate,${spec:-},$ready,$versions" >> "$CSV"
  [ "${spec:-0}" -gt "$PEAK_PODS" ] && PEAK_PODS=$spec
  if [ "$backlog" != "-" ] && python3 -c "import sys; sys.exit(0 if $backlog > $PEAK_BACKLOG else 1)"; then PEAK_BACKLOG=$backlog; fi
  if [ "$versions" != "-" ] && [ "${versions%.*}" -gt "$MAX_VERSIONS" ]; then MAX_VERSIONS=${versions%.*}; fi
  if [ -z "$FIRST_SCALE" ] && [ "$PHASE" = "crise" ] && [ "${spec:-1}" -gt 1 ]; then FIRST_SCALE=$(( t - CRISIS_T )); fi
  # Capacité d'un pod : mesurable seulement quand les pods sont saturés (un retard existe)
  if [ "$PHASE" = "crise" ] && [ "$rate" != "-" ] && [ "$backlog" != "-" ] && [ "$ready" -gt 0 ] \
     && python3 -c "import sys; sys.exit(0 if $backlog > 50 else 1)"; then
    per_pod=$(python3 -c "print(round($rate / $ready, 1))")
    python3 -c "import sys; sys.exit(0 if $per_pod > $PEAK_PER_POD else 1)" && PEAK_PER_POD=$per_pod
  fi
}

PHASE="départ"
echo "Relevés toutes les $INTERVAL s (fichier : $CSV)"
sample

echo; echo "CRISE : le générateur passe à $CRISIS_RATE messages/s pendant $CRISIS_SECONDS s"
kubectl set env deployment/telecom-generator-deployment -n ingestion RATE="$CRISIS_RATE" >/dev/null
kubectl rollout status deployment/telecom-generator-deployment -n ingestion --timeout=120s >/dev/null
PHASE="crise"; CRISIS_T=$(( $(date +%s) - T0 ))
END=$(( $(date +%s) + CRISIS_SECONDS ))
while [ "$(date +%s)" -lt "$END" ]; do sleep "$INTERVAL"; sample; done

echo; echo "RETOUR AU CALME : RATE=1, observation de la réduction pendant $CALM_SECONDS s"
kubectl set env deployment/telecom-generator-deployment -n ingestion RATE=1 >/dev/null
PHASE="calme"
END=$(( $(date +%s) + CALM_SECONDS ))
while [ "$(date +%s)" -lt "$END" ]; do sleep "$INTERVAL"; sample; done

echo
echo "================ Bilan"
echo "Débit de crise demandé            : $CRISIS_RATE messages/s (× $CRISIS_RATE par rapport au débit normal)"
echo "Première montée en charge         : ${FIRST_SCALE:-aucune} ${FIRST_SCALE:+s après le début de la crise}"
echo "Pic de pods de l'IA               : $PEAK_PODS"
echo "Pic de retard dans la file        : $PEAK_BACKLOG messages"
echo "Capacité d'un pod (pods saturés)  : ${PEAK_PER_POD} messages/s (0 = jamais saturé ; à comparer au seuil « throughput » de 50)"
echo "Versions du modèle en service     : au plus $MAX_VERSIONS à la fois (1 attendu)"
echo "Pods à la fin du retour au calme  : $(kubectl get deployment -n ia telecom-ia-deployment -o 'jsonpath={.spec.replicas}')"
echo "Relevés détaillés                 : $CSV"
if [ -z "$FIRST_SCALE" ]; then
  echo
  echo "Aucun pod ajouté pendant la crise : vérifier le ScaledObject (kubectl describe scaledobject -n ia telecom-ia-scaler)."
fi
if [ "$PEAK_PER_POD" = "0" ]; then
  echo "Les pods n'ont jamais saturé : pour mesurer la capacité d'un pod, CRISIS_RATE=$((CRISIS_RATE * 3)) ./v6-load-test.sh"
fi
