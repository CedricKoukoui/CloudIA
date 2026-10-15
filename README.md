## 13. Version v4 : rétention des métriques et ressources de l'observabilité

La v4 part de la v3 et traite deux sujets liés à Prometheus.

*   **Q13 (stockage) :** « Prometheus va finir par saturer le disque du cluster. Quelle stratégie évite la panne de stockage ? » Réponse : une **stratégie de rétention** (durée et taille) sur un volume dédié.
*   **Rattrapage de la v3.** La question initiale (« si l'IA sature le CPU, comment garantir que RabbitMQ ou Prometheus ne crashent pas ? ») n'était traitée que pour RabbitMQ. Prometheus et Grafana tournent dans le namespace `default` avec les valeurs par défaut du chart Helm : ni `requests/limits`, ni `nodeSelector`, ni `ResourceQuota`. Ils n'étaient protégés qu'indirectement, par le plafond de 500m de l'IA.

### 13.1 Fichiers ajoutés ou modifiés

| Fichier | Rôle | Pilier |
|---|---|---|
| `observability-values.yaml` | Valeurs Helm de `kube-prometheus-stack` : `resources` de chaque conteneur, `nodeSelector`, volume de Prometheus, rétention. | A, C, Q13 |
| `03-observability-quota.yaml` | `ResourceQuota` et `LimitRange` du namespace `default`. | A, B |
| `.github/workflows/ci-cd.yaml` | Déclenché aussi sur la branche `v4`. Valide les valeurs d'observabilité (rétention inférieure au volume, ressources déclarées, `nodeSelector`) et le nouveau manifeste. | CI |

### 13.2 Les trois piliers appliqués à l'observabilité

| Composant | `requests` | `limits` | Placement (`nodeSelector`) |
|---|---|---|---|
| Prometheus | 200m / 512Mi | 500m / 1Gi | `workload=ingestion` |
| Grafana | 100m / 128Mi | 200m / 256Mi | `workload=ingestion` |
| Grafana, 2 conteneurs auxiliaires (sidecars) | 25m / 64Mi chacun | 50m / 128Mi chacun | (dans le pod Grafana) |
| Opérateur Prometheus | 50m / 64Mi | 100m / 128Mi | `workload=ingestion` |
| config-reloader (Prometheus, Alertmanager) | 25m / 32Mi chacun | 50m / 64Mi chacun | (dans les pods) |
| Alertmanager | 25m / 64Mi | 100m / 128Mi | `workload=ingestion` |
| kube-state-metrics | 25m / 64Mi | 50m / 128Mi | `workload=ingestion` |
| node-exporter (DaemonSet, 3 nœuds) | 25m / 32Mi par pod | 50m / 64Mi par pod | aucun : un pod par nœud, borné par ses limites |

*   **Pilier A (cgroups)** : chaque conteneur a ses `requests` et `limits`. Un composant d'observabilité ne peut plus prendre la CPU des autres, ni être tué à cause d'un autre.
*   **Pilier B (namespace)** : `03-observability-quota.yaml` plafonne le total du namespace. Le `LimitRange` donne des valeurs par défaut aux conteneurs qui n'en déclarent pas (sans lui, le quota refuserait par exemple les Jobs de l'opérateur).
*   **Pilier C (nœud)** : la stack d'observabilité est placée sur le worker `ingestion`, jamais sur le worker `compute` de l'IA. Seul node-exporter, un DaemonSet, tourne sur tous les nœuds.

### 13.3 Calcul du quota du namespace `default`

Somme des conteneurs du tableau précédent (3 nœuds, donc 3 pods node-exporter) :

| Ressource | Somme | Quota | Marge |
|---|---|---|---|
| `requests.cpu` | 575m | 1 | 74 % |
| `requests.memory` | 1120Mi | 1536Mi | 37 % |
| `limits.cpu` | 1300m | 2 | 54 % |
| `limits.memory` | 2240Mi | 3Gi | 37 % |

