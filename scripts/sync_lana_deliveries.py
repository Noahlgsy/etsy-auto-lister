#!/usr/bin/env python3
"""CLI : synchronise les dates de livraison depuis la boîte Gmail « Lana ».

Équivalent en ligne de commande du bouton « 🚚 Actualiser livraisons » du
dashboard. La logique vit dans web.finance.sync_deliveries() (source unique,
partagée avec l'endpoint /api/finance/sync-deliveries).

────────────────────────────────────────────────────────────────────────────
SETUP (une seule fois)
  1. Sur le compte Gmail de Lana : active la validation en 2 étapes.
  2. Crée un mot de passe d'application : https://myaccount.google.com/apppasswords
  3. Vérifie qu'IMAP est activé : Gmail › Paramètres › « Transfert et POP/IMAP ».
  4. Dans .env (racine du projet) :
        LANA_GMAIL=l24513610@gmail.com
        LANA_GMAIL_APP_PWD=xxxx xxxx xxxx xxxx
        # facultatif : LANA_SINCE=01-May-2026

LANCEMENT
        .venv/bin/python scripts/sync_lana_deliveries.py
────────────────────────────────────────────────────────────────────────────
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from web import finance  # noqa: E402


def main() -> int:
    r = finance.sync_deliveries()
    if not r.get("ok"):
        print("✗", r.get("error"))
        return 1
    print(
        f"✓ {r['updated']} date(s) de livraison ajoutée(s) "
        f"({r['detected']} livraison(s) détectée(s) dans les mails)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
