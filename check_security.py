"""Contrôles statiques de sécurité de la v5, étendus aux composants de la v6 (sans cluster). Utilisé par la CI ; lançable en local :
    python check_security.py
Échoue (code 1) si une règle de la v5 n'est plus respectée par les manifestes du dépôt.
"""
import re
import sys

import yaml

WORKLOADS = ["rabbitmq-v3-persistant.yaml", "deployment.yaml", "generator-deployment.yaml",
             "08-mlops.yaml", "09-train-job.yaml"]  # v6 : registre MLflow et Job d'entraînement
errors = []


def load(path):
    with open(path, encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def check(cond, message):
    print(("  OK     " if cond else "  ÉCHEC  ") + message)
    if not cond:
        errors.append(message)


print("== Charges de travail : compte de service dédié, sans jeton, non-root, sans secret en clair")
for path in WORKLOADS:
    raw = open(path, encoding="utf-8").read()
    check(not re.search(r"://[^/\s:@$]+:[^/\s@$]+@", raw), f"{path} : aucun « utilisateur:motdepasse@ » en clair")
    for doc in load(path):
        if doc["kind"] not in ("Deployment", "StatefulSet", "Job"):
            continue
        name = f"{path} ({doc['kind']} {doc['metadata'].get('name') or doc['metadata'].get('generateName')})"
        pod = doc["spec"]["template"]["spec"]
        check(pod.get("serviceAccountName") not in (None, "default"), f"{name} : compte de service dédié")
        check(pod.get("automountServiceAccountToken") is False, f"{name} : jeton d'API non monté")
        check(pod.get("securityContext", {}).get("runAsNonRoot") is True, f"{name} : runAsNonRoot")
        for c in pod["containers"]:
            sc = c.get("securityContext", {})
            check(sc.get("allowPrivilegeEscalation") is False, f"{name}/{c['name']} : allowPrivilegeEscalation désactivé")
            check(sc.get("capabilities", {}).get("drop") == ["ALL"], f"{name}/{c['name']} : toutes les capacités retirées")
            for e in c.get("env", []):
                if re.search(r"PASS|SECRET|TOKEN", e["name"], re.I):
                    check("valueFrom" in e, f"{name}/{c['name']} : {e['name']} lu dans un Secret (pas de valeur en clair)")

print("== Réseau : refus par défaut en entrée ET en sortie, flux sortants limités")
policies = load("02-network-policy.yaml") + load("04-network-policy-observability.yaml")
by = {(p["metadata"]["namespace"], p["metadata"]["name"]): p for p in policies}
for ns in ("ia", "ingestion"):
    p = by.get((ns, "default-deny-all"))
    check(bool(p) and p["spec"]["podSelector"] == {} and set(p["spec"]["policyTypes"]) == {"Ingress", "Egress"}
          and "ingress" not in p["spec"] and "egress" not in p["spec"], f"namespace {ns} : refus par défaut entrée + sortie")
p = by.get(("default", "default-deny-ingress"))
check(bool(p) and p["spec"]["podSelector"] == {} and "Ingress" in p["spec"]["policyTypes"], "namespace default : refus par défaut en entrée")
gen = by[("ingestion", "generator-egress-to-rabbitmq")]["spec"]["egress"]
check(len(gen) == 1 and [x["port"] for x in gen[0]["ports"]] == [5671], "générateur : unique sortie = RabbitMQ:5671")
scrape = by[("ia", "allow-metrics-scrape")]["spec"]["ingress"][0]["from"][0]
check("podSelector" in scrape and "namespaceSelector" in scrape, "métriques de l'IA : source = pod Prometheus précis (et pas un namespace entier)")
amqp = by[("ingestion", "allow-amqps-to-rabbitmq")]["spec"]["ingress"][0]
check([x["port"] for x in amqp["ports"]] == [5671], "RabbitMQ : seul le port chiffré 5671 est ouvert")
check(all("podSelector" in src for src in amqp["from"]), "RabbitMQ : sources désignées par pod")

# v6 : registre de modèles, métriques de la file, autoscaler
p = by.get(("mlops", "default-deny-all"))
check(bool(p) and p["spec"]["podSelector"] == {} and set(p["spec"]["policyTypes"]) == {"Ingress", "Egress"}
      and "ingress" not in p["spec"] and "egress" not in p["spec"], "namespace mlops : refus par défaut entrée + sortie")
reg = by[("mlops", "allow-model-registry-clients")]["spec"]["ingress"][0]
check([x["port"] for x in reg["ports"]] == [5000] and all("podSelector" in src for src in reg["from"]),
      "registre MLflow : port 5000 ouvert aux seuls pods désignés (entraînement, IA)")
check(not any(src.get("podSelector", {}).get("matchLabels", {}).get("app") == "telecom-generator" for src in reg["from"]),
      "registre MLflow : le générateur (pod exposé de Q3 / Q9) ne peut pas y déposer un modèle")
tr = by[("mlops", "trainer-egress-to-model-registry")]["spec"]["egress"]
check(len(tr) == 1 and [x["port"] for x in tr[0]["ports"]] == [5000], "entraînement : unique sortie = MLflow:5000")
ia_egress = [r for (ns, _), pol in by.items() if ns == "ia" and "egress" in pol["spec"]
             and pol["spec"]["podSelector"].get("matchLabels", {}).get("app") == "telecom-ia" for r in pol["spec"]["egress"]]
check(sorted(x["port"] for r in ia_egress for x in r["ports"]) == [5000, 5671],
      "IA : sorties limitées à RabbitMQ:5671 et MLflow:5000")
mx = by[("ingestion", "allow-rabbitmq-metrics-scrape")]["spec"]["ingress"][0]
check([x["port"] for x in mx["ports"]] == [15692] and "podSelector" in mx["from"][0] and "namespaceSelector" in mx["from"][0],
      "métriques de RabbitMQ (15692) : source = pod Prometheus précis")
kp = by[("default", "allow-keda-to-prometheus")]["spec"]["ingress"][0]
check([x["port"] for x in kp["ports"]] == [9090] and "podSelector" in kp["from"][0] and "namespaceSelector" in kp["from"][0],
      "Prometheus (9090) ouvert à l'opérateur KEDA seul, pas à tout le namespace keda")

print("== RBAC : aucun droit à l'échelle du cluster, aucun droit sur les secrets")
rbac = load("05-rbac.yaml")
kinds = {d["kind"] for d in rbac}
check(not kinds & {"ClusterRole", "ClusterRoleBinding"}, "aucun ClusterRole ni ClusterRoleBinding")
for d in rbac:
    if d["kind"] == "Role":
        for rule in d["rules"]:
            check("secrets" not in rule["resources"] and "*" not in rule["resources"] and "*" not in rule["verbs"],
                  f"Role {d['metadata']['namespace']}/{d['metadata']['name']} : pas de secrets, pas de joker ({','.join(rule['resources'])})")
    if d["kind"] == "ServiceAccount":
        check(d.get("automountServiceAccountToken") is False, f"ServiceAccount {d['metadata']['name']} : jeton non monté")
# v6 : personne (hors administrateur) ne lance d'entraînement ni ne modifie l'autoscaler
for d in rbac:
    if d["kind"] == "Role" and d["metadata"]["namespace"] in ("ia", "mlops"):
        for rule in d["rules"]:
            if {"jobs", "scaledobjects", "horizontalpodautoscalers"} & set(rule["resources"]):
                check(set(rule["verbs"]) <= {"get", "list", "watch"},
                      f"Role {d['metadata']['namespace']}/{d['metadata']['name']} : lecture seule sur {','.join(rule['resources'])}")

print("== Observabilité : mot de passe Grafana en Secret, règle d'alerte Q14")
vals = yaml.safe_load(open("observability-values.yaml", encoding="utf-8"))
check("adminPassword" not in vals["grafana"], "Grafana : pas de mot de passe dans les valeurs Helm")
check(vals["grafana"].get("admin", {}).get("existingSecret") == "grafana-admin", "Grafana : mot de passe lu dans le Secret grafana-admin")
rule = load("06-alert-rules.yaml")[0]
names = [r["alert"] for g in rule["spec"]["groups"] for r in g["rules"]]
check("IaScaledToZero" in names, "règle d'alerte IaScaledToZero (IA mise à 0 réplique)")
check(rule["metadata"]["labels"].get("release") == "telecom-monitor", "règles découvertes par Prometheus (label release)")

print()
if errors:
    print(f"{len(errors)} règle(s) non respectée(s).")
    sys.exit(1)
print("Toutes les règles de sécurité sont respectées.")
