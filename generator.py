import time
import json
import random
import pika
import os
import sys

AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
params = pika.URLParameters(AMQP_URL)

print("🚀 Initialisation du simulateur d'équipement réseau (Version v2 - Messages Persistants)...")

connection = None
for i in range(15):
    try:
        connection = pika.BlockingConnection(params)
        break
    except pika.exceptions.AMQPConnectionError:
        print(f"⏳ RabbitMQ n'est pas encore prêt (Tentative {i+1}/15)... Attente de 2s")
        time.sleep(2)

if not connection:
    print("❌ Impossible de se connecter à RabbitMQ après plusieurs tentatives.")
    sys.exit(1)

channel = connection.channel()

# SOLUTION v2 : On déclare la file d'attente comme DURABLE (surit à l'arrêt du broker)
channel.queue_declare(queue='telecom_traffic', durable=True)
print("🟢 Connecté avec succès à RabbitMQ. Début de l'envoi du flux persistant...")

while True:
    if random.random() > 0.15:
        data = {
            "source": "Antenne_Elancourt_01",
            "packets": int(random.normalvariate(100, 10)),
            "errors": round(random.normalvariate(1, 0.2), 2),
            "latency": int(random.normalvariate(10, 2))
        }
    else:
        data = {
            "source": "Antenne_Elancourt_01",
            "packets": random.randint(500, 800),
            "errors": random.randint(8, 15),
            "latency": random.randint(90, 150)
        }
    
    try:
        # SOLUTION v2 : Ajout de delivery_mode=2 pour forcer l'écriture immédiate sur le disque
        channel.basic_publish(
            exchange='', 
            routing_key='telecom_traffic', 
            body=json.dumps(data),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        print(f"📡 Métrique persistante envoyée : {data}")
    except Exception as e:
        print(f"⚠️ Erreur lors de l'envoi : {e}")
    time.sleep(1)

