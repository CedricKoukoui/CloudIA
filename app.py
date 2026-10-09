import os
import time
import json
import sys
import numpy as np
import pika  # Comme client de RabbitMQ
from sklearn.ensemble import IsolationForest

# Import de la bibliothèque de métriques Cloud Native
from prometheus_client import start_http_server, Counter

# Déclaration des compteurs de métriques pour Grafana
TRAFFIC_COUNTER = Counter('telecom_processed_packets_total', 'Nombre total de paquets analysés')
ANOMALY_COUNTER = Counter('telecom_anomalies_detected_total', 'Nombre total d\'anomalies réseau détectées')

# 1. Simulation / Génération de données réseau normales (Entraînement)
print("🤖 Entraînement du modèle IA avec le trafic normal...")
data_normal = np.random.normal(loc=[100, 1, 20], scale=[10, 0.5, 2], size=(500, 3))

# Entraînement de l'Isolation Forest
clf = IsolationForest(contamination=0.05, random_state=42)
clf.fit(data_normal)
print("✅ Modèle entraîné et prêt à analyser le flux réseau (Version v2 - Mode ACK Securisé).")

# Démarrage du serveur de métriques Prometheus sur le port 8000
start_http_server(8000)
print("📊 Serveur de métriques Prometheus démarré sur le port 8000")

# ---------------
# Connexion à RabbitMQ avec boucle de reconnexion résiliente pour la CI/CD
AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
params = pika.URLParameters(AMQP_URL)

connection = None
for i in range(15):
    try:
        print(f"⏳ Tentative de connexion à RabbitMQ ({i+1}/15)...")
        connection = pika.BlockingConnection(params)
        break
    except Exception as e:
        print(f"⚠️ Erreur de connexion temporaire : {e}. Attente de 3 secondes...")
        time.sleep(3)

if not connection:
    print("❌ L'IA n'a pas pu se connecter à RabbitMQ après 15 tentatives.")
    sys.exit(1)

channel = connection.channel()

# SOLUTION v2 : On déclare la file d'attente comme DURABLE (alignement avec l'infrastructure)
channel.queue_declare(queue='telecom_traffic', durable=True)

# ---------------
# 2. Traitement du flux de production en continu

def callback(ch, method, properties, body):
    # Réception du message contenant les métriques réseau
    data = json.loads(body)
    
    # Extraction des métriques
    metrics = np.array([[data['packets'], data['errors'], data['latency']]])
    
    # Prédiction de l'IA
    prediction = clf.predict(metrics)

    # Incrémentation des métriques Cloud Native
    TRAFFIC_COUNTER.inc()
    if prediction == -1:
        ANOMALY_COUNTER.inc()
        status = "🚨 ANOMALIE"
    else:
        status = "🟢 Normal"	

    print(f"[{status}] Source: {data['source']} -> Paquets: {data['packets']}, Erreurs: {data['errors']}%, Latence: {data['latency']}ms")

    # SOLUTION v2 : On envoie l'acquittement manuel (ACK) UNIQUEMENT après le traitement de l'IA
    ch.basic_ack(delivery_tag=method.delivery_tag)

print("📥 En attente de flux réseau sur la file durable 'telecom_traffic'...")

# SOLUTION v2 : Désactivation de auto_ack (auto_ack=False) pour activer la sécurité de rétention
channel.basic_consume(queue='telecom_traffic', on_message_callback=callback, auto_ack=False)
channel.start_consuming()

