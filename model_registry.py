"""Accès au registre de modèles MLflow (v6), partagé par l'entraînement (train.py) et l'IA (app.py).

Le pod de l'IA ne s'entraîne pas : il demande au registre quelle version porte l'alias de production,
puis télécharge EXACTEMENT cette version (models:/<nom>/<version>, et non l'alias, pour qu'un
changement d'alias entre les deux appels ne mélange pas deux versions). Il calcule l'empreinte
SHA-256 du fichier du modèle et la compare à celle notée par l'entraînement : tous les pods lancés
après une même promotion utilisent donc le même modèle, octet pour octet, et le prouvent par une
métrique (telecom_model_info).

Si le registre est injoignable ou qu'aucune version n'est promue, le pod attend et réessaie : il ne
retombe JAMAIS sur un entraînement local (c'était le piège de la v5). Tant qu'il n'a pas de modèle,
il n'est pas « Ready » et ne consomme aucun message.
"""
import hashlib
import os
import tempfile
import time

import mlflow
from mlflow import MlflowClient

MODEL_FILE = "model.pkl"  # fichier écrit par mlflow.sklearn (format pickle)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_version(model_name, version, dst_dir):
    """Télécharge les fichiers d'une version précise du modèle. Retourne (dossier, empreinte du modèle)."""
    local = mlflow.artifacts.download_artifacts(artifact_uri=f"models:/{model_name}/{version}", dst_path=dst_dir)
    return local, file_sha256(os.path.join(local, MODEL_FILE))


def load_production_model(tracking_uri, model_name, alias, retry_delay=5):
    """Retourne (modèle, version, run_id, empreinte sha256). Bloque jusqu'à ce que ce soit possible."""
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    attempt = 0
    while True:
        attempt += 1
        try:
            mv = client.get_model_version_by_alias(model_name, alias)
            with tempfile.TemporaryDirectory() as tmp:
                local, fingerprint = download_version(model_name, mv.version, tmp)
                expected = (mv.tags or {}).get("model_sha256")
                if expected and expected != fingerprint:
                    raise RuntimeError(f"empreinte inattendue pour la version {mv.version} "
                                       f"({fingerprint[:12]} au lieu de {expected[:12]})")
                model = mlflow.sklearn.load_model(local)
            return model, str(mv.version), mv.run_id, fingerprint
        except Exception as e:
            if attempt == 1 or attempt % 6 == 0:
                print(f"⏳ Modèle « {model_name}@{alias} » indisponible dans le registre {tracking_uri} "
                      f"(tentative {attempt}) : {e}")
            time.sleep(retry_delay)
