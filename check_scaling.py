"""Contrôles statiques du passage à l'échelle de la v6 (sans cluster). Utilisé par la CI ; lançable en local :
    python check_scaling.py
Échoue (code 1) si une règle de la v6 n'est plus respectée par les fichiers du dépôt.
"""
import ast
import re
import sys

import yaml

errors = []


def load(path):
    with open(path, encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def check(cond, message):
    print(("  OK     " if cond else "  ÉCHEC  ") + message)
    if not cond:
        errors.append(message)


def cpu(v):
    """CPU en millicœurs (entiers : pas d'erreur d'arrondi, 6 x 100m = 600m exactement)."""
    v = str(v)
    return int(v[:-1]) if v.endswith("m") else int(float(v) * 1000)


def mem(v):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi)", str(v))
    return float(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


print("== Le piège de la v5 : aucun entraînement dans les pods de l'IA")
tree = ast.parse(open("app.py", encoding="utf-8").read())
calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
check("fit" not in calls, "app.py n'appelle jamais .fit() (pas d'entraînement local)")
check("IsolationForest" not in names and "sklearn" not in names, "app.py n'importe pas scikit-learn pour entraîner")
check("load_production_model" in names, "app.py charge le modèle validé du registre (model_registry.load_production_model)")
lib = open("model_lib.py", encoding="utf-8").read()
check("default_rng(seed)" in lib, "model_lib.py : données d'entraînement reproductibles (graine fixée)")

print("== Deployment de l'IA : nombre de pods laissé à l'autoscaler, mise à jour progressive")
dep = load("deployment.yaml")[0]
spec = dep["spec"]
pod = spec["template"]["spec"]
c = pod["containers"][0]
env = {e["name"]: e.get("value") for e in c["env"]}
check("replicas" not in spec, "deployment.yaml : pas de champ replicas (sinon kubectl apply écrase la décision de l'autoscaler)")
check(spec["strategy"]["type"] == "RollingUpdate" and spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 0,
      "deployment.yaml : RollingUpdate sans indisponibilité (maxUnavailable 0)")
surge = spec["strategy"]["rollingUpdate"]["maxSurge"]
check("readinessProbe" in c and "exec" in c["readinessProbe"], "deployment.yaml : sonde de disponibilité exec (modèle chargé)")
check(env.get("MLFLOW_TRACKING_URI", "").endswith("mlflow.mlops.svc.cluster.local:5000"), "deployment.yaml : registre MLflow configuré")
check(env.get("MODEL_ALIAS") == "production", "deployment.yaml : l'IA charge l'alias « production »")

print("== Autoscaler (KEDA) cohérent avec le quota du namespace ia")
docs = load("10-autoscaling.yaml")
so = next(d for d in docs if d["kind"] == "ScaledObject")
pm = next(d for d in docs if d["kind"] == "PodMonitor")
s = so["spec"]
check(s["scaleTargetRef"]["name"] == dep["metadata"]["name"], "ScaledObject : cible le Deployment de l'IA")
check(s["minReplicaCount"] >= 1, "ScaledObject : jamais 0 pod (minReplicaCount >= 1)")
quota = next(d for d in load("01-namespaces-quotas.yaml") if d["kind"] == "ResourceQuota" and d["metadata"]["namespace"] == "ia")
hard = quota["spec"]["hard"]
need = s["maxReplicaCount"] + surge
check(int(hard["pods"]) >= need, f"quota ia : {hard['pods']} pods >= maxReplicaCount + maxSurge ({need})")
res = c["resources"]
for key, conv, val in (("requests.cpu", cpu, res["requests"]["cpu"]), ("requests.memory", mem, res["requests"]["memory"]),
                       ("limits.cpu", cpu, res["limits"]["cpu"]), ("limits.memory", mem, res["limits"]["memory"])):
    check(conv(hard[key]) >= need * conv(val), f"quota ia : {key} {hard[key]} >= {need} x {val}")
triggers = {t["name"]: t for t in s["triggers"]}
check(set(triggers) == {"backlog", "throughput"}, "ScaledObject : deux métriques (retard de la file, débit traité)")
check("rabbitmq_detailed_queue_messages_ready" in triggers["backlog"]["metadata"]["query"], "trigger backlog : messages en attente dans RabbitMQ")
check("telecom_processed_packets_total" in triggers["throughput"]["metadata"]["query"], "trigger throughput : débit traité par l'IA")
check(all(t["metadata"]["serverAddress"].startswith("http://prometheus-operated.default.svc:9090") for t in s["triggers"]),
      "triggers : Prometheus de la pile v4 (Service prometheus-operated)")
check(s.get("fallback", {}).get("replicas", 0) >= 1, "ScaledObject : repli défini si Prometheus ne répond plus")
ep = pm["spec"]["podMetricsEndpoints"][0]
check(pm["metadata"]["namespace"] == "ingestion" and ep["path"] == "/metrics/detailed"
      and ep["params"]["family"] == ["queue_coarse_metrics"], "PodMonitor RabbitMQ : métriques par file (queue_coarse_metrics)")
rmq = next(d for d in load("rabbitmq-v3-persistant.yaml") if d["kind"] == "StatefulSet")
ports = {p.get("name"): p["containerPort"] for p in rmq["spec"]["template"]["spec"]["containers"][0]["ports"]}
check(ports.get("prometheus") == 15692, "RabbitMQ : port 15692 nommé « prometheus » (cible du PodMonitor)")

print("== Registre de modèles et entraînement")
mlops = load("08-mlops.yaml")
ml = next(d for d in mlops if d["kind"] == "Deployment")
args = " ".join(ml["spec"]["template"]["spec"]["containers"][0]["args"])
check("mlflow.mlops.svc.cluster.local" in args, "MLflow : --allowed-hosts contient le nom DNS du Service (sinon HTTP 403)")
check("--workers=1" in args, "MLflow : un seul processus de service (mémoire)")
ml_env = {e["name"]: e["value"] for e in ml["spec"]["template"]["spec"]["containers"][0]["env"]}
check(ml_env.get("MLFLOW_SERVER_ENABLE_JOB_EXECUTION") == "false", "MLflow : tâches de fond désactivées (mémoire)")
req = open("requirements.txt", encoding="utf-8").read()
client_version = re.search(r"mlflow-skinny==([\d.]+)", req).group(1)
image = ml["spec"]["template"]["spec"]["containers"][0]["image"]
check(image.endswith(f":v{client_version}"), f"MLflow : même version côté serveur ({image}) et client ({client_version})")
job = load("09-train-job.yaml")[0]
check(job["kind"] == "Job" and "generateName" in job["metadata"] and job["spec"]["backoffLimit"] == 0,
      "Job d'entraînement : un nouveau Job par lancement, pas de nouvel essai d'un modèle refusé")

print()
if errors:
    print(f"{len(errors)} règle(s) non respectée(s).")
    sys.exit(1)
print("Toutes les règles du passage à l'échelle sont respectées.")
