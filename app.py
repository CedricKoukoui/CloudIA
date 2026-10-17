"""Moteur d'IA (v6) : consomme la file RabbitMQ et classe chaque mesure avec le modèle VALIDÉ du registre.

Ce qui change par rapport à la v5 (passage à l'échelle horizontal) :
  - plus d'entraînement local : le modèle est téléchargé depuis le registre MLflow (alias « production »),
    donc tous les pods utilisent le même modèle (model_registry.py) ;
  - le pod n'est « Ready » qu'une fois le modèle chargé (fichier /tmp/model-ready, lu par la sonde) ;
  - métriques de passage à l'échelle : version du modèle par pod, durée d'inférence ;
  - arrêt propre sur SIGTERM (réduction du nombre de pods par l'autoscaler) : le message en cours est
    soit acquitté, soit rendu à la file par RabbitMQ, jamais perdu ;
  - journaux limités (anomalies + un résumé périodique) : à 100 messages/s et 5 pods, un journal par
    message coûterait plus cher que l'inférence elle-même.

Plusieurs pods lisant la même file n'est PAS un problème : c'est le modèle des « consommateurs
concurrents ». RabbitMQ remet chaque message à un seul consommateur ; PREFETCH borne le nombre de
messages qu'un pod garde en réserve (non acquittés), ce qui laisse le reste de la file aux autres pods.
"""
import json
import os
import signal
import time

import numpy as np
import pika  # Comme client de RabbitMQ
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from amqp_tls import build_params
from model_registry import load_production_model

QUEUE = 'telecom_traffic'
# Nombre max de messages non acquittés en vol par pod : borne le travail simultané et laisse
# le reste de la file aux autres pods (répartition entre consommateurs concurrents)
PREFETCH = int(os.environ.get('PREFETCH', '50'))
MLFLOW_TRACKING_URI = os.environ.get('MLFLOW_TRACKING_URI', 'http://localhost:5000')
MODEL_NAME = os.environ.get('MODEL_NAME', 'telecom-anomaly')
MODEL_ALIAS = os.environ.get('MODEL_ALIAS', 'production')
READY_FILE = os.environ.get('READY_FILE', '/tmp/model-ready')
SUMMARY_EVERY = float(os.environ.get('SUMMARY_EVERY', '10'))  # secondes entre deux résumés

# Métriques Cloud Native (les deux compteurs gardent leur nom : tableaux de bord et tests inchangés)
TRAFFIC_COUNTER = Counter('telecom_processed_packets_total', 'Nombre total de paquets analysés')
ANOMALY_COUNTER = Counter('telecom_anomalies_detected_total', 'Nombre total d\'anomalies réseau détectées')
INFERENCE_SECONDS = Histogram('telecom_inference_seconds', 'Durée d\'analyse d\'un message (décodage + prédiction)',
                              buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25))
MODEL_INFO = Gauge('telecom_model_info', 'Modèle chargé par ce pod (valeur 1)',
                   ['model_name', 'model_version', 'model_sha256'])


class Shutdown(BaseException):
    """Levée par le gestionnaire de SIGTERM pour sortir proprement. BaseException (comme KeyboardInterrupt) :
    les « except Exception » des boucles de reconnexion ne l'interceptent donc pas."""


def on_sigterm(signum, frame):
    raise Shutdown()


signal.signal(signal.SIGTERM, on_sigterm)

# 1. Le modèle validé, téléchargé depuis le registre (AUCUN entraînement dans le pod)
try:
    os.remove(READY_FILE)
except FileNotFoundError:
    pass
print(f"📦 Chargement du modèle « {MODEL_NAME}@{MODEL_ALIAS} » depuis le registre {MLFLOW_TRACKING_URI}...")
try:
    clf, MODEL_VERSION, RUN_ID, MODEL_SHA = load_production_model(MLFLOW_TRACKING_URI, MODEL_NAME, MODEL_ALIAS)
except (Shutdown, KeyboardInterrupt):
    print("🛑 Arrêt demandé avant le chargement du modèle.")
    raise SystemExit(0)
MODEL_INFO.labels(MODEL_NAME, MODEL_VERSION, MODEL_SHA[:12]).set(1)
print(f"✅ Modèle version {MODEL_VERSION} chargé (empreinte {MODEL_SHA[:12]}, run {RUN_ID[:8]}). "
      "Version v6 - modèle centralisé + passage à l'échelle.")

