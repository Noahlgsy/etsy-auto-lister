#!/bin/bash
# Lanceur de l'Atelier Etsy — double-clique ce fichier dans le Finder.
# Démarre le serveur web local sur http://127.0.0.1:8000 et l'ouvre dans
# ton navigateur. Le serveur tourne tant que cette fenêtre Terminal reste
# ouverte (ferme-la ou Ctrl-C pour l'arrêter).

cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "Environnement Python introuvable (.venv). Lance d'abord l'installation."
  read -r -p "Appuie sur Entrée pour fermer."
  exit 1
fi

# Si le port 8000 répond déjà, on ouvre simplement le navigateur.
if curl -fsS -o /dev/null http://127.0.0.1:8000/api/config 2>/dev/null; then
  echo "La bête tourne déjà. Ouverture du navigateur…"
  open http://127.0.0.1:8000/
  exit 0
fi

echo "Démarrage de l'Atelier Etsy sur http://127.0.0.1:8000 …"
# Ouvre le navigateur dès que le serveur répond (en tâche de fond).
( for _ in $(seq 1 40); do
    curl -fsS -o /dev/null http://127.0.0.1:8000/api/config 2>/dev/null && { open http://127.0.0.1:8000/; break; }
    sleep 0.5
  done ) &

exec .venv/bin/python -m web.app
