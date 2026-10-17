import time
import requests
import sys
import os
import subprocess

# Deux modes de test (variable TEST_MODE) :
#   k8s     (défaut) : cluster Kind. Tunnel kubectl vers l'IA, suppression du pod RabbitMQ,
#                      contrôle du compteur RESTARTS de l'IA.
#   compose           : stack docker-compose.test.yml (jusqu'à Grafana). SIGKILL du conteneur
#                      RabbitMQ puis redémarrage, contrôle que le conteneur de l'IA n'a pas
#                      redémarré, et contrôle de la chaîne Prometheus -> Grafana avant et après.
MODE = os.environ.get("TEST_MODE", "k8s")
if MODE not in ("k8s", "compose"):
    sys.exit(f"❌ TEST_MODE inconnu : {MODE!r} (attendu : k8s ou compose)")

COMPOSE_FILE = os.environ.get("COMPOSE_FILE", "docker-compose.test.yml")
URL_METRICS = os.environ.get("URL_METRICS", "http://127.0.0.1:8000")
URL_PROMETHEUS = os.environ.get("URL_PROMETHEUS", "http://127.0.0.1:9090")
URL_GRAFANA = os.environ.get("URL_GRAFANA", "http://127.0.0.1:3000")
GRAFANA_AUTH = (os.environ.get("GRAFANA_USER", "admin"), os.environ.get("GRAFANA_PASSWORD", "admin"))
GRAFANA_DATASOURCE_UID = "prometheus"   # défini dans observability/grafana-datasource.yml
GRAFANA_DASHBOARD_UID = "cloudia-v3"    # défini dans observability/cloudia-v3-dashboard.json
# Requête exécutée À TRAVERS Grafana : prouve que la chaîne IA -> Prometheus -> Grafana fonctionne
COUNTER_QUERY = "sum(telecom_processed_packets_total)"

# TEST_TIMEOUT : durée maximale de la phase de crash-test (la CI est plus lente qu'un poste local)
TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "60" if MODE == "k8s" else "150"))
# STACK_TIMEOUT : durée maximale pour que Prometheus et Grafana voient les métriques (mode compose)
STACK_TIMEOUT = int(os.environ.get("STACK_TIMEOUT", "90"))

rabbitmq_status = "RUNNING"
initial_packets = 0.0
initial_marker = None
grafana_before_crash = None
tunnel_proc = None


# ---------------------------------------------------------------------------
# Accès à l'environnement (k8s ou compose)
# ---------------------------------------------------------------------------
def compose(*args):
    """Lance une commande docker compose sur le fichier de test."""
    return subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, *args],
                          capture_output=True, text=True, timeout=120)


def get_ia_marker():
    """Marqueur de redémarrage de l'IA, relevé avant puis après la panne (None si illisible).

    k8s     : compteur RESTARTS du conteneur de l'IA.
    compose : état, date de démarrage et RestartCount du conteneur (changent s'il a redémarré).
    """
    try:
        if MODE == "compose":
            cid = compose("ps", "-q", "ia-analyzer").stdout.strip()
            if not cid:
                return None
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}} {{.State.StartedAt}} {{.RestartCount}}", cid],
                capture_output=True, text=True, timeout=10
            ).stdout.strip()
            return out or None
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", "ia", "-l", "app=telecom-ia",
             "-o", "jsonpath={.items[0].status.containerStatuses[0].restartCount}"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip()
        return int(out)
    except Exception:
        return None


def kill_broker():
    """Supprime brutalement le broker."""
    if MODE == "compose":
        print("💥 SIMULATION DE PANNE : SIGKILL du conteneur RabbitMQ (docker compose kill)...")
        r = compose("kill", "rabbitmq")
        if r.returncode != 0:
            print(f"⚠️ docker compose kill a échoué : {r.stderr.strip()}")
    else:
        print("💥 SIMULATION DE PANNE V3 : Suppression brutale du Pod RabbitMQ...")
        os.system("kubectl delete pod -l app=rabbitmq -n ingestion --grace-period=0 --force")


def restore_broker():
    """Remet le broker en service. Sur k8s, le StatefulSet le recrée tout seul : rien à faire."""
    if MODE == "compose":
        print("🔄 REPRISE : redémarrage du conteneur RabbitMQ (même volume, file durable conservée)...")
        r = compose("start", "rabbitmq")
        if r.returncode != 0:
            print(f"⚠️ docker compose start a échoué : {r.stderr.strip()}")


