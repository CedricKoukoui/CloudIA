import time
import requests
import sys
import os

URL_METRICS = "http://localhost:8000"
TIMEOUT = 30  # Timeout étendu pour observer l'impact du crash
start_time = time.time()
rabbitmq_killed = False

print("🏁 Lancement du test de limite V1 (Simulation de panne totale)...")

while time.time() - start_time < TIMEOUT:
    try:
        response = requests.get(URL_METRICS, timeout=2)
        if response.status_code == 200:
            content = response.text
            
            if "telecom_processed_packets_total" in content:
                print("🟢 Serveur de métriques détecté.")
                
                # Extraction du volume de paquets actuel
                nb_paquets = 0.0
                for line in content.split("\n"):
                    if line.startswith("telecom_processed_packets_total"):
                        nb_paquets = float(line.split()[1])
                        print(f"📊 Volume analysé par l'IA : {nb_paquets} paquets.")
                
                # 💥 DÉCLENCHEMENT DE LA PANNE : Dès qu'on voit que l'IA a commencé à bosser
                if nb_paquets >= 1 and not rabbitmq_killed:
                    print("💥 CRASH SIMULÉ : Extinction brutale du conteneur RabbitMQ (RAM jetée)...")
                    # Coupe brutalement le broker de la CI
                    os.system("docker stop cloudia-rabbitmq-1") 
                    rabbitmq_killed = True
                    print("⏳ Attente de 5 secondes pour analyser l'état post-crash...")
                    time.sleep(5)
                    continue
                
                # Si le broker a été tué et qu'on essaie de lire la suite
                if rabbitmq_killed:
                    print("❌ CONSTAT : Le broker est mort, la file volatile en RAM a disparu.")
                    print(f"📉 Le compteur est bloqué à {nb_paquets} paquets. Les messages en transit sont perdus.")
                    print("❌ PROUVE DE LIMITE V1 : Échec du test d'intégration face à la panne.")
                    sys.exit(1)  # Fait exploser le pipeline GitHub Actions pour valider la limite
                            
        time.sleep(2)
    except requests.exceptions.ConnectionError:
        if not rabbitmq_killed:
            print("⏳ En attente de l'initialisation des briques réseau et de l'IA...")
        else:
            print("💀 Alerte : Perte de connexion réseau induite par le crash du broker.")
            print("❌ PROUVE DE LIMITE V1 : Le pipeline ne survit pas à l'interruption.")
            sys.exit(1)
        time.sleep(2)

print("❌ ÉCHEC : Le pipeline a expiré ou a crashé de manière non gérée.")
sys.exit(1)

