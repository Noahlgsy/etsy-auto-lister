#!/bin/sh
# Démarrage du conteneur cloud.
# Les secrets (clés Etsy, Anthropic) arrivent en variables d'environnement via
# `fly secrets`. On les sème UNE FOIS dans le volume persistant /data/.env :
# ainsi la rotation du refresh token Etsy (réécrit ce fichier) survit aux
# redéploiements. ETSY_ENV_FILE (=/data/.env) dit à l'appli où lire/écrire.
set -e

mkdir -p /data

if [ ! -f /data/.env ]; then
  echo "# Généré au 1er démarrage depuis les secrets de l'hébergeur." > /data/.env
  # Recopie toutes les variables Etsy + la clé Anthropic.
  env | grep -E '^(ETSY_|ANTHROPIC_API_KEY=)' >> /data/.env || true
  echo "entrypoint: /data/.env initialisé depuis les secrets."
fi

# batch.txt (prix/tag par défaut) — get_config a un repli, mais c'est plus propre.
[ -f /app/batch.txt ] || cp /app/batch.txt.example /app/batch.txt

exec python -m web.app
