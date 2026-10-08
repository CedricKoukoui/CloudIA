import time
import json
import random
import pika

# Connexion locale à RabbitMQ (pour le test direct depuis votre Mac)

# il envoie les paquets sur le port 5672
params = pika.URLParameters('amqp://user:password@localhost:5672/%2F')
connection = pika.BlockingConnection(params)

# Même liaison 'telecom_traffic' que celle declarée au niveau du récepteur (app.py) 
channel = connection.channel()
channel.queue_declare(queue='telecom_traffic')

print("🚀 Lancement du simulateur d'équipement réseau...")

while True:
    # Simulation de données

    # traffic normmal
    if random.random() > 0.15:
        data = {
            "source": "Antenne_Elancourt_01",
            "packets": int(random.normalvariate(100, 10)),
            "errors": round(random.normalvariate(1, 0.2), 2),
            "latency": int(random.normalvariate(10, 2))
        }
    else:
    # traffic anormal
        # Génération d'une anomalie
        data = {
            "source": "Antenne_Elancourt_01",
            "packets": random.randint(500, 800),
            "errors": random.randint(8, 15),
            "latency": random.randint(90, 150)
        }
    
    # Envoi au broker ".basic_publish"
    channel.basic_publish(exchange='', routing_key='telecom_traffic', body=json.dumps(data))
    print(f"📡 Métriques envoyées : {data}")
    time.sleep(1)

