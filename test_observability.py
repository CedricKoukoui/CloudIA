import time
import requests
import sys

URL_METRICS = "http://localhost:8000"
TIMEOUT = 10  # Temps max d'attente en secondes
start_time = time.time()

print("🏁 Lancement du test d'observabilité automatique...")

while time.time() - start_time < TIMEOUT:
    try:
        response = requests.get(URL_METRICS, timeout=2)
        if response.status_code == 200:
            content = response.text
            
            # Vérification de la présence des métriques Prometheus de notre IA
            if "telecom_processed_packets_total" in content:
                print("🟢 Serveur de métriques détecté.")
                
                # Extraction de la valeur actuelle pour s'assurer que l'IA bosse
                for line in content.split("\n"):
                    if line.startswith("telecom_processed_packets_total"):
                        nb_paquets = float(line.split()[1])
                        print(f"📊 Volume analysé par l'IA : {nb_paquets} paquets.")
                        
                        if nb_paquets > 2:
                            print("✅ SUCCÈS : Le pipeline transmet les données et l'observabilité est active !")
                            sys.exit(0)  # Code 0 = Le test réussit, le pipeline GitHub passe au vert
                            
        time.sleep(2)
    except requests.exceptions.ConnectionError:
        print("⏳ En attente de l'initialisation des briques réseau et de l'IA...")
        time.sleep(2)

print("❌ ÉCHEC : Le test a expiré. L'IA ou le Broker ne répondent pas ou aucune métrique n'est générée.")
sys.exit(1)  # Code 1 = Le test échoue, le pipeline GitHub passe au rouge ("Explose")

