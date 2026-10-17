"""Logique du modèle d'IA (v6), sans dépendance au registre : données, entraînement, porte de qualité.

Jusqu'à la v5, chaque pod de l'IA tirait ses propres données au hasard (np.random.normal SANS graine)
puis entraînait son propre Isolation Forest au démarrage. Avec 10 pods, on obtenait 10 modèles
légèrement différents : deux pods pouvaient classer différemment le même message.

En v6, ce module n'est utilisé que par l'entraînement CENTRALISÉ (train.py, Job Kubernetes) :
données reproductibles (graine fixée), entraînement unique, validation, puis publication dans le
registre MLflow. Les pods de l'IA ne s'entraînent plus : ils téléchargent le modèle validé.
"""
import numpy as np
from sklearn.ensemble import IsolationForest

FEATURES = ["packets", "errors", "latency"]

# Référence du trafic normal (identique à celle de la v5 et du générateur) : 100 paquets, 1 % d'erreurs, 20 ms
NORMAL_MEAN = [100, 1, 20]
NORMAL_STD = [10, 0.5, 2]

# Anomalies émises par generator.py (bornes de random.randint) : servent à la validation
ANOMALY_LOW = [500, 8, 90]
ANOMALY_HIGH = [800, 15, 150]

# Porte de qualité : un modèle qui ne la passe pas n'est jamais promu en production
MIN_DETECTION_RATE = 0.95    # part des anomalies détectées
MAX_FALSE_POSITIVE_RATE = 0.10  # part du trafic normal signalé à tort


def make_training_data(n_samples=500, seed=42):
    """Trafic normal de référence. La graine rend les données (et donc le modèle) reproductibles."""
    rng = np.random.default_rng(seed)
    return rng.normal(loc=NORMAL_MEAN, scale=NORMAL_STD, size=(n_samples, 3))


def train_model(data, seed=42, contamination=0.05, n_estimators=100):
    model = IsolationForest(contamination=contamination, n_estimators=n_estimators, random_state=seed)
    model.fit(data)
    return model


def make_validation_data(n_normal=2000, n_anomalies=500, seed=4242):
    """Jeu de validation indépendant de l'entraînement : trafic normal + anomalies du générateur."""
    rng = np.random.default_rng(seed)
    normal = rng.normal(loc=NORMAL_MEAN, scale=NORMAL_STD, size=(n_normal, 3))
    anomalies = rng.uniform(low=ANOMALY_LOW, high=ANOMALY_HIGH, size=(n_anomalies, 3))
    return normal, anomalies


def evaluate(model, seed=4242):
    """Taux de détection et taux de faux positifs sur le jeu de validation."""
    normal, anomalies = make_validation_data(seed=seed)
    detection_rate = float(np.mean(model.predict(anomalies) == -1))
    false_positive_rate = float(np.mean(model.predict(normal) == -1))
    return {"detection_rate": detection_rate, "false_positive_rate": false_positive_rate}


def passes_quality_gate(metrics):
    return (metrics["detection_rate"] >= MIN_DETECTION_RATE
            and metrics["false_positive_rate"] <= MAX_FALSE_POSITIVE_RATE)
