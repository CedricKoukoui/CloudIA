# Utilisation d'une image Python officielle et légère
FROM python:3.10-slim

# Définition du répertoire de travail dans le conteneur
WORKDIR /app

# Copie et installation des dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du script d'IA
COPY app.py .

# Commande d'exécution du script
CMD ["python", "app.py"]

