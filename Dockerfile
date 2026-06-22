# Image cloud de l'Atelier Etsy — UNIQUEMENT les onglets Ventes / Trésorerie /
# Comptabilité (la génération d'images Flow reste en local, hors de ce conteneur).
FROM python:3.12-slim

# Dépendances système minimales pour Pillow (zlib/libjpeg) au cas où.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dépendances Python (couche cachée tant que requirements ne change pas).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif (le .dockerignore exclut .env, data/, .venv, .git…).
COPY src ./src
COPY web ./web
COPY batch.txt.example ./batch.txt.example
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

# Variables fixées par l'image ; les SECRETS arrivent par `fly secrets`.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    CLOUD_MODE=1 \
    FINANCE_DB=/data/finance.db \
    ETSY_ENV_FILE=/data/.env \
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]
