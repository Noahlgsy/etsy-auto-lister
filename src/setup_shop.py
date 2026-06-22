"""
Decouvre et sauvegarde le shop_id Etsy d'une boutique dans .env.

A lancer une fois apres l'auth OAuth d'un slot :
    python -m src.setup_shop                # boutique 1 (cles historiques)
    python -m src.setup_shop --slot 2       # boutique 2 (cles ETSY_SHOP2_*)

Slot 1  -> ecrit ETSY_SHOP_ID        (lit ETSY_USER_ID / ETSY_REFRESH_TOKEN)
Slot N>=2 -> ecrit ETSY_SHOP{N}_ID   (lit ETSY_SHOP{N}_USER_ID / _REFRESH_TOKEN)

Important : a ce stade la boutique n'a PAS encore de shop_id dans .env, donc
elle n'est pas listee par le registre (src/shops). On ne peut donc pas passer
par get_api_headers() (qui resout une boutique deja configuree) : on signe
directement avec le refresh_token brut du slot via headers_for_refresh_token().
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

from . import shops
from .auth import headers_for_refresh_token

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
ENV_PATH = Path(os.environ.get("ETSY_ENV_FILE") or (Path(__file__).resolve().parent.parent / ".env"))


def _api_get(path: str, headers: dict, params: dict | None = None) -> requests.Response:
    return requests.get(f"{ETSY_API_BASE}{path}", headers=headers, params=params, timeout=30)


def main(slot: str | int = "1") -> int:
    slot = str(slot).strip()
    load_dotenv(ENV_PATH, override=True)

    names = shops._env_names(slot)
    user_id = (os.environ.get(names["user_id"]) or "").strip()
    refresh_token = (os.environ.get(names["refresh_token"]) or "").strip()
    if not (user_id and refresh_token):
        print(
            f"ERREUR : {names['user_id']} / {names['refresh_token']} manquant. "
            f"Lance d'abord `python -m src.auth --slot {slot}`."
        )
        return 1

    # Signe avec le refresh_token brut du slot (le shop_id n'est pas encore
    # connu). Le token tourne et est reecrit sur sa propre cle.
    headers = headers_for_refresh_token(refresh_token, env_key=names["refresh_token"])

    # 1. Sanity check via l'endpoint user (qui necessite OAuth)
    print(f"Test de connexion (slot {slot}, user_id={user_id})...")
    user_resp = _api_get(f"/users/{user_id}", headers)
    if user_resp.status_code != 200:
        print(f"  Echec : {user_resp.status_code} - {user_resp.text}")
        return 1
    user_data = user_resp.json()
    print(
        f"  Compte : {user_data.get('login_name', '?')} "
        f"({user_data.get('primary_email', '?')})"
    )

    # 2. Trouver la boutique
    print("\nRecherche de ta boutique...")
    shop_id: int | None = None

    # Approche 1 : /users/{user_id}/shops
    shops_resp = _api_get(f"/users/{user_id}/shops", headers)
    if shops_resp.status_code == 200:
        data = shops_resp.json()
        if isinstance(data, dict):
            if data.get("shop_id"):
                shop_id = data["shop_id"]
            elif data.get("results"):
                shop_id = data["results"][0].get("shop_id")
        elif isinstance(data, list) and data:
            shop_id = data[0].get("shop_id")

    if shop_id:
        print(f"  Boutique detectee automatiquement : shop_id={shop_id}")
    else:
        # Approche 2 : demander le shop name a l'utilisateur
        print("  Detection auto impossible (peut-etre que ta boutique n'est pas encore 'ouverte').")
        print("  Ton URL boutique Etsy est de la forme etsy.com/shop/NOM_BOUTIQUE")
        shop_name = input("  Tape exactement le NOM_BOUTIQUE : ").strip()
        if not shop_name:
            print("  Aucun nom fourni, abandon.")
            return 1

        find_resp = _api_get("/shops", headers, params={"shop_name": shop_name})
        if find_resp.status_code != 200:
            print(f"  Echec recherche : {find_resp.status_code} - {find_resp.text}")
            return 1
        results = find_resp.json().get("results", [])
        if not results:
            print(f"  Aucune boutique trouvee pour '{shop_name}'.")
            return 1

        # Match exact si possible
        for r in results:
            if r.get("shop_name", "").lower() == shop_name.lower():
                shop_id = r["shop_id"]
                print(f"  Boutique trouvee : {r.get('shop_name')} (shop_id={shop_id})")
                break
        if not shop_id:
            shop_id = results[0]["shop_id"]
            print(
                f"  Pas de match exact, prise du premier resultat : "
                f"{results[0].get('shop_name')} (shop_id={shop_id})"
            )

    set_key(str(ENV_PATH), names["shop_id"], str(shop_id))
    print(f"\nshop_id sauvegarde dans .env : {names['shop_id']}={shop_id}")
    if slot != "1":
        print(
            f"Boutique {slot} prete : elle apparaitra dans le selecteur « 🏪 » "
            f"de l'atelier (rafraichis la page)."
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Decouvre et enregistre le shop_id Etsy d'une boutique."
    )
    parser.add_argument(
        "--slot",
        default="1",
        help="Numero de boutique : 1 (par defaut, cles historiques) ou 2..9.",
    )
    args = parser.parse_args()
    sys.exit(main(args.slot))
