"""Entraînement CENTRALISÉ du modèle (v6), lancé une seule fois par un Job Kubernetes (09-train-job.yaml).

  1. génère les données de référence avec une graine fixe (reproductibles) ;
  2. entraîne l'Isolation Forest ;
  3. le valide sur un jeu indépendant (porte de qualité de model_lib.py) ;
  4. l'enregistre dans le registre MLflow (nouvelle version du modèle MODEL_NAME) ;
  5. SEULEMENT s'il passe la porte de qualité : déplace l'alias MODEL_ALIAS (« production ») sur
     cette version. Les pods de l'IA ne chargent que la version désignée par cet alias.

Variables d'environnement :
  MLFLOW_TRACKING_URI  adresse du serveur MLflow (ex. http://mlflow.mlops.svc.cluster.local:5000)
  MODEL_NAME           nom du modèle dans le registre (défaut telecom-anomaly)
  MODEL_ALIAS          alias de production (défaut production)
  TRAINING_SEED        graine des données et du modèle (défaut 42)
  TRAINING_SAMPLES     nombre d'échantillons d'entraînement (défaut 500)
  TRAINING_CONTAMINATION  part d'anomalies supposée (défaut 0.05 ; 0.3 = modèle refusé, pour la démonstration)

Code de sortie : 0 si un modèle validé est promu, 1 sinon (le Job apparaît alors en échec).
"""
import os
import sys
import tempfile
import time

import mlflow
import sklearn
from mlflow import MlflowClient

import model_lib
import model_registry

TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
MODEL_NAME = os.environ.get("MODEL_NAME", "telecom-anomaly")
MODEL_ALIAS = os.environ.get("MODEL_ALIAS", "production")
SEED = int(os.environ.get("TRAINING_SEED", "42"))
SAMPLES = int(os.environ.get("TRAINING_SAMPLES", "500"))
# Part d'anomalies supposée dans les données. 0.05 par défaut ; une valeur forte (ex. 0.3) produit
# volontairement un modèle trop sensible, refusé par la porte de qualité (démonstration).
CONTAMINATION = float(os.environ.get("TRAINING_CONTAMINATION", "0.05"))
EXPERIMENT = "telecom-anomaly-training"


def wait_for_registry(timeout=180):
    """Le serveur MLflow peut démarrer après le Job : on attend qu'il réponde (sans dépasser timeout)."""
    client = MlflowClient()
    deadline = time.time() + timeout
    while True:
        try:
            client.search_experiments(max_results=1)
            return
        except Exception as e:
            if time.time() > deadline:
                sys.exit(f"❌ Registre MLflow injoignable ({TRACKING_URI}) : {e}")
            print(f"⏳ Registre MLflow pas encore prêt ({e.__class__.__name__}), nouvel essai dans 5 s...")
            time.sleep(5)


def main():
    mlflow.set_tracking_uri(TRACKING_URI)
    print(f"🏭 Entraînement centralisé : graine {SEED}, {SAMPLES} échantillons, contamination {CONTAMINATION}, registre {TRACKING_URI}")
    wait_for_registry()
    mlflow.set_experiment(EXPERIMENT)

    data = model_lib.make_training_data(n_samples=SAMPLES, seed=SEED)
    model = model_lib.train_model(data, seed=SEED, contamination=CONTAMINATION)
    metrics = model_lib.evaluate(model)
    passed = model_lib.passes_quality_gate(metrics)
    print(f"📏 Validation : détection {metrics['detection_rate']:.1%}, faux positifs {metrics['false_positive_rate']:.1%}"
          f" -> porte de qualité {'RÉUSSIE' if passed else 'ÉCHOUÉE'}")

    with mlflow.start_run(run_name=f"train-seed-{SEED}") as run:
        mlflow.log_params({
            "seed": SEED, "samples": SAMPLES, "contamination": CONTAMINATION, "n_estimators": 100,
            "sklearn_version": sklearn.__version__,
            "min_detection_rate": model_lib.MIN_DETECTION_RATE,
            "max_false_positive_rate": model_lib.MAX_FALSE_POSITIVE_RATE,
        })
        mlflow.log_metrics(metrics)
        mlflow.set_tag("quality_gate", "passed" if passed else "failed")
        mlflow.sklearn.log_model(sk_model=model, name="model", registered_model_name=MODEL_NAME,
                                 serialization_format="pickle")
        run_id = run.info.run_id

    client = MlflowClient()
    versions = client.search_model_versions(f"name='{MODEL_NAME}' and run_id='{run_id}'")
    if not versions:
        sys.exit("❌ La version du modèle n'a pas été créée dans le registre.")
    version = versions[0].version
    # Empreinte du fichier TEL QU'IL EST STOCKÉ dans le registre : chaque pod de l'IA recalcule la même
    # après téléchargement et refuse un fichier différent (stockage corrompu ou remplacé).
    with tempfile.TemporaryDirectory() as tmp:
        _, fingerprint = model_registry.download_version(MODEL_NAME, version, tmp)
    client.set_model_version_tag(MODEL_NAME, version, "quality_gate", "passed" if passed else "failed")
    client.set_model_version_tag(MODEL_NAME, version, "model_sha256", fingerprint)
    for name, value in metrics.items():
        client.set_model_version_tag(MODEL_NAME, version, name, f"{value:.4f}")

    if not passed:
        print(f"⛔ Version {version} enregistrée mais NON promue : l'alias « {MODEL_ALIAS} » reste sur l'ancienne version.")
        sys.exit(1)

    client.set_registered_model_alias(MODEL_NAME, MODEL_ALIAS, version)
    print(f"✅ Modèle {MODEL_NAME} version {version} promu : alias « {MODEL_ALIAS} » (empreinte {fingerprint[:12]}).")
    print("   Les pods de l'IA démarrés à partir de maintenant chargeront cette version ;"
          " les pods déjà lancés gardent la leur jusqu'à leur redémarrage (kubectl rollout restart).")


if __name__ == "__main__":
    main()
