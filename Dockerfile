# Utilisation d'une image Python officielle et légère
FROM python:3.10-slim

# Définition du répertoire de travail dans le conteneur
WORKDIR /app

# Copie et installation des dépendances (v6 : + client MLflow et pandas pour le registre de modèles)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# v6 : journaux plus sobres (pas de message d'aide de MLflow au démarrage de chaque pod)
ENV MLFLOW_DISABLE_AGENT_HINT=1 \
    PYTHONUNBUFFERED=1

# Copie du code (app.py, generator.py, train.py...). Le fichier .dockerignore exclut .git, .venv,
# les clés et certificats, la documentation.
COPY . .

# Commande par défaut : le moteur d'IA. Le générateur et l'entraînement redéfinissent la commande.
CMD ["python", "app.py"]
