import json
import os
import random
import sys
import time
from collections import deque

import pika

from amqp_tls import build_params

AMQP_URL = os.environ.get('RABBITMQ_URL', 'amqp://user:password@localhost:5672/%2F')
QUEUE = 'telecom_traffic'
# Débit de production en messages par seconde (1 par défaut)
RATE = float(os.environ.get('RATE', '1'))
# Taille max du tampon local qui accumule les messages pendant une panne du broker.
# À dimensionner : RATE x durée de la panne (ex. 50 msg/s x 60 s = 3000).
BUFFER_MAX = int(os.environ.get('BUFFER_MAX', '5000'))
# RESET_QUEUE=1 supprime la file au démarrage (à n'utiliser que ponctuellement)
RESET_QUEUE = os.environ.get('RESET_QUEUE') == '1'
TICK = 0.2  # pas de la boucle principale, en secondes

params = build_params(AMQP_URL)  # v5 : amqps:// + certificat client si AMQP_CA_FILE / AMQP_CERT_FILE / AMQP_KEY_FILE
params.socket_timeout = 3  # une tentative de connexion ne doit pas bloquer la boucle d'envoi

print(f"🚀 Simulateur d'équipement réseau (v5 - reconnexion + tampon local + TLS) : {RATE} msg/s, tampon {BUFFER_MAX}...")


def try_connect(reset=False):
    """Une seule tentative de connexion. Retourne (connection, channel) ou (None, None)."""
    try:
        connection = pika.BlockingConnection(params)
        channel = connection.channel()
        if reset:
            print("🧹 Suppression de la file (RESET_QUEUE=1)...")
            channel.queue_delete(queue=QUEUE)
        # Déclaration idempotente : file DURABLE, conservée sur le PVC du broker
        channel.queue_declare(queue=QUEUE, durable=True)
        # Le broker confirme chaque message : on ne retire du tampon que ce qu'il a bien reçu
        channel.confirm_delivery()
        return connection, channel
    except Exception as e:
        print(f"⏳ Broker injoignable : {e}")
        return None, None


def make_message():
    if random.random() > 0.15:
        # Trafic normal : même référence que l'entraînement de l'IA (app.py : 100 / 1 / 20)
        return {
            "source": "Antenne_Elancourt_01",
            "packets": int(random.normalvariate(100, 10)),
            "errors": round(random.normalvariate(1, 0.2), 2),
            "latency": int(random.normalvariate(20, 2))
        }
    return {
        "source": "Antenne_Elancourt_01",
        "packets": random.randint(500, 800),
        "errors": random.randint(8, 15),
        "latency": random.randint(90, 150)
    }


# Connexion initiale : on abandonne après 15 tentatives, Kubernetes relancera le pod
connection, channel = None, None
for i in range(15):
    print(f"⏳ Connexion à RabbitMQ (tentative {i+1}/15)...")
    connection, channel = try_connect(reset=RESET_QUEUE)
    if connection:
        break
    time.sleep(2)

if not connection:
    print("❌ Impossible de se connecter à RabbitMQ après plusieurs tentatives.")
    sys.exit(1)

print("🟢 Connecté avec succès à RabbitMQ. Début de l'envoi du flux persistant...")

# Tampon local : pendant une panne, les messages s'accumulent ici,
# puis sont envoyés d'un coup au retour du broker (le « pic de rattrapage »).
buffer = deque(maxlen=BUFFER_MAX)
dropped = 0  # messages perdus car le tampon était plein

# La production suit l'horloge (pas la boucle) : le débit reste exact même si
# une tentative de reconnexion bloque quelques secondes.
last_tick = time.monotonic()
last_attempt = last_tick
last_log = last_tick
sent_in_window = 0
catchup_threshold = int(RATE * TICK * 3) + 2  # au-delà : c'est un rattrapage, pas le flux normal

while True:
    now = time.monotonic()

    # 1. Production des messages dus depuis le dernier passage
    due = int((now - last_tick) * RATE)
    if due > 0:
        last_tick += due / RATE
        for _ in range(due):
            if len(buffer) == BUFFER_MAX:
                dropped += 1
            buffer.append(make_message())

    # 2. Broker perdu : une tentative de reconnexion par seconde
    if connection is None and now - last_attempt >= 1.0:
        last_attempt = now
        connection, channel = try_connect()
        if connection:
            print(f"🔁 Reconnecté. Rattrapage de {len(buffer)} message(s) accumulé(s)"
                  + (f" ({dropped} perdu(s), tampon plein)." if dropped else "."))
            dropped = 0

    # 3. Envoi du tampon
    if connection is not None and buffer:
        try:
            backlog = len(buffer)
            while buffer:
                msg = buffer[0]
                channel.basic_publish(
                    exchange='',
                    routing_key=QUEUE,
                    body=json.dumps(msg),
                    properties=pika.BasicProperties(delivery_mode=2)  # écriture sur disque
                )
                buffer.popleft()
                sent_in_window += 1
                if RATE <= 5:
                    print(f"📡 Métrique persistante envoyée : {msg}")
            if backlog > catchup_threshold:
                print(f"📦 {backlog} métriques envoyées d'un coup (rattrapage).")
        except Exception as e:
            print(f"⚠️ Connexion perdue ({e}). {len(buffer)} message(s) conservé(s) en tampon.")
            try:
                connection.close()
            except Exception:
                pass
            connection, channel = None, None
            last_attempt = time.monotonic()

    # 4. Résumé par seconde quand le débit est trop élevé pour un log par message
    if RATE > 5 and now - last_log >= 1.0:
        print(f"📡 {sent_in_window} métriques envoyées sur la dernière seconde (tampon : {len(buffer)}).")
        sent_in_window = 0
        last_log = now

    # 5. Pause (sleep() de pika traite aussi les heartbeats AMQP)
    if connection is not None:
        try:
            connection.sleep(TICK)
        except Exception as e:
            print(f"⚠️ Connexion perdue pendant la pause ({e}). {len(buffer)} message(s) conservé(s) en tampon.")
            connection, channel = None, None
            last_attempt = time.monotonic()
    else:
        time.sleep(TICK)
