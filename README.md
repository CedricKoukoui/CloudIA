# 📡 CloudIA : Architecture Souveraine & IA pour la Supervision Télécom (Version v3)

[![CI](https://github.com/CedricKoukoui/CloudIA/actions/workflows/ci-cd.yaml/badge.svg?branch=v3)](https://github.com/CedricKoukoui/CloudIA/actions/workflows/ci-cd.yaml)

> 📊 **Note d'architecture :** Le schéma détaillé de cette infrastructure Cloud-Native est disponible au format vectoriel dans le fichier `schema-architecture.pdf` et au format image dans `schema-architecture-v3.png` à la racine de ce dépôt.

Ce projet implémente un prototype d'architecture Cloud-Native, asynchrone (Event-Driven) et monitorée, dédiée à la détection d'anomalies quasi temps réel sur des flux de télécommunications.

La version **v3** ajoute l'isolation des ressources entre les composants (cgroups, Namespaces, ResourceQuotas, NetworkPolicy, cluster multi-nœuds avec `nodeSelector`) **et** la résilience applicative qui permet de mesurer cette isolation lors d'une panne du broker.

Ce document explique le problème, la démarche de résolution, l'implémentation, **le choix de chaque valeur** et les mesures qui les justifient. Les chiffres cités ont été relevés sur le cluster de test décrit en section 8 (un seul passage par scénario : ce sont des ordres de grandeur, pas des statistiques).

---

## 1. bref 
| | |
|---|---|
| **Problème** | Après une panne du broker, l'IA recevait d'un coup tout le backlog accumulé, en concurrence avec les autres composants de la machine hôte. |
| **Solution** | Plafonner chaque composant (cgroups), le compartimenter (Namespaces, ResourceQuotas, NetworkPolicy), le placer sur un nœud dédié (`nodeSelector`), et rendre le générateur et l'IA capables de se reconnecter seuls. |
| **Preuve mesurée** | Panne de 3 min à 100 msg/s (≈ 18 000 messages en attente). Au retour du broker, la CPU de l'IA plafonne à sa limite (0,5 cœur), le throttling approche 100 % pendant ≈ 1 min, le débit de traitement monte à 390-555 msg/s (contre 100 en régime normal) et la mémoire reste ≤ 134 Mo (≈ 25 % de la limite). La consommation de l'IA ne dépasse jamais sa limite. |
| **Résilience** | Après correction, l'IA ne redémarre plus pendant une panne du broker d'environ 3 min (`RESTARTS = 0`), et le générateur ne perd aucun message tant que son tampon ne déborde pas. |
| **Sonde sans cluster** | `docker-compose.test.yml` monte toute la chaîne **jusqu'à Grafana** (générateur, RabbitMQ, IA, Prometheus, Grafana avec tableau de bord provisionné), avec les mêmes limites CPU/RAM que Kubernetes, puis tue le broker et vérifie que la courbe Grafana reprend. Lancée par la CI à chaque push (job `e2e`, section 10) ; guide en section 11, étape 12. |

---

## 2. Le problème (hérité de la version v2bis)

### Le constat

Dans la v2bis, le générateur tourne dans le cluster. Lors du crash-test (suppression du pod RabbitMQ), le flux continue : les messages s'accumulent côté générateur, puis l'IA reprend le traitement au retour du broker.

Un symptôme apparaît pourtant au redémarrage du broker :

1. L'IA reçoit d'un coup la masse de messages accumulés et les traite en une fraction de seconde.
2. Ce rattrapage provoque un **pic de consommation CPU et mémoire**, qui entre en concurrence avec les autres composants (RabbitMQ, Prometheus, Grafana) et avec la machine qui héberge le cluster.
3. Kubernetes arrête le conteneur de l'IA lorsqu'il dépasse son plafond mémoire, ce qui le place transitoirement en `CrashLoopBackOff`.

Pour confirmer ce type de cause sur un cluster : `kubectl describe pod <pod-ia> | grep -A3 "Last State"` affiche `OOMKilled` si la mémoire est en cause. (Dans toutes les mesures de la v3, aucun `OOMKilled` n'a été observé : la mémoire de l'IA n'a jamais dépassé 134 Mo.)

### Les causes techniques

| # | Cause | Mécanisme | Conséquence |
|---|---|---|---|
| 1 | Pas de lissage du rattrapage | L'IA consomme tout le backlog le plus vite possible | Pic de CPU et de RAM |
| 2 | Aucune isolation entre composants | Tous les pods partagent le même espace, sans quota global | Un composant en pic peut priver les autres de ressources |
| 3 | Broker non sanctuarisé | RabbitMQ lancé par `kubectl create deployment` (sans requests/limits, sans PVC) | Pas de CPU garanti, et la persistance de la v2 est perdue |
| 4 | Un seul nœud | Tous les pods sur le même nœud Kind | Aucune séparation entre calcul (IA) et ingestion (broker) |

### Deux ressources, deux comportements

*   **Le CPU est compressible :** au-delà de sa `limit`, un conteneur est **bridé** (*CPU throttling*). Il ralentit mais continue de tourner.
*   **La mémoire ne l'est pas :** au-delà de sa `limit`, le conteneur est **arrêté** (`OOMKilled`). La limite mémoire doit donc être dimensionnée pour le pic de rattrapage.

---

## 3. Parcours de résolution : ce qui a été découvert, dans l'ordre

Mettre en place les quotas et les limites ne suffisait pas : pour *voir* leur effet, il a fallu corriger plusieurs défauts du prototype. Chaque ligne ci-dessous correspond à un symptôme réellement observé.

| # | Symptôme observé | Cause identifiée | Correction | Fichier |
|---|---|---|---|---|
| 1 | Le test restait à `0.0 paquet`. La file affichait `0 message, 1 consommateur` et le générateur répétait `Channel is closed`. | Le générateur n'ouvrait jamais de nouvelle connexion après la perte du broker. | Reconnexion automatique, tampon local, confirmations d'envoi. | `generator.py` |
| 2 | Presque tout le trafic normal était classé `ANOMALIE`. | Le générateur émettait une latence normale de ≈ 10 ms alors que le modèle est entraîné sur 20 ms. | Latence normale alignée sur l'entraînement (20 ms). | `generator.py` |
| 3 | Un pod du générateur restait en `ImagePullBackOff` (286 tentatives) et le déploiement ne se terminait pas. | L'image n'existe que dans Kind, mais `imagePullPolicy` valait `Always` / `IfNotPresent`. Le quota presque plein empêche en plus le pod de surplus d'un rolling update. | `imagePullPolicy: Never` et stratégie `Recreate`. | `deployment.yaml`, `generator-deployment.yaml` |
| 4 | Le compteur `RESTARTS` de l'IA augmentait à chaque redémarrage du broker (`ConnectionClosedByBroker`). | Exception non gérée dans `start_consuming()`. | Boucle de reconnexion. | `app.py` |
| 5 | Tous les pods tournaient sur le même worker. | Aucun label de rôle, aucun `nodeSelector` : le pilier C n'était pas implémenté. | Labels dans la config Kind, `nodeSelector` dans les trois manifestes. | `kind-config.yaml` et manifestes |
| 6 | Deux fichiers NetworkPolicy se contredisaient : l'un restreignait RabbitMQ, l'autre ouvrait tout. | Les politiques s'additionnent : la plus permissive l'emporte. | Une seule politique : refus par défaut, puis autorisations ciblées. | `02-network-policy.yaml` |
| 7 | Premier essai de charge (50 msg/s, panne de 71 s) : aucun pic visible. | Backlog trop petit (3 569 messages, ≈ 14 s de CPU à la limite) pour la résolution de 30 s de cAdvisor. | Débit porté à 100 msg/s et panne à 3 min (section 7). | procédure |
| 8 | Deuxième essai : l'IA s'est arrêtée (`Exit Code 1`) à 18:23:13, au retour du broker. | `connect_with_retry` abandonnait après 15 tentatives (≈ 3 min) puis quittait le processus. | Tentatives sans limite. | `app.py` |
| 9 | La CI passait toujours au vert. | `kubectl diff --local ... \|\| true` ne validait rien, et le test de résilience n'y tournait pas. | `kubeconform` strict ; test E2E à chaque push sur la stack Compose jusqu'à Grafana ; test sur cluster Kind disponible à la demande. | `.github/workflows/` |

---

## 4. La solution d'architecture (v3)

Trois piliers d'isolation, plus la résilience applicative sans laquelle ces piliers ne sont pas démontrables.

### Pilier A : le rationnement matériel via les cgroups (`deployment.yaml`)

*   **`requests` (garantie de démarrage) :** 250m CPU / 256Mi RAM.
*   **`limits` (plafond absolu) :** 500m CPU / 512Mi RAM. Lors d'un pic, le CPU de l'IA est bridé : elle met plus de temps à écluser la file, mais elle ne dépasse jamais sa limite. Le choix des valeurs est justifié en section 7.

### Pilier B : la sanctuarisation des composants

*   Deux compartiments logiques : **`ingestion`** (RabbitMQ, générateur) et **`ia`** (moteur d'analyse).
*   Des **ResourceQuotas** plafonnent la consommation de chaque namespace (`01-namespaces-quotas.yaml`).
*   RabbitMQ est un **StatefulSet** avec un **PVC de 2Gi** : la file durable survit à la perte du pod.
*   Des **NetworkPolicy** (`02-network-policy.yaml`) refusent par défaut tout trafic entrant dans les deux namespaces, puis autorisent uniquement :
    *   AMQP (TCP 5672) vers RabbitMQ, depuis `ingestion` (générateur) et `ia` (moteur d'analyse) ;
    *   les métriques (TCP 8000) de l'IA, depuis le namespace où tourne Prometheus (ici `default`).

### Pilier C : la séparation physique (cluster multi-nœuds + nodeSelector)

*   Un cluster Kind **`cloudia-v3`** à **3 nœuds** (1 control-plane, 2 workers) dont les workers portent un **label de rôle** : `workload=ingestion` (broker et générateur) et `workload=compute` (IA).
*   Les manifestes imposent le nœud avec un **`nodeSelector`**. Un pic de charge de l'IA ne se répercute donc plus sur le nœud du broker.
*   Le volume persistant de RabbitMQ (stockage local de Kind) est lié au nœud où il a été créé : le `nodeSelector` garantit que le pod est toujours recréé au même endroit et retrouve ses données.
*   En production, le même mécanisme dédie des serveurs équipés de **GPU** aux pods d'IA. Les GPU ne sont pas disponibles en local : le principe est démontré, le matériel reste une cible d'architecture.

### Résilience applicative (ce qui rend la panne observable)

| Composant | Comportement | Pourquoi |
|---|---|---|
| Générateur | Produit à débit constant (`RATE`) en suivant l'horloge, garde les messages dans un **tampon local** (`BUFFER_MAX`) pendant la panne, tente de se reconnecter chaque seconde, puis envoie tout d'un coup. | C'est ce tampon qui crée le « pic de rattrapage » décrit en section 2. |
| Générateur | File **durable**, messages **persistants** (`delivery_mode=2`), **confirmations d'envoi** : un message ne quitte le tampon qu'une fois confirmé par le broker. | Aucun message perdu entre le générateur et le disque du broker. |
| IA | Boucle de reconnexion **sans limite** (une tentative ≈ toutes les 13 s : 10 s de délai de connexion + 3 s d'attente). | Une panne longue ne doit pas redémarrer l'IA (sinon `RESTARTS` monte et les compteurs Prometheus repartent à 0). |
| IA | `queue_declare` idempotent au démarrage. | Plus de course entre l'IA et le générateur si l'IA démarre la première. |
| IA | `prefetch` = 50. | Borne le nombre de messages en cours de traitement : la mémoire de l'IA ne dépend pas de la taille du backlog. |
| IA | Acquittement (`ack`) systématique, même en cas d'erreur d'analyse. | Un message illisible ne bloque pas la file (choix assumé, voir limites). |

---

## 5. Cartographie de l'architecture & rôle des fichiers

```mermaid
graph LR
    subgraph w1["Worker cloudia-v3-worker (workload=ingestion)"]
        subgraph ingestion["Namespace ingestion : ResourceQuota + NetworkPolicy"]
            Generator["generator.py : tampon local + reconnexion"] -- "AMQP 5672" --> Broker["RabbitMQ StatefulSet + PVC 2Gi"]
        end
    end
    subgraph w2["Worker cloudia-v3-worker2 (workload=compute)"]
        subgraph ia["Namespace ia : ResourceQuota + NetworkPolicy"]
            IA["app.py : Isolation Forest, limits 500m / 512Mi"]
        end
    end
    Broker -- "consommation, prefetch 50" --> IA
    IA -- "métriques port 8000" --> Prometheus["Prometheus"]
    Prometheus --> Grafana["Grafana port 3000"]
```

### 📂 Inventaire des fichiers

| Fichier | Rôle | Pilier |
|---|---|---|
| `kind-config.yaml` | Cluster Kind `cloudia-v3` à 3 nœuds, avec un label de rôle par worker. | C |
| `01-namespaces-quotas.yaml` | Namespaces `ia` et `ingestion` et leurs ResourceQuotas. | B |
| `02-network-policy.yaml` | Refus par défaut du trafic entrant, puis autorisations ciblées (AMQP, métriques). | B |
| `rabbitmq-v3-persistant.yaml` | RabbitMQ en StatefulSet dans `ingestion` : PVC 2Gi, requests/limits compatibles avec le quota, `nodeSelector: workload=ingestion`, Service. Remplace la commande `kubectl create deployment` de la v2bis. | A, B, C |
| `deployment.yaml` | IA dans `ia` : 1 réplica, limits CPU/RAM, `nodeSelector: workload=compute`, `imagePullPolicy: Never`, stratégie `Recreate`. | A, C |
| `generator-deployment.yaml` | Simulateur dans `ingestion` : requests/limits pour passer le quota, `nodeSelector: workload=ingestion`, `imagePullPolicy: Never`, stratégie `Recreate`. | B, C |
| `prometheus-link.yaml` | `Service` de l'IA (utilisé par le test pour le `port-forward`) et `PodMonitor` (scraping toutes les 5 s). | observabilité |
| `app.py` | Moteur analytique (Isolation Forest) : consommation de la file, reconnexion, métriques Prometheus (port 8000). | A |
| `generator.py` | Simulateur d'antenne 5G : débit réglable (`RATE`), tampon, reconnexion, messages persistants. | résilience |
| `test_observability.py` | Crash-test automatisé, deux modes (`TEST_MODE`). `k8s` (défaut) : supprime le pod du broker, vérifie que le flux reprend **et que `RESTARTS` de l'IA n'a pas bougé**. `compose` : vérifie d'abord la chaîne jusqu'à Grafana, tue (SIGKILL) puis redémarre le conteneur RabbitMQ, vérifie que le flux reprend, que le conteneur de l'IA n'a pas redémarré **et que Grafana voit la reprise**. | tests |
| `docker-compose.test.yml` | Sonde locale **sans cluster**, jusqu'à Grafana : RabbitMQ, IA, générateur, Prometheus, Grafana. Mêmes limites CPU/RAM que les manifestes ; deux réseaux séparés (`ingestion`, `monitoring`) qui jouent le rôle des namespaces. Ne couvre pas ResourceQuotas, `nodeSelector`, PVC, vraies NetworkPolicy ni le throttling mesuré par cAdvisor (rôle du workflow Kind). | tests |
| `observability/` | Configuration de la sonde Compose : `prometheus.yml` (scraping de l'IA toutes les 5 s), `grafana-datasource.yml` (source Prometheus, uid `prometheus`), `grafana-dashboards.yml` (chargement des tableaux de bord) et `cloudia-v3-dashboard.json` (tableau de bord `cloudia-v3`, 6 panneaux). | observabilité |
| `.github/workflows/ci-cd.yaml` | CI/CD à chaque push : Linting, Test E2E (`docker-compose.test.yml`), puis Image du cluster (publication sur GitHub Container Registry). | CI/CD |
| `.github/workflows/kind-e2e.yaml` | Test de résilience sur un vrai cluster Kind à 3 nœuds, déclenché à la main (*Actions > Kind E2E (manuel) > Run workflow*). | CI |
| `Dockerfile` | Image `python:3.10-slim` contenant les scripts et dépendances. | |
| `.dockerignore` | Garde hors de l'image le venv local, les captures, la documentation et les manifestes (`COPY . .` copie tout le dossier). | |
| `.gitignore` | Exclut `.venv/`, `__pycache__/`, `*.pyc` et `.DS_Store` du dépôt. | |
| `requirements.txt` | Versions verrouillées (`scikit-learn`, `numpy`, `pika`, `prometheus-client`). `requests`, utilisé uniquement par le test, n'est pas verrouillé. | |
| `docs/img/` | Captures Grafana citées dans ce document. | |

---

## 6. Registre des paramètres : où chaque valeur est définie

| Paramètre | Valeur | Défini dans | Justification |
|---|---|---|---|
| Limites de l'IA | 500m CPU / 512Mi RAM | `deployment.yaml` (`resources.limits`) | 3,2 fois la CPU mesurée à 100 msg/s (0,157 cœur) et 4 fois la mémoire maximale observée (134 Mo). |
| Requests de l'IA | 250m CPU / 256Mi RAM | `deployment.yaml` (`resources.requests`) | 1,6 fois la CPU et 2 fois la mémoire mesurées à 100 msg/s. |
| Quota `ia` | requests 250m / 256Mi, limits 500m / 512Mi | `01-namespaces-quotas.yaml` | Calé exactement sur le pod unique de l'IA. Conséquence : stratégie `Recreate`, car un rolling update ne pourrait pas créer un second pod. |
| Quota `ingestion` | requests 500m / 512Mi, limits 1 / 1Gi | `01-namespaces-quotas.yaml` | Requests utilisées : 350m / 384Mi (broker + générateur). Limits : 700m / 768Mi. |
| RabbitMQ | requests 250m / 256Mi, limits 500m / 512Mi, PVC 2Gi (ReadWriteOnce) | `rabbitmq-v3-persistant.yaml` | CPU garanti pour le broker, qui ne doit pas être privé de ressources. |
| Générateur | requests 100m / 128Mi, limits 200m / 256Mi | `generator-deployment.yaml` | Obligatoire pour passer le quota ; marge pour le tampon. |
| Labels des workers | `workload=ingestion` et `workload=compute` | `kind-config.yaml` | Pilier C. |
| `nodeSelector` | idem | `deployment.yaml`, `generator-deployment.yaml`, `rabbitmq-v3-persistant.yaml` | Une seule source de vérité : le label du nœud. |
| `imagePullPolicy` | `Never` | `deployment.yaml`, `generator-deployment.yaml` | L'image est chargée dans Kind avec `kind load docker-image`, jamais téléchargée. |
| `RATE` | 1 msg/s par défaut | `generator.py` (variable d'environnement) | Débit nominal. Modifié à chaud pour les essais : `kubectl set env ... RATE=100`. |
| `BUFFER_MAX` | 5 000 par défaut, 30 000 pour l'essai 2 | `generator.py` (variable d'environnement) | Doit dépasser `RATE × durée de panne` (section 7). |
| `RESET_QUEUE` | désactivé | `generator.py` | Supprime la file au démarrage si `=1` (ponctuel). |
| `TICK` | 0,2 s | `generator.py` (constante) | Pas de la boucle principale. |
| `PREFETCH` | 50 | `app.py` (variable d'environnement) | Borne les messages non acquittés. |
| Entraînement de l'IA | 500 échantillons ~ N(100 ; 10), N(1 ; 0,5), N(20 ; 2), `contamination=0.05` | `app.py` | Référence du trafic normal : paquets, % d'erreurs, latence en ms. |
| Trafic simulé | 85 % normal (mêmes moyennes que l'entraînement), 15 % d'anomalies (500-800 paquets, 8-15 % d'erreurs, 90-150 ms) | `generator.py` | Doit rester cohérent avec l'entraînement (voir parcours, ligne 2). |
| Métriques exposées | `telecom_processed_packets_total`, `telecom_anomalies_detected_total` (port 8000) | `app.py` | Débit de traitement et nombre d'anomalies. |
| Scraping | 5 s | `prometheus-link.yaml` (PodMonitor) | Résolution fine du compteur applicatif. |
| Politiques réseau | TCP 5672 (broker) et TCP 8000 (métriques) | `02-network-policy.yaml` | Moindre privilège. |
| Délai du test | 60 s par défaut en mode `k8s` (180 s en CI), 150 s en mode `compose` | `test_observability.py` (variable `TEST_TIMEOUT`) | La CI est plus lente qu'un poste local ; en mode `compose`, le broker doit aussi redémarrer. |
| Délai de la chaîne Grafana | 90 s (120 s en CI) | `test_observability.py` (variable `STACK_TIMEOUT`) | Temps laissé à Prometheus (scraping toutes les 5 s) et à Grafana pour voir les premières métriques. |
| Limites de la sonde Compose | IA et broker : `cpus: 0.5`, `mem_limit: 512m` ; générateur : `cpus: 0.2`, `mem_limit: 256m` | `docker-compose.test.yml` | Mêmes valeurs que les manifestes Kubernetes (`512m` = `512Mi`). |
| Images de la sonde Compose | `prom/prometheus:v2.53.0`, `grafana/grafana:11.1.0`, `rabbitmq:3-management` | `docker-compose.test.yml` | Versions épinglées (sauf RabbitMQ) pour une sonde reproductible. |
| Identifiants fixes de la sonde | source de données `prometheus`, tableau de bord `cloudia-v3`, Grafana `admin` / `admin` | `observability/` | Référencés par `test_observability.py` ; prototype uniquement. |

---

## 7. Calculs : comment le débit et la durée de panne ont été choisis

L'objectif est qu'après une panne, le backlog soit assez gros pour que le plafond CPU de l'IA se voie sur les courbes. La résolution des métriques cAdvisor est d'un point toutes les 30 s : un pic doit durer **bien plus de 30 s** pour apparaître.

### Étape 1 : mesurer le coût CPU d'un message

La CPU réelle est lue dans le conteneur (`/sys/fs/cgroup/cpu.stat`), indépendamment de cAdvisor :

| Régime | Mesure | CPU de l'IA | Coût par message |
|---|---|---|---|
| 1 msg/s | graphique Grafana | ≈ 0,006 cœur | (coût fixe dominant) |
| 50 msg/s | `usage_usec` : + 977 260 µs en 10 s | **0,098 cœur** | ≈ 1,95 ms |
| 100 msg/s | `usage_usec` : + 27 626 552 µs en 176 s | **0,157 cœur** | ≈ 1,57 ms |

### Étape 2 : estimer la capacité de l'IA sous sa limite

Avec une limite de 0,5 cœur : 0,5 ÷ (1,57 à 1,95 ms) ≈ **250 à 320 messages par seconde**. C'est une estimation faite en régime stable (voir étape 5 pour l'écart avec la mesure).

### Étape 3 : taille du backlog

`backlog = RATE × durée de la panne`

| Essai | RATE | Panne | Backlog | Source |
|---|---|---|---|---|
| 1 | 50 msg/s | 71 s | ≈ 3 550 attendus, **3 569 mesurés** (+ 139 produits pendant l'envoi) | log du générateur |
| 2 | 100 msg/s | ≈ 3 min | ≈ 18 000 attendus | calcul (le log du générateur n'a pas été conservé) |

### Étape 4 : temps CPU nécessaire pour écluser le backlog

| Essai | Temps CPU = backlog × coût | Durée minimale à 0,5 cœur | Visible à 30 s de résolution ? |
|---|---|---|---|
| 1 | 3 569 × 1,95 ms ≈ 7 s | ≈ 14 s | **Non** : sous la résolution. |
| 2 | 18 000 × 1,57 ms ≈ 28 s | ≈ 57 s | **Oui** : plusieurs points. |

Pendant le rattrapage, le flux courant consomme encore 0,157 cœur. Il ne reste que 0,5 − 0,157 = 0,343 cœur pour le backlog, soit une durée attendue de **28 ÷ 0,343 ≈ 82 s**.

### Étape 5 : comparer à la mesure

Mesuré à l'essai 2 : plateau d'**environ 1 minute** (deux points à 30 s d'intervalle), débit de traitement de 390 puis 555 msg/s. Le rattrapage est donc un peu plus rapide que l'estimation : le coût CPU par message est plus faible en rafale que dans le régime stable (moins de temps perdu entre deux messages). L'estimation est prudente.

### Étape 6 : dimensionner le reste

*   **`BUFFER_MAX = 30 000`** : doit dépasser 18 000 (100 msg/s × 180 s), avec une marge de 67 %. À ≈ 0,5 Ko par message en mémoire (estimation, non mesurée), 18 000 messages représentent environ 9 Mo, très en dessous de la limite de 256Mi du générateur.
*   **`PREFETCH = 50`** : l'IA ne détient jamais plus de 50 messages non acquittés. Mesuré : mémoire ≤ 134 Mo pour ≈ 18 000 messages écoulés.
*   **Limites 500m / 512Mi** : voir le registre (section 6). La limite CPU est assez basse pour être atteinte pendant un rattrapage (c'est l'objet de la démonstration) et assez haute pour absorber le régime à 100 msg/s avec une marge de 3.
*   **Panne de 180 s** : assez longue pour produire un plateau de rattrapage couvrant plusieurs points de mesure.

---

## 8. Mesures : scénarios, courbes et résultats

### 8.0 Dispositif de mesure

*   **Matériel :** MacBook Air, Docker Desktop (6 à 8 Go de RAM alloués), cluster Kind `cloudia-v3` (3 nœuds).
*   **Captures :** Grafana → *Explore* (source Prometheus), une requête par panneau. Heures locales (UTC+2). Les logs Kubernetes (`--timestamps`) sont en UTC.
*   **Requêtes PromQL** (fenêtre ≥ 1 min : cAdvisor n'expose qu'un point toutes les 30 s, une fenêtre plus courte donne des trous) :

```promql
# CPU de l'IA, en cœurs (la limite est 0,5)
sum(rate(container_cpu_usage_seconds_total{namespace="ia",container="ia-analyzer"}[1m]))

# Part des périodes CFS bridées (0 = jamais, 1 = en permanence)
sum(rate(container_cpu_cfs_throttled_periods_total{namespace="ia",container="ia-analyzer"}[1m]))
  / sum(rate(container_cpu_cfs_periods_total{namespace="ia",container="ia-analyzer"}[1m]))

# Débit de traitement de l'IA, en messages par seconde
sum(rate(telecom_processed_packets_total[15s]))

# Mémoire de l'IA, en octets (la limite est 512Mi = 536 870 912)
container_memory_working_set_bytes{namespace="ia",container="ia-analyzer"}
```

*   **Vérité terrain CPU :** `kubectl exec -n ia deploy/telecom-ia-deployment -- cat /sys/fs/cgroup/cpu.stat` (`usage_usec`, `nr_throttled`, `throttled_usec`).
*   **Panne simulée :** `kubectl scale statefulset telecom-broker-rabbitmq -n ingestion --replicas=0`, attente, puis `--replicas=1`. Le PVC est conservé.

### 8.1 Régime nominal : 1 msg/s

Référence de repos : l'IA traite un message par seconde.

| CPU de l'IA | Throttling | Débit traité | Mémoire |
|---|---|---|---|
| ![CPU nominale](docs/img/nominal-rate1-cpu.png) | ![Throttling nominal](docs/img/nominal-rate1-throttling.png) | ![Débit nominal](docs/img/nominal-rate1-debit.png) | ![Mémoire nominale](docs/img/nominal-rate1-memoire.png) |

*   CPU : 0,004 à 0,008 cœur. Throttling : 0 (un point isolé à 0,036).
*   Débit : 0,9 à 1,1 msg/s. L'oscillation est un effet de la fenêtre de 15 s (un message de plus ou de moins), pas une variation réelle.
*   Mémoire : 130,42 à 130,45 Mo, stable.

### 8.2 Essai 1 : 50 msg/s, panne de 71 s (le pic reste invisible)

Chronologie : le broker s'arrête à 16:51:41 ; le générateur se reconnecte à 16:52:52 et envoie **3 569 messages d'un coup** à 16:52:55 ; l'IA ne redémarre pas.

| CPU de l'IA | Throttling | Débit traité | Mémoire |
|---|---|---|---|
| ![CPU essai 1](docs/img/essai1-rate50-cpu.png) | ![Throttling essai 1](docs/img/essai1-rate50-throttling.png) | ![Débit essai 1](docs/img/essai1-rate50-debit.png) | ![Mémoire essai 1](docs/img/essai1-rate50-memoire.png) |

*   Avant la panne, à 50 msg/s : CPU ≈ 0,1 cœur (cohérent avec la mesure `cpu.stat` de 0,098).
*   Pendant la panne : débit 0 (16:52:00 et 16:52:30), CPU ≈ 0 dès 16:52:30.
*   Au rattrapage : le débit moyen monte à ≈ 140 msg/s sur une fenêtre de 15 s, puis revient à 50. **La CPU et le throttling ne montrent aucun pic** : le backlog est écoulé en moins de 30 s.
*   Mémoire : 115,6 à 115,9 Mo, sans variation.

**Conclusion :** la reprise fonctionne et la mémoire est sans effet, mais le plafonnement CPU n'est pas démontré. D'où l'essai 2 (calculs de la section 7).

### 8.3 Essai 2 : 100 msg/s, panne de 3 min (le lissage devient visible)

Le débit passe de 50 à 100 msg/s vers 18:20, puis le broker est arrêté (≈ 18:20:30) et relancé 3 minutes plus tard (≈ 18:23).

| CPU de l'IA | Throttling | Débit traité | Mémoire |
|---|---|---|---|
| ![CPU essai 2](docs/img/essai2-rate100-cpu.png) | ![Throttling essai 2](docs/img/essai2-rate100-throttling.png) | ![Débit essai 2](docs/img/essai2-rate100-debit.png) | ![Mémoire essai 2](docs/img/essai2-rate100-memoire.png) |

| Phase | Heure | CPU | Throttling | Débit traité |
|---|---|---|---|---|
| Régime à 50 msg/s | avant 18:20 | ≈ 0,1 | ≈ 0 | 50 |
| Panne du broker | 18:20:30 à 18:23:00 | ≈ 0 | 0 | 0 |
| **Rattrapage** | **18:23:30 à 18:24:00** | **0,27 puis 0,5 (la limite)** | **0,97 puis 1,0** | **390 puis 555** |
| Retour au régime | 18:24:30 | 0,16 | ≈ 0 | 100 |

*   La CPU **plafonne à 0,5**, exactement la limite du cgroup, et le throttling approche 100 % : l'IA est bridée presque en permanence pendant le rattrapage. C'est la démonstration du pilier A.
*   Le débit de traitement est de 390 à 555 msg/s pendant environ 1 minute (contre 100 en régime normal).
*   La mémoire passe de 116-117 Mo à 133,7 Mo (**≈ 25 % de la limite**). Les deux couleurs de la courbe sont deux conteneurs différents (voir 8.4).
*   Vérité terrain (`cpu.stat` à 18:28:17, conteneur démarré à 18:23:13) : **485 périodes bridées et 9,2 s de bridage cumulé**, contre 115 périodes en près de 13 h pour le conteneur précédent. Ensuite, à 100 msg/s en régime stable : 0,157 cœur et seulement 35 périodes bridées en 176 s (≈ 2 %).

### 8.4 Incident découvert pendant l'essai 2 : l'IA a redémarré

Les compteurs `cpu.stat` avaient été remis à zéro et la courbe mémoire changeait de série. Vérification :

```text
Last State:  Terminated
  Reason:    Error
  Exit Code: 1
  Started:   Tue, 13 Oct 2026 05:24:41 +0200
  Finished:  Tue, 13 Oct 2026 18:23:13 +0200
...
⏳ Tentative de connexion à RabbitMQ (15/15)...
❌ L'IA n'a pas pu se connecter à RabbitMQ après 15 tentatives.
```

**Cause :** `connect_with_retry` abandonnait après 15 tentatives, soit environ 3 minutes (≈ 13 s par tentative). L'essai 1 (71 s) était passé sous ce seuil, l'essai 2 (3 min) l'a dépassé. Kubernetes a relancé le conteneur, qui a consommé le backlog au retour du broker.

**Effet sur la mesure :** le plafonnement CPU reste valide (la limite s'applique à n'importe quel conteneur), mais le rattrapage a été traité par le conteneur redémarré.

**Correction (`app.py`) :** `connect_with_retry` réessaie sans limite, et le processus ne quitte plus.

### 8.5 Validation du correctif

Nouveau pod de l'IA, panne du broker d'environ 3 min :

```text
18:51:39  ⚠️ Broker injoignable : ... Nouvel essai toutes les 3 secondes...   (tentative 1)
18:53:36  ⚠️ Broker injoignable : ... Nouvel essai toutes les 3 secondes...   (tentative 10)
18:54:37  📥 En attente de flux réseau sur la file 'telecom_traffic'...         (reconnecté après 178 s)
```

`RESTARTS = 0` après la panne. Cette répétition dure à peu près autant que l'ancien seuil (≈ 3 min) : elle ne le dépasse pas. L'absence de limite est garantie par construction (`while True`), mais une panne de 5 minutes reste à rejouer pour le démontrer (voir les limites).

### 8.6 Synthèse

| Situation | Débit | CPU de l'IA | Throttling | Mémoire de l'IA |
|---|---|---|---|---|
| Nominal | 1 msg/s | ≈ 0,006 cœur | ≈ 0 | 130 Mo |
| Charge soutenue | 50 msg/s | 0,098 cœur | 0,19 % des périodes (cumul) | 116 Mo |
| Charge soutenue | 100 msg/s | 0,157 cœur | ≈ 2 % des périodes | 134 Mo |
| **Rattrapage** (18 000 messages) | 390-555 msg/s | **0,5 (limite)** | **97 à 100 %** pendant ≈ 1 min | ≤ 134 Mo (≈ 25 % de la limite) |

---

## 9. Isolation réseau : tests

`kindnet`, le CNI par défaut de Kind, applique bien les NetworkPolicy. Vérification faite après l'application de `02-network-policy.yaml` :

| Test | Commande (résumé) | Attendu | Observé |
|---|---|---|---|
| A. IA vers broker | connexion TCP 5672 depuis un pod de `ia` | autorisé | `OK` |
| B. Pod extérieur vers broker | `busybox` dans `default` vers le broker | bloqué | `Connection timed out` |
| C. Générateur vers métriques de l'IA | connexion TCP 8000 depuis `ingestion` | bloqué | `TimeoutError` |
| D. Prometheus vers l'IA | `up{namespace="ia"}` | autorisé | `1` |

Commandes complètes en section 11, étape 7.

---

## 10. CI/CD (`.github/workflows/`)

À chaque `push` (et pull request) sur `main` ou `v3`, `ci-cd.yaml` enchaîne trois étapes. Chacune ne démarre que si la précédente est verte.

```mermaid
graph LR
    P["git push"] --> L["CI · Linting"] --> E["CI · Test E2E<br/>docker-compose.test.yml"] --> C["CD · Image du cluster<br/>(push uniquement)"]
```

| Job | Contenu |
|---|---|
| `lint` : CI · Linting | Lint Flake8 (erreurs de syntaxe et noms indéfinis). Validité YAML / JSON des fichiers de `observability/`. Validité du fichier `docker-compose.test.yml` (`docker compose config`). Validation des manifestes avec **`kubeconform`** en mode strict (schémas Kubernetes officiels et schéma du `PodMonitor`) : un manifeste invalide fait échouer le job. |
| `e2e` : CI · Test E2E | Exécute **`docker-compose.test.yml`** (`up -d --build --wait`), puis `TEST_MODE=compose python test_observability.py` : Prometheus voit l'IA en `UP`, Grafana répond, sa source de données et son tableau de bord sont provisionnés, Grafana restitue le compteur de l'IA ; puis SIGKILL du conteneur RabbitMQ, redémarrage avec le même volume, reprise du flux, conteneur de l'IA non redémarré et reprise visible dans Grafana. Affiche l'état et les logs des conteneurs en cas d'échec, puis supprime la stack et son volume. |
| `cd-image` : CD · Image du cluster | Uniquement sur un `push` (jamais sur une pull request). Construit l'image avec Buildx, vérifie qu'elle importe `numpy`, `scikit-learn`, `pika` et `prometheus_client`, puis la publie sur GitHub Container Registry : `ghcr.io/cedrickoukoui/cloudia`, avec les étiquettes `<sha du commit>` et `<nom de la branche>` (`v3`), plus `latest` sur `main`. |

Le script de test tourne sur le runner et non dans un conteneur : il pilote le conteneur RabbitMQ avec la CLI `docker` (kill puis start), ce qu'un service `testeur` ne pourrait pas faire sans accès à la socket Docker.

Les manifestes Kubernetes continuent d'utiliser l'image locale `telecom-ia:latest` chargée dans Kind (`imagePullPolicy: Never`) : l'image publiée est celle qu'un cluster distant tirerait.

**Test sur cluster Kind (à la demande).** `kind-e2e.yaml` crée un cluster à 3 nœuds avec `kind-config.yaml`, construit et charge l'image, installe les CRD Prometheus, déploie tous les manifestes, vérifie le placement des pods (`nodeSelector`), puis lance `test_observability.py` en mode `k8s` (suppression du pod du broker, reprise du flux, `RESTARTS` de l'IA inchangé). Il est plus lourd que la sonde Compose : il ne tourne donc pas à chaque push.

Le badge en tête de ce document reflète le Linting, le Test E2E sur la stack jusqu'à Grafana **et** la construction de l'image.

---

## 11. 🚀 Guide d'exploitation : récupération & lancement local

### Prérequis

*   Docker Desktop (**6 à 8 Go de RAM alloués** : 3 nœuds Kind et la stack Prometheus/Grafana sont gourmands)
*   `kind`, `kubectl` et Helm v3

### 1. Clonage et environnement

```bash
git clone https://github.com/CedricKoukoui/CloudIA.git
cd CloudIA && git checkout v3
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Création du cluster multi-nœuds (Kind)

```bash
kind delete cluster --name cloudia-v3
kind create cluster --name cloudia-v3 --config kind-config.yaml
kubectl get nodes --show-labels
```

Les 3 nœuds doivent être `Ready`, et les deux workers doivent porter `workload=ingestion` et `workload=compute`.

Si le cluster existe déjà et que tu ne veux pas le recréer, ajoute les labels à la main :

```bash
kubectl label node cloudia-v3-worker workload=ingestion
kubectl label node cloudia-v3-worker2 workload=compute
```

### 3. Compilation et chargement de l'image

```bash
docker build --no-cache -t telecom-ia:latest .
kind load docker-image telecom-ia:latest --name cloudia-v3
```

Les manifestes utilisent `imagePullPolicy: Never` : sans cette étape, les pods échouent faute d'image.

### 4. Stack d'observabilité (Prometheus + Grafana)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install telecom-monitor prometheus-community/kube-prometheus-stack \
  --set grafana.adminPassword=admin
```

La stack est installée dans le namespace `default`. Les `NetworkPolicy` de `02-network-policy.yaml` autorisent le scraping depuis ce namespace : si tu l'installes ailleurs (par exemple `--namespace monitoring --create-namespace`), adapte la valeur `kubernetes.io/metadata.name` dans la politique `allow-metrics-scrape`. Attends que les pods soient `Running` (`kubectl get pods`) : le `PodMonitor` de l'étape 6 a besoin des CRD installées ici.

### 5. Namespaces, quotas et broker

```bash
kubectl apply -f 01-namespaces-quotas.yaml
kubectl apply -f rabbitmq-v3-persistant.yaml
kubectl get pods,pvc -n ingestion
```

Le pod RabbitMQ doit être `Running` et le PVC `Bound`.

### 6. Déploiement de l'IA, du générateur et de l'observabilité

```bash
kubectl apply -f deployment.yaml -f generator-deployment.yaml -f prometheus-link.yaml
kubectl get pods -A -o wide
```

Colonne `NODE` : les pods du namespace `ia` doivent être sur le worker `compute` (`cloudia-v3-worker2`), ceux de `ingestion` sur le worker `ingestion` (`cloudia-v3-worker`).

> **Attention :** l'IA est dans `ia` et RabbitMQ dans `ingestion`. `RABBITMQ_URL` utilise donc le nom complet du service : `amqp://user:password@telecom-broker-rabbitmq.ingestion.svc.cluster.local:5672/%2F`. Avec le nom court, la résolution DNS échoue entre namespaces.

### 7. Isolation réseau

```bash
kubectl apply -f 02-network-policy.yaml

# A. doit afficher OK
kubectl exec -n ia deploy/telecom-ia-deployment -- python -c "import socket; socket.create_connection(('telecom-broker-rabbitmq.ingestion.svc.cluster.local', 5672), 3); print('OK')"

# B. doit échouer (timeout)
kubectl run np-test --rm -it --restart=Never -n default --image=busybox:1.36 -- nc -zv -w 3 telecom-broker-rabbitmq.ingestion.svc.cluster.local 5672

# C. doit échouer (timeout)
kubectl exec -n ingestion deploy/telecom-generator-deployment -- python -c "import socket; socket.create_connection(('telecom-ia-service.ia.svc.cluster.local', 8000), 3); print('OK')"
```

### 8. Vérification des quotas et des limites

```bash
kubectl describe resourcequota -n ia
kubectl describe resourcequota -n ingestion
kubectl get pods -n ia -o jsonpath='{.items[0].spec.containers[0].resources}'
```

### 9. Visualisation (Grafana)

```bash
kubectl port-forward svc/telecom-monitor-grafana 3000:80
```

Accès à `http://127.0.0.1:3000` (admin / admin), onglet *Explore*. Les requêtes PromQL sont en section 8.0.

### 10. Crash-test rapide (validation du pilier A)

Terminal 1 :

```bash
kubectl get pods -n ia -w
```

Terminal 2 :

```bash
kubectl delete pod -n ingestion -l app=rabbitmq
```

Résultat attendu : le pod RabbitMQ est recréé **sur le même worker** et retrouve son PVC. L'IA reste `Running` et `RESTARTS` ne bouge pas. Le générateur journalise la perte, puis `Reconnecté. Rattrapage de N message(s)`.

### 11. Test automatisé

Avec le venv actif :

```bash
python3 test_observability.py
```

En mode `k8s` (par défaut), le script ouvre un tunnel vers le cluster, relève `RESTARTS` de l'IA, supprime le broker, vérifie que le flux reprend, puis vérifie que `RESTARTS` est inchangé.

### 12. Variante sans cluster : la stack Docker Compose jusqu'à Grafana

Cette sonde ne demande ni Kind, ni Helm, ni `kubectl` : seulement Docker. Elle répond à la question « tout fonctionne-t-il de bout en bout, jusqu'à la courbe dans Grafana, y compris après la perte brutale du broker ? ».

```mermaid
graph LR
    subgraph ingestion["réseau ingestion"]
        G["generator<br/>0,2 cpu / 256m"] -- "AMQP 5672" --> R["rabbitmq<br/>0,5 cpu / 512m + volume"]
    end
    R -- "consommation" --> I["ia-analyzer<br/>0,5 cpu / 512m"]
    subgraph monitoring["réseau monitoring"]
        P["prometheus<br/>scraping 5 s"] --> GR["grafana<br/>dashboard cloudia-v3"]
    end
    I -- "métriques 8000" --> P
```

Le générateur n'est pas sur le réseau `monitoring` (il ne peut pas lire les métriques de l'IA) et Prometheus n'est pas sur le réseau `ingestion` (il ne peut pas joindre le broker) : c'est l'équivalent, à l'échelle de Docker, des NetworkPolicy de l'étape 7.

```bash
# 1. Démarrer la stack (le premier lancement construit l'image et télécharge les images publiques)
docker compose -f docker-compose.test.yml up -d --build --wait

# 2. Lancer le test, venv actif (contrôle de la chaîne, panne du broker, reprise)
TEST_MODE=compose python3 test_observability.py

# 3. Regarder le résultat : Grafana (admin / admin), tableau de bord « CloudIA v3 : supervision télécom »
#    http://127.0.0.1:3000/d/cloudia-v3   (Prometheus : http://127.0.0.1:9090)

# 4. Tout arrêter et supprimer le volume
docker compose -f docker-compose.test.yml down -v
```

Le test enchaîne : (1) Prometheus voit l'IA en `UP` ; (2) Grafana répond, sa source de données et son tableau de bord sont provisionnés ; (3) Grafana, interrogé via sa source de données, restitue `sum(telecom_processed_packets_total)` ; (4) SIGKILL du conteneur RabbitMQ, 12 s d'attente, redémarrage avec le même volume ; (5) le flux reprend, le conteneur de l'IA n'a pas redémarré (état, date de démarrage et `RestartCount` identiques) ; (6) Grafana voit le compteur augmenter après la panne.

Pour provoquer un vrai rattrapage dans cette stack, augmente le débit : `RATE=100 BUFFER_MAX=30000 docker compose -f docker-compose.test.yml up -d --build --wait`. La sonde ne mesure pas le throttling CPU (cAdvisor n'y est pas déployé) : cette mesure reste celle du cluster (section 8).

### 13. Reproduire la mesure du rattrapage (essai 2)

```bash
# Débit 100 msg/s et tampon de 30 000
kubectl set env deploy/telecom-generator-deployment -n ingestion RATE=100 BUFFER_MAX=30000
kubectl rollout status deploy/telecom-generator-deployment -n ingestion

# Photo CPU de départ
kubectl exec -n ia deploy/telecom-ia-deployment -- grep -E "usage_usec|nr_throttled|throttled_usec" /sys/fs/cgroup/cpu.stat

# Panne de 3 minutes
kubectl scale statefulset telecom-broker-rabbitmq -n ingestion --replicas=0
sleep 180
kubectl scale statefulset telecom-broker-rabbitmq -n ingestion --replicas=1

# À lancer IMMÉDIATEMENT : relevé toutes les 10 s pendant le rattrapage
for i in $(seq 1 18); do date +%T; kubectl exec -n ia deploy/telecom-ia-deployment -- grep -E "usage_usec|nr_throttled|throttled_usec" /sys/fs/cgroup/cpu.stat; sleep 10; done

# Retour au débit nominal
kubectl set env deploy/telecom-generator-deployment -n ingestion RATE- BUFFER_MAX-
kubectl get pods -n ia
```

Pendant le rattrapage, `usage_usec` doit augmenter d'environ 5 000 000 par intervalle de 10 s (0,5 cœur) et `nr_throttled` croître vite. Ensuite, regarde les quatre requêtes de la section 8.0 sur la plage de la panne.

### 14. Nettoyage

```bash
kind delete cluster --name cloudia-v3
```

---

## 12. 🔍 Point de situation technique & limites

Les quatre causes du problème de la v2bis sont traitées : plafonds CPU/RAM, quotas par namespace, broker persistant et cluster multi-nœuds avec `nodeSelector`. Les mesures de la section 8 montrent le plafonnement de l'IA pendant un rattrapage de ≈ 18 000 messages.

**Limites connues**

*   **Panne de 5 minutes non rejouée.** Le correctif de l'IA (tentatives sans limite) est validé pour une panne de ≈ 3 min (section 8.5). L'absence de plafond est acquise par construction, mais une panne plus longue reste à rejouer pour le démontrer.
*   **Un seul passage par scénario.** Les valeurs de la section 8 sont des ordres de grandeur. La résolution des métriques cAdvisor est de 30 s, et la mise en veille du Mac interrompt Prometheus (trous dans les courbes). La vérité terrain est `cpu.stat`.
*   **Backlog de l'essai 2 non journalisé.** Les ≈ 18 000 messages sont calculés (100 msg/s × 180 s), pas relevés ; le générateur a été remplacé depuis. L'essai 1 est, lui, journalisé (3 569 messages).
*   **Tampon du générateur en mémoire.** Si le pod du générateur disparaît pendant une panne du broker, le tampon est perdu.
*   **Livraison au moins une fois.** Avec les confirmations d'envoi, un message peut être livré en double après une coupure ambiguë. L'IA acquitte toujours, même en cas d'erreur d'analyse : un message illisible est abandonné plutôt que de bloquer la file.
*   **Une seule réplique d'IA, Python monothread.** Même sans limite CPU, un processus Python n'utilise guère plus d'un cœur. Il n'y a pas d'autoscaling.
*   **Modèle statique.** L'Isolation Forest est entraîné au démarrage sur des données synthétiques ; `contamination=0.05` implique environ 5 % de faux positifs attendus.
*   **Secrets en clair.** `user` / `password` figurent dans les manifestes : prototype uniquement. En production, un `Secret` Kubernetes.
*   **Politiques réseau en entrée seulement.** Aucune politique de sortie (egress).
*   **Image RabbitMQ non épinglée** (`rabbitmq:3-management`) et `requests` non verrouillé dans `requirements.txt`.
*   **La sonde Compose n'est pas Kubernetes.** Elle valide l'image, la chaîne jusqu'à Grafana et la reprise après la perte du broker, avec les mêmes limites CPU/RAM, mais pas les ResourceQuotas, les `nodeSelector`, le PVC, les vraies NetworkPolicy ni le throttling mesuré par cAdvisor. Le tableau de bord provisionné n'a donc pas de panneau CPU ou mémoire.
*   **Sonde Compose : `docker compose kill` n'est pas une suppression de pod.** Le broker est tué (SIGKILL) puis redémarré avec le même volume, sans passer par un orchestrateur : ce test ne remplace pas celui du workflow Kind.
*   **GPU.** Non disponibles en local : le principe du `nodeSelector` est démontré, le matériel reste une cible d'architecture.