La marge absorbe un pod de Grafana supplémentaire pendant une mise à jour progressive et les conteneurs de courte durée. **Les `requests/limits` sont des hypothèses de départ** (méthode de vérification : section 8.0 et schéma d'architecture, pastilles V1 à V3).

### 13.4 Stratégie de rétention (Q13)

| Paramètre | Valeur | Rôle |
|---|---|---|
| `retention` | `15d` | Purge des données de plus de 15 jours. |
| `retentionSize` | `1GB` | Purge des plus anciens blocs dès que la base atteint 1 Go. La première limite atteinte (durée ou taille) l'emporte. |
| `storageSpec` | PVC `2Gi` | Volume dédié : la base n'écrit plus dans le disque éphémère du nœud. |
| `walCompression` | `true` | Compresse le journal d'écriture (WAL). |

*   **Pourquoi 1 Go pour un volume de 2 Gi (47 %) :** la TSDB a besoin de place en plus des blocs de données, pour le WAL et la compaction. Garder `retentionSize` sous 85 % du volume est la règle usuelle ; 50 % est une marge prudente.
*   **Comment dimensionner la rétention :** `espace ≈ durée de rétention (s) × échantillons ingérés par seconde × octets par échantillon` (1 à 2 octets par échantillon après compression, d'après la documentation de Prometheus). Le débit d'ingestion se mesure avec `rate(prometheus_tsdb_head_samples_appended_total[1h])`. Si `durée × débit × 2 octets` dépasse `retentionSize`, la limite de taille sera atteinte avant celle de 15 jours.

### 13.5 Mise en œuvre

```bash
git checkout v3 && git pull origin v3 && git checkout -b v4

# Version du chart déjà installé (colonne CHART, par exemple kube-prometheus-stack-XX.Y.Z)
helm list

helm repo update
helm upgrade telecom-monitor prometheus-community/kube-prometheus-stack \
  --version XX.Y.Z -f observability-values.yaml
kubectl get pods -n default -o wide -w      # attendre que tout soit Running (Ctrl+C ensuite)

kubectl apply -f 03-observability-quota.yaml
```

*   **Garder la version du chart** (`--version`) évite une montée de version majeure du chart, qui demanderait de mettre à jour les CRD à la main.
*   **Ordre :** l'`upgrade` d'abord (les nouveaux pods reçoivent leurs ressources), le quota ensuite.
*   **Effet de bord :** le volume de Prometheus change (disque éphémère puis PVC). L'historique des métriques précédentes est perdu.

### 13.6 Vérifications

```bash
# Placement : Prometheus, Grafana, opérateur, Alertmanager et kube-state-metrics sur le worker « ingestion »
kubectl get pods -n default -o wide

# Quota : colonne Used à comparer à Hard
kubectl describe resourcequota observability-quota -n default

# Ressources effectivement posées sur Prometheus
kubectl get pod -n default -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].spec.containers[*].resources}'
```

```promql
# Rétention configurée (octets) et espace utilisé par les blocs (octets)
prometheus_tsdb_retention_limit_bytes
prometheus_tsdb_storage_blocks_bytes

# Remplissage du volume de Prometheus (1 = plein)
kubelet_volume_stats_used_bytes{namespace="default",persistentvolumeclaim=~".*prometheus.*"}
  / kubelet_volume_stats_capacity_bytes{namespace="default",persistentvolumeclaim=~".*prometheus.*"}

# CPU et mémoire de Prometheus, à comparer à ses limites (500m, 1Gi)
sum(rate(container_cpu_usage_seconds_total{namespace="default",container="prometheus"}[2m]))
container_memory_working_set_bytes{namespace="default",container="prometheus"}

# Nombre de purges déclenchées par la limite de taille (augmente quand retentionSize est atteint)
prometheus_tsdb_size_retentions_total
```

*   Les fenêtres `[2m]` s'expliquent par le pas de collecte de cAdvisor (30 s : il faut au moins deux points, et une fenêtre de 4 fois le pas évite les trous).
*   **Test de la limite de taille :** réduire temporairement `retentionSize` (par exemple `50MB`) et faire un `helm upgrade`. La purge ne retire que des blocs déjà écrits sur disque (la TSDB les écrit toutes les 2 h) : il faut compter plusieurs heures avant de voir `prometheus_tsdb_size_retentions_total` augmenter. Remettre `1GB` ensuite.

### 13.7 Limites de la v4

*   **Pas de sous-échantillonnage (downsampling).** La conservation à long terme avec agrégation (Thanos ou Cortex, stockage objet MinIO) est la cible d'architecture ; elle n'est pas déployée ici.
*   **Valeurs non mesurées.** Les `requests/limits` du tableau 13.2 et le débit d'ingestion de Prometheus sont des hypothèses. À vérifier avec la méthode des pastilles V1 à V3 du schéma d'architecture.
*   **Quota du namespace `default`.** Il s'applique à tout pod créé dans ce namespace, pas seulement à la stack d'observabilité. Le `LimitRange` fournit des valeurs par défaut, mais un pod qui dépasserait le quota serait refusé.
*   **Pas de haute disponibilité de Prometheus** : une seule réplique, sur un nœud.
*   **Mac en veille :** la collecte s'interrompt (trous dans les courbes), comme en v3.

### Lignes à ajouter au registre des paramètres (section 6)

| Paramètre | Valeur | Défini dans | Justification |
|---|---|---|---|
| Ressources de la stack d'observabilité | voir tableau 13.2 | `observability-values.yaml` | Pilier A pour Prometheus, Grafana et leurs satellites. |
| Quota du namespace `default` | requests 1 CPU / 1536Mi, limits 2 CPU / 3Gi | `03-observability-quota.yaml` | Somme des conteneurs + marge de 30 % (section 13.3). |
| Valeurs par défaut des conteneurs | 50m / 64Mi (requests), 100m / 128Mi (limits) | `03-observability-quota.yaml` (`LimitRange`) | Évite le refus d'un pod sans `requests/limits`. |
| Rétention de Prometheus | 15 jours ou 1GB | `observability-values.yaml` (`retention`, `retentionSize`) | Q13 : la première limite atteinte purge les plus anciens blocs. |
| Volume de Prometheus | PVC 2Gi | `observability-values.yaml` (`storageSpec`) | `retentionSize` = 47 % du volume (marge pour WAL et compaction). |
| `nodeSelector` de l'observabilité | `workload=ingestion` | `observability-values.yaml` | Pilier C : jamais sur le worker de l'IA. |