def start_tunnel():
    """Ouvre ou réouvre le tunnel réseau IPv4 à chaud (k8s uniquement : Compose publie les ports)."""
    global tunnel_proc
    if MODE != "k8s":
        return
    if tunnel_proc:
        try:
            tunnel_proc.terminate()
        except Exception:
            pass
    tunnel_proc = subprocess.Popen(
        ["kubectl", "port-forward", "svc/telecom-ia-service", "8000:8000", "-n", "ia", "--address", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)


def stop_tunnel():
    if tunnel_proc:
        try:
            tunnel_proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Contrôle de la chaîne jusqu'à Grafana (mode compose)
# ---------------------------------------------------------------------------
def grafana_counter():
    """Valeur de COUNTER_QUERY lue À TRAVERS Grafana (source de données Prometheus). None si absente."""
    r = requests.get(
        f"{URL_GRAFANA}/api/datasources/proxy/uid/{GRAFANA_DATASOURCE_UID}/api/v1/query",
        params={"query": COUNTER_QUERY}, auth=GRAFANA_AUTH, timeout=5)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    return float(result[0]["value"][1]) if result else None


def grafana_counter_above(threshold):
    """Valeur du compteur lue dans Grafana si elle dépasse le seuil, sinon None (jamais d'exception)."""
    try:
        value = grafana_counter()
    except Exception:
        return None
    return value if value is not None and value > threshold else None


def prometheus_target_up():
    """True si Prometheus voit la cible telecom-ia en état UP."""
    r = requests.get(f"{URL_PROMETHEUS}/api/v1/query",
                     params={"query": 'up{job="telecom-ia"}'}, timeout=5)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    return bool(result) and result[0]["value"][1] == "1"


def prometheus_model_from_registry():
    """v6 : l'IA expose la version du modèle téléchargé depuis le registre (telecom_model_info)."""
    r = requests.get(f"{URL_PROMETHEUS}/api/v1/query", params={"query": "telecom_model_info"}, timeout=5)
    r.raise_for_status()
    result = r.json()["data"]["result"]
    return bool(result) and result[0]["metric"].get("model_version")


def grafana_health_ok():
    r = requests.get(f"{URL_GRAFANA}/api/health", timeout=5)
    r.raise_for_status()
    return r.json().get("database") == "ok"


def grafana_datasource_ok():
    r = requests.get(f"{URL_GRAFANA}/api/datasources/uid/{GRAFANA_DATASOURCE_UID}", auth=GRAFANA_AUTH, timeout=5)
    r.raise_for_status()
    return r.json().get("type") == "prometheus"


def grafana_dashboard_ok():
    r = requests.get(f"{URL_GRAFANA}/api/dashboards/uid/{GRAFANA_DASHBOARD_UID}", auth=GRAFANA_AUTH, timeout=5)
    r.raise_for_status()
    return bool(r.json().get("dashboard", {}).get("panels"))


def wait_for(label, check, timeout):
    """Appelle check() toutes les 3 s jusqu'à ce qu'elle renvoie une valeur vraie. None si le délai est dépassé."""
    deadline = time.time() + timeout
    last_error = None
    while True:
        try:
            value = check()
            if value:
                return value
        except Exception as e:
            last_error = e
        if time.time() >= deadline:
            detail = f" (dernière erreur : {last_error})" if last_error else ""
            print(f"❌ ÉCHEC : {label} : délai de {timeout} s dépassé{detail}")
            return None
        time.sleep(3)


def check_stack_to_grafana():
    """Vérifie toute la chaîne jusqu'à Grafana. Retourne la valeur du compteur lue dans Grafana, ou None."""
    print("🔎 Contrôle de la chaîne jusqu'à Grafana...")
    steps = [
        ("Prometheus collecte l'IA (up{job=\"telecom-ia\"} = 1)", prometheus_target_up),
        ("L'IA sert le modèle validé du registre (telecom_model_info, v6)", prometheus_model_from_registry),
        ("Grafana répond (/api/health, base de données ok)", grafana_health_ok),
        ("Source de données Prometheus provisionnée dans Grafana", grafana_datasource_ok),
        (f"Tableau de bord '{GRAFANA_DASHBOARD_UID}' provisionné dans Grafana", grafana_dashboard_ok),
    ]
    for label, check in steps:
        if not wait_for(label, check, STACK_TIMEOUT):
            return None
        print(f"   ✅ {label}")
    value = wait_for(f"Grafana restitue {COUNTER_QUERY} > 0", lambda: grafana_counter_above(0), STACK_TIMEOUT)
    if value:
        print(f"   ✅ Grafana restitue les données de l'IA : {COUNTER_QUERY} = {value}")
    return value


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------
print(f"🏁 Lancement du test de validation V3 Résilient (mode {MODE})...")

if MODE == "compose":
    # Avant la panne : toute la chaîne doit déjà fonctionner jusqu'à Grafana
    if check_stack_to_grafana() is None:
        sys.exit(1)

start_tunnel()
start_time = time.time()

try:
    while time.time() - start_time < TIMEOUT:
        try:
            response = requests.get(URL_METRICS, timeout=2)
            if response.status_code == 200:
                content = response.text

                if "telecom_processed_packets_total" in content:
                    nb_paquets = 0.0
                    for line in content.split("\n"):
                        if line.startswith("telecom_processed_packets_total") and not line.startswith("telecom_processed_packets_total_"):
                            parts = line.split()
                            if len(parts) >= 2:
                                nb_paquets = float(parts[1])

                    print(f"📊 [Statut Broker: {rabbitmq_status}] Volume analysé par l'IA : {nb_paquets} paquets.")

                    # PHASE 1 : Déclenchement du crash
                    if rabbitmq_status == "RUNNING" and nb_paquets >= 1:
                        initial_marker = get_ia_marker()
                        print(f"📌 Marqueur de redémarrage de l'IA avant la panne : {initial_marker}")
                        if MODE == "compose":
                            grafana_before_crash = grafana_counter_above(0)
                            print(f"📌 Compteur lu dans Grafana avant la panne : {grafana_before_crash}")
                        kill_broker()
                        rabbitmq_status = "KILLED"
                        initial_packets = nb_paquets
                        print("⏳ Attente de 12 secondes (Le Générateur accumule, l'IA temporise)...")
                        time.sleep(12)
                        continue

                    # PHASE 2 : Constat et remise en service (broker en compose, tunnel en k8s)
                    if rabbitmq_status == "KILLED":
                        restore_broker()
                        if MODE == "k8s":
                            print("🔄 REPRISE APRÈS SINISTRE : Ré-instanciation dynamique du tunnel réseau...")
                        start_tunnel()
                        rabbitmq_status = "RESTARTED"
                        continue

                    # PHASE 3 : Validation finale (le flux reprend ET l'IA n'a pas redémarré)
                    if rabbitmq_status == "RESTARTED" and nb_paquets > initial_packets + 1:
                        marker_after = get_ia_marker()
                        print(f"📌 Marqueur de redémarrage de l'IA après la panne : {marker_after}")
                        if None in (initial_marker, marker_after):
                            stop_tunnel()
                            print("❌ ÉCHEC V3 : impossible de lire le marqueur de redémarrage de l'IA.")
                            sys.exit(1)
                        if marker_after != initial_marker:
                            stop_tunnel()
                            print("❌ ÉCHEC V3 : l'IA a redémarré pendant la panne.")
                            sys.exit(1)

                        if MODE == "compose":
                            # Le flux repris doit aussi être visible dans Grafana (la collecte est toutes les 5 s)
                            after = wait_for(
                                "Grafana voit le compteur augmenter après la panne",
                                lambda: grafana_counter_above(grafana_before_crash or 0),
                                60)
                            if not after:
                                sys.exit(1)
                            print(f"   ✅ Compteur lu dans Grafana après la panne : {after} (avant : {grafana_before_crash})")
                            print(f"   👉 Tableau de bord : {URL_GRAFANA}/d/{GRAFANA_DASHBOARD_UID}")

                        stop_tunnel()
                        print("🟢 CONSTAT : le flux a repris et l'IA s'est reconnectée sans redémarrer.")
                        print("✅ SUCCÈS V3 : le pipeline a survécu à la panne.")
                        sys.exit(0)

            time.sleep(2)
        except requests.exceptions.ConnectionError:
            if rabbitmq_status == "KILLED":
                print("⏳ Connexion coupée par le crash. En attente de la phase de remise en service...")
            elif MODE == "k8s":
                print("⏳ Tentative de reconnexion au endpoint des métriques...")
                start_tunnel()
            else:
                print("⏳ Métriques de l'IA injoignables, nouvel essai...")
            time.sleep(2)
        except Exception as e:
            print(f"⚠️ Erreur interne du script de test : {e}")
            time.sleep(2)

    print("❌ ÉCHEC V3 : Le pipeline n'a pas récupéré ses données.")
    stop_tunnel()
    sys.exit(1)

except KeyboardInterrupt:
    print("\n🛑 Interruption par l'utilisateur. Fermeture du tunnel...")
    stop_tunnel()
    sys.exit(1)
