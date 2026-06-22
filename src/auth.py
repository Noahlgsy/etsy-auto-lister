"""
Flow OAuth 2.0 (PKCE) pour l'Open API Etsy v3.

Usage initial (une seule fois) :
    python -m src.auth

Le script :
1. Ouvre ton navigateur sur la page d'autorisation Etsy
2. Recoit le callback en local (port 3003)
3. Echange le code contre access_token + refresh_token
4. Sauvegarde le refresh_token dans .env

Ensuite, get_access_token() permet d'obtenir un access_token frais
en utilisant le refresh_token. Appele cette fonction au debut de chaque
requete vers l'API Etsy.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv, set_key

from . import shops

ETSY_AUTH_URL = "https://www.etsy.com/oauth/connect"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
REDIRECT_URI = "http://localhost:3003/oauth/redirect"
CALLBACK_PORT = 3003
# transactions_r permet de lire les commandes (onglet Ventes). Si ton token a
# été créé sans ce scope, relance `python -m src.auth` pour ré-autoriser.
SCOPES = "listings_w listings_r listings_d shops_r shops_w transactions_r email_r"

ENV_PATH = Path(os.environ.get("ETSY_ENV_FILE") or (Path(__file__).resolve().parent.parent / ".env"))


def _generate_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    received: dict[str, str] = {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/oauth/redirect":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.received = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
            b"<h1>Autorisation recue</h1>"
            b"<p>Tu peux fermer cet onglet et revenir au terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args, **kwargs) -> None:
        pass


def run_initial_oauth(slot: int | str = 1) -> None:
    """Run the interactive PKCE flow and store the tokens for `slot`.

    `slot` selects which shop's env keys receive the refresh token + user id:
    slot 1 → the legacy unsuffixed keys (ETSY_REFRESH_TOKEN / ETSY_USER_ID);
    slot N≥2 → ETSY_SHOP{N}_REFRESH_TOKEN / ETSY_SHOP{N}_USER_ID. Log into the
    target Etsy account in your browser BEFORE authorising.
    """
    slot = str(slot).strip()
    load_dotenv(ENV_PATH)
    keystring = os.environ.get("ETSY_KEYSTRING")
    if not keystring:
        raise RuntimeError("ETSY_KEYSTRING manquant dans .env")

    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    auth_params = {
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "client_id": keystring,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{ETSY_AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Ouverture du navigateur pour autoriser l'app sur Etsy...")
    print(f"Si rien ne s'ouvre, colle cette URL : {auth_url}\n")
    webbrowser.open(auth_url)

    thread.join(timeout=300)
    server.server_close()

    received = _CallbackHandler.received
    if not received:
        raise RuntimeError("Aucune reponse recue dans les 5 minutes")
    if received.get("state") != state:
        raise RuntimeError("State invalide (possible CSRF) - relance le script")
    if "error" in received:
        raise RuntimeError(f"Etsy a rejete l'autorisation : {received['error']}")
    code = received.get("code")
    if not code:
        raise RuntimeError("Aucun code recu dans la callback")

    resp = requests.post(
        ETSY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": keystring,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Echec echange du code : {resp.status_code} - {resp.text}")
    tokens = resp.json()

    user_id = tokens["access_token"].split(".")[0]

    refresh_key = shops.refresh_token_env_key(slot)
    user_key = "ETSY_USER_ID" if slot == "1" else f"ETSY_SHOP{slot}_USER_ID"
    set_key(str(ENV_PATH), refresh_key, tokens["refresh_token"])
    set_key(str(ENV_PATH), user_key, user_id)

    print(f"Auth reussie (slot {slot}). refresh_token sauvegarde dans .env ({refresh_key}).")
    print(f"User ID Etsy : {user_id}")
    if slot != "1":
        print(f"Etape suivante : python -m src.setup_shop --slot {slot}")


def _xapikey() -> str:
    """The `keystring:shared_secret` value Etsy expects in `x-api-key`.

    The dev-app credentials are shared across every shop.
    """
    load_dotenv(ENV_PATH, override=True)
    keystring = os.environ.get("ETSY_KEYSTRING")
    shared_secret = os.environ.get("ETSY_SHARED_SECRET")
    if not (keystring and shared_secret):
        raise RuntimeError("ETSY_KEYSTRING ou ETSY_SHARED_SECRET manquant dans .env")
    return f"{keystring}:{shared_secret}"


def _refresh_access_token(refresh_token: str, env_key: str) -> str:
    """Exchange a refresh token for an access token.

    Etsy rotates refresh tokens (single-use); when a new one comes back it is
    written to `env_key` — which, for multi-shop, must be that shop's own key,
    NOT a shared one.
    """
    load_dotenv(ENV_PATH, override=True)
    keystring = os.environ.get("ETSY_KEYSTRING")
    if not (keystring and refresh_token):
        raise RuntimeError(
            "ETSY_KEYSTRING ou refresh_token manquant. "
            "Lance `python -m src.auth` une fois."
        )

    resp = requests.post(
        ETSY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": keystring,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Echec refresh : {resp.status_code} - {resp.text}")
    tokens = resp.json()

    new_refresh = tokens.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        set_key(str(ENV_PATH), env_key, new_refresh)

    return tokens["access_token"]


def get_access_token(shop=None) -> str:
    """Fresh access token for `shop` (a Shop, a shop key, or None = active shop)."""
    if isinstance(shop, shops.Shop):
        sh = shop
    elif shop is None:
        sh = shops.active_shop()
    else:
        sh = shops.get_shop(shop)
    return _refresh_access_token(sh.refresh_token, shops.refresh_token_env_key(sh.key))


def get_api_headers(shop=None) -> dict[str, str]:
    """Headers d'auth pour les appels a l'API Etsy v3 (boutique `shop`).

    `shop` peut etre un Shop, une cle de boutique ("1", "2", …) ou None pour
    la boutique active du contexte courant. Etsy exige le format
    `keystring:shared_secret` dans `x-api-key`, plus le bearer token OAuth.
    """
    return {
        "x-api-key": _xapikey(),
        "Authorization": f"Bearer {get_access_token(shop)}",
    }


def headers_for_refresh_token(
    refresh_token: str, env_key: str = "ETSY_REFRESH_TOKEN"
) -> dict[str, str]:
    """Auth headers built directly from a raw refresh token.

    Used during setup of a brand-new shop slot, before its shop_id is known (so
    it isn't yet listed by the registry). The rotated token is written to
    `env_key`.
    """
    return {
        "x-api-key": _xapikey(),
        "Authorization": f"Bearer {_refresh_access_token(refresh_token, env_key)}",
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Autorise l'app Etsy pour une boutique (flow OAuth PKCE)."
    )
    parser.add_argument(
        "--slot",
        default="1",
        help="Numero de boutique : 1 (par defaut, cles historiques) ou 2..9.",
    )
    args = parser.parse_args()
    run_initial_oauth(args.slot)
