import os
import time
import json
import numpy as np
import pika  # Comme client de RabbitMQ
from sklearn.ensemble import IsolationForest

from amqp_tls import build_params

# Import de la bibliothèque de métriques Cloud Native
from prometheus_client import start_http_server, Counter

QUEUE = 'telecom_traffic'
# Nombre max de messages non acquittés en vol : borne le travail simultané lors d'un rattrapage
PREFETCH = int(os.environ.get('PREFETCH', '50'))

# Déclaration des compteurs de métriques pour Grafana
TRAFFIC_COUNTER = Counter('telecom_processed_packets_total', 'Nombre total de paquets analysés')
ANOMALY_COUNTER = Counter('telecom_anomalies_detected_total', 'Nombre total d\'anomalies réseau détectées')

# 1. Simulation / Génération de données réseau normales (Entraînement)
# Référence du trafic normal : 100 paquets, 1 % d'erreurs, 20 ms de latence (le générateur doit l'utiliser)
print("🤖 Entraînement du modèle IA avec le trafic normal...")
data_normal = np.random.normal(loc=[100, 1, 20], scale=[10, 0.5, 2], size=(500, 3))

# Entraînement de l'Isolation Forest
clf = IsolationForest(contamination=0.05, random_state=42)
clf.fit(data_normal)
print("✅ Modèle entraîné et prêt à analyser le flux réseau (Version v5 - ACK + reconnexion + TLS).")

# Démarrage du serveur de métriques Prometheus sur le port 8000
start_http_server(8000)
print("📊 Serveur de métriques Prometheus démarré sur le port 8000")

# ---------------
AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
params = build_params(AMQP_URL)  # v5 : amqps:// + certificat client si AMQP_CA_FILE / AMQP_CERT_FILE / AMQP_KEY_FILE


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


# ---------------
# 2. Traitement du flux de production en continu

def callback(ch, method, properties, body):
    try:
        # Réception du message contenant les métriques réseau
        data = json.loads(body)

        # Extraction des métriques
        metrics = np.array([[data['packets'], data['errors'], data['latency']]])

        # Prédiction de l'IA
        prediction = clf.predict(metrics)

        # Incrémentation des métriques Cloud Native
        TRAFFIC_COUNTER.inc()
        if prediction[0] == -1:
            ANOMALY_COUNTER.inc()
            status = "🚨 ANOMALIE"
        else:
            status = "🟢 Normal"

        print(f"[{status}] Source: {data['source']} -> Paquets: {data['packets']}, Erreurs: {data['errors']}%, Latence: {data['latency']}ms")

    except Exception as e:
        print(f"⚠️ Erreur lors de l'analyse du message : {e}")

    finally:
        # On envoie toujours l'acquittement pour éviter de bloquer la file
        ch.basic_ack(delivery_tag=method.delivery_tag)


# Boucle de résilience : si le broker disparaît, l'IA se reconnecte
# au lieu de planter (pas de RESTARTS, pas de CrashLoopBackOff).
while True:
    connection = connect_with_retry()

    try:
        channel = connection.channel()
        # Déclaration idempotente (mêmes arguments que le générateur) : plus de course
        # au démarrage si l'IA arrive avant le générateur (sinon erreur 404 sur la file).
        channel.queue_declare(queue=QUEUE, durable=True)
        channel.basic_qos(prefetch_count=PREFETCH)
        channel.basic_consume(queue=QUEUE, on_message_callback=callback, auto_ack=False)
        print(f"📥 En attente de flux réseau sur la file '{QUEUE}'...")
        channel.start_consuming()
    except KeyboardInterrupt:
        print("🛑 Arrêt demandé.")
        break
    except (pika.exceptions.AMQPConnectionError, pika.exceptions.AMQPChannelError) as e:
        print(f"⚠️ Connexion perdue : {e}. Reconnexion dans 3 secondes...")
        time.sleep(3)
