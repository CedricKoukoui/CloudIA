import os
import time
import json
import numpy as np
import pika # Comme client de RabbitMQ
from sklearn.ensemble import IsolationForest

# Import de la bibliothèque de métriques Cloud Native
from prometheus_client import start_http_server, Counter



# Déclaration des compteurs de métriques pour Grafana
TRAFFIC_COUNTER = Counter('telecom_processed_packets_total', 'Nombre total de paquets analysés')
ANOMALY_COUNTER = Counter('telecom_anomalies_detected_total', 'Nombre total d\'anomalies réseau détectées')




# 1. Simulation / Génération de données réseau normales (Entraînement)

# Génère 500 mesures (size=(500, 3)) avec 3 caractéristiques chacune
# Caractéristiques : [Volume paquets/s, Taux d'erreur %, Latence ms]

print("🤖 Entraînement du modèle IA avec le trafic normal...")
data_normal = np.random.normal(loc=[100, 1, 20], scale=[10, 0.5, 2], size=(500, 3))

# Entraînement de l'Isolation Forest
clf = IsolationForest(contamination=0.05, random_state=42)
clf.fit(data_normal)
print("✅ Modèle entraîné et prêt à analyser le flux réseau.")


# Démarrage du serveur de métriques Prometheus sur le port 8000
start_http_server(8000)
print("📊 Serveur de métriques Prometheus démarré sur le port 8000")


# ---------------
# Connexion à RabbitMQ via les variables d'environnement Kubernetes
# Client : (AMQP) --> Serveur (RabbitMQ)

# RabbitMQ : par défaut a été configuré sur le Port 5672 
AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
params = pika.URLParameters(AMQP_URL)
connection = pika.BlockingConnection(params)
channel = connection.channel()


# Déclaration de la file d'attente pour le flux télécom (telecom_traffic)
channel.queue_declare(queue='telecom_traffic')

# ---------------

# 2. Simulation d'un flux de production en continu

def callback(ch, method, properties, body):
    # Réception du message contenant les métriques réseau
    data = json.loads(body)
    # Les métrics
    metrics = np.array([[data['packets'], data['errors'], data['latency']]])
    
    # Prédiction de l'IA
    prediction = clf.predict(metrics)

    # test d'annomalies
    # status = "🚨 ANOMALIE" if prediction == -1 else "🟢 Normal"
    
    # Incrémentation des métriques Cloud Native
    TRAFFIC_COUNTER.inc()
    if prediction == -1:
        ANOMALY_COUNTER.inc()
        status = "🚨 ANOMALIE"
    else:
        status = "🟢 Normal"	

    print(f"[{status}] Source: {data['source']} -> Paquets: {data['packets']}, Erreurs: {data['errors']}%, Latence: {data['latency']}ms")

print("📥 En attente de flux réseau de production sur la file 'telecom_traffic'...")

# Reception de la donnée : ".basic_consume"
channel.basic_consume(queue='telecom_traffic', on_message_callback=callback, auto_ack=True)
channel.start_consuming()



#
#while True:
#    # 90% de chance d'avoir du trafic normal, 10% d'avoir une anomalie (ex: pic de trafic ou latence)
	
#    # random.random() donne un nombre entre 0 et 1 :
#    # random.random() > 0.1 : Dans ~90 % des cas : on génère une mesure normale.

#    if random.random() > 0.1:
#        metrics = np.random.normal(loc=[100, 1, 20], scale=[10, 0.5, 2]).reshape(1, -1)
#    else:
#        # Simulation d'une anomalie réseau (Ex: DDoS -> gros volume(entre 500, 1000), forte latence(80, 200))
#        metrics = np.array([[random.randint(500, 1000), random.randint(5, 20), random.randint(80, 200)]])

    
#    # Prédiction de l'IA (1 = normal, -1 = anomalie)
#    prediction = clf.predict(metrics)[0]
    
#   # Log du résultat
#    status = "🚨 ANOMALIE DÉTECTÉE" if prediction == -1 else "🟢 Trafic Normal"
#    print(f"[{status}] Métriques analysées -> Paquets: {metrics[0][0]:.1f}/s, Erreurs: {metrics[0][1]:.2f}%, Latence: {metrics[0][2]:.1f}ms")
#    
#    time.sleep(1) # Analyse toutes les secondes


