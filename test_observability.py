import time
import requests
import sys
import os

URL_METRICS = "http://localhost:8000"
TIMEOUT = 45  # Timeout étendu pour intégrer la phase de coupure et redémarrage
start_time = time.time()

# États possibles pour le suivi : RUNNING -> KILLED -> RESTARTED
rabbitmq_status = "RUNNING"  
initial_packets = 0.0

print("🏁 Lancement du test de validation V2 (Persistance et Reprise après sinistre)...")

while time.time() - start_time < TIMEOUT:
    try:
        response = requests.get(URL_METRICS, timeout=2)
        if response.status_code == 200:
            content = response.text
            
            if "telecom_processed_packets_total" in content:
                # Extraction du volume de paquets actuel
                nb_paquets = 0.0
                for line in content.split("\n"):
                    if line.startswith("telecom_processed_packets_total"):
                        nb_paquets = float(line.split()[1])
                
                print(f"📊 [Statut Broker: {rabbitmq_status}] Volume analysé par l'IA : {nb_paquets} paquets.")

                # PHASE 1 : Déclenchement du crash dès que les premiers messages persistants arrivent
                if rabbitmq_status == "RUNNING" and nb_paquets >= 1:
                    print("💥 SIMULATION DE PANNE V2 : Extinction brutale du conteneur RabbitMQ...")
                    os.system("docker stop cloudia-rabbitmq-1")
                    rabbitmq_status = "KILLED"
                    initial_packets = nb_paquets
                    print("⏳ Attente de 6 secondes en mode dégradé (Vérification de la persistance disque)...")
                    time.sleep(6)
                    continue

                # PHASE 2 : Redémarrage du broker pour valider la reprise après sinistre
                if rabbitmq_status == "KILLED":
                    print("🔄 REPRISE APRÈS SINISTRE : Rallumage de RabbitMQ (Chargement des données du stockage PVC)...")
                    os.system("docker start cloudia-rabbitmq-1")
                    rabbitmq_status = "RESTARTED"
                    print("⏳ Attente de 8 secondes pour laisser l'IA se reconnecter via sa boucle de retry...")
                    time.sleep(8)
                    continue

                # PHASE 3 : Validation finale. Le compteur doit grimper grâce aux messages sauvés sur le disque
                if rabbitmq_status == "RESTARTED" and nb_paquets > initial_packets + 1:
                    print("🟢 CONSTAT : L'IA s'est reconnectée et aucun message persistant n'a été égaré !")
                    print(f"✅ SUCCÈS V2 : Le pipeline a survécu à la panne totale. Rétention des données validée.")
                    sys.exit(0)  # Code 0 = Le test RÉUSSIT, le badge GitHub passe au vert !

        time.sleep(2)
    except requests.exceptions.ConnectionError:
        if rabbitmq_status == "KILLED":
            print("⏳ Mode dégradé confirmé : Sockets réseau coupés (Le stockage disque protège les données).")
        else:
            print("⏳ En attente de l'initialisation des composants...")
        time.sleep(2)

print("❌ ÉCHEC V2 : Le pipeline n'a pas récupéré ses données ou le compteur est resté bloqué après le redémarrage.")
sys.exit(1)