# 2. Serveur de métriques Prometheus sur le port 8000
start_http_server(8000)
print("📊 Serveur de métriques Prometheus démarré sur le port 8000")

AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
params = build_params(AMQP_URL)  # amqps:// + certificat client si AMQP_CA_FILE / AMQP_CERT_FILE / AMQP_KEY_FILE


def connect_with_retry(delay=3):
    """Connexion à RabbitMQ : réessaie sans limite. Une panne longue du broker ne doit pas
    faire sortir l'IA (sinon le pod redémarre, RESTARTS monte et les compteurs repartent à 0)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            if attempt == 1 or attempt % 10 == 0:
                print(f"⏳ Tentative de connexion à RabbitMQ (n°{attempt})...")
            return pika.BlockingConnection(params)
        except Exception as e:
            if attempt == 1 or attempt % 10 == 0:
                print(f"⚠️ Broker injoignable : {e}. Nouvel essai toutes les {delay} secondes...")
            time.sleep(delay)


# Résumé périodique : nombre de messages et d'anomalies depuis le dernier résumé
window = {"start": time.monotonic(), "messages": 0, "anomalies": 0, "last_normal_log": 0.0}


def log_progress(status, data):
    now = time.monotonic()
    # Une ligne par anomalie ; au plus une ligne « Normal » par seconde (témoin de vie)
    if status != "🟢 Normal" or now - window["last_normal_log"] >= 1.0:
        if status == "🟢 Normal":
            window["last_normal_log"] = now
        print(f"[{status}] Source: {data['source']} -> Paquets: {data['packets']}, "
              f"Erreurs: {data['errors']}%, Latence: {data['latency']}ms")
    if now - window["start"] >= SUMMARY_EVERY:
        elapsed = now - window["start"]
        print(f"📈 {window['messages']} messages en {elapsed:.0f} s ({window['messages'] / elapsed:.1f}/s), "
              f"{window['anomalies']} anomalie(s), modèle v{MODEL_VERSION}")
        window.update(start=now, messages=0, anomalies=0)


# 3. Traitement du flux de production en continu
def callback(ch, method, properties, body):
    try:
        with INFERENCE_SECONDS.time():
            data = json.loads(body)
            metrics = np.array([[data['packets'], data['errors'], data['latency']]])
            prediction = clf.predict(metrics)
        TRAFFIC_COUNTER.inc()
        window["messages"] += 1
        if prediction[0] == -1:
            ANOMALY_COUNTER.inc()
            window["anomalies"] += 1
            status = "🚨 ANOMALIE"
        else:
            status = "🟢 Normal"
        log_progress(status, data)
    except Exception as e:
        print(f"⚠️ Erreur lors de l'analyse du message : {e}")
    # Acquittement après traitement (un message illisible est aussi acquitté pour ne pas bloquer la file).
    # Si SIGTERM arrive avant cette ligne, le message n'est pas acquitté : RabbitMQ le remet à un autre pod.
    ch.basic_ack(delivery_tag=method.delivery_tag)


# Boucle de résilience : si le broker disparaît, l'IA se reconnecte
# au lieu de planter (pas de RESTARTS, pas de CrashLoopBackOff).
connection = None
try:
    while True:
        connection = connect_with_retry()
        try:
            channel = connection.channel()
            # Déclaration idempotente (mêmes arguments que le générateur)
            channel.queue_declare(queue=QUEUE, durable=True)
            channel.basic_qos(prefetch_count=PREFETCH)
            channel.basic_consume(queue=QUEUE, on_message_callback=callback, auto_ack=False)
            open(READY_FILE, 'w').close()  # sonde de disponibilité : modèle chargé, consommation en place
            print(f"📥 En attente de flux réseau sur la file '{QUEUE}' (prefetch {PREFETCH})...")
            channel.start_consuming()
        except (pika.exceptions.AMQPConnectionError, pika.exceptions.AMQPChannelError) as e:
            print(f"⚠️ Connexion perdue : {e}. Reconnexion dans 3 secondes...")
            time.sleep(3)
except (Shutdown, KeyboardInterrupt):
    # Arrêt demandé (réduction d'échelle, mise à jour, kubectl delete) : on ferme proprement la
    # connexion ; les messages reçus mais non acquittés retournent dans la file pour les autres pods.
    print("🛑 Arrêt demandé : fermeture de la connexion, les messages non acquittés retournent dans la file.")
    try:
        if connection is not None and connection.is_open:
            connection.close()
    except Exception:
        pass
