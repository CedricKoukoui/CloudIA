"""Contrôles statiques de sécurité de la v5 (sans cluster). Utilisé par la CI ; lançable en local :
    python check_security.py
Échoue (code 1) si une règle de la v5 n'est plus respectée par les manifestes du dépôt.
"""
import re
import sys

import yaml

WORKLOADS = ["rabbitmq-v3-persistant.yaml", "deployment.yaml", "generator-deployment.yaml"]
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
        if doc["kind"] not in ("Deployment", "StatefulSet"):
            continue
        name = f"{path} ({doc['kind']} {doc['metadata']['name']})"
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
