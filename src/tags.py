"""
Recuperation des tags Etsy optimises via l'API eRank scraper locale.

Appelle l'endpoint `GET /best-tags?q=<tag>&n=13` de l'API FastAPI
(par defaut http://127.0.0.1:8765) et formate la reponse pour Etsy.

Contraintes Etsy appliquees automatiquement :
- max 13 tags par fiche
- chaque tag <= 20 caracteres
- lowercase
- pas de doublons

Le tag de base fourni est toujours inclus en premier (au cas ou l'API
eRank ne le retourne pas dans son top).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

ERANK_API_URL = os.environ.get("ERANK_API_URL", "http://127.0.0.1:8765")
ERANK_API_TOKEN = os.environ.get("ERANK_API_TOKEN")  # facultatif

ETSY_TAG_MAX_CHARS = 20
ETSY_MAX_TAGS = 13


def _clean_tag(tag: str) -> str:
    """Normalise un tag pour qu'il respecte les conventions Etsy."""
    return tag.strip().lower()


def get_optimized_tags(
    base_tag: str,
    *,
    country: str = "USA",
    pages: int = 20,
    n: int = ETSY_MAX_TAGS,
) -> list[str]:
    """Recupere les meilleurs tags Etsy pour un tag de base.

    Args:
        base_tag: tag de base (ex: "peluche", "bougie lavande").
        country: code pays pour la recherche eRank (USA, GLO, EEA, ...).
        pages: nombre de pages OpenSearch a parcourir (plus = plus precis mais plus long).
        n: nombre max de tags renvoyes (plafonne a 13 pour Etsy).

    Returns:
        Liste de tags conformes Etsy (lowercase, <=20 chars, dedupliques).
        Si l'API n'est pas joignable ou echoue, fallback sur [base_tag].
    """
    headers = {}
    if ERANK_API_TOKEN:
        headers["X-API-Key"] = ERANK_API_TOKEN

    try:
        resp = requests.get(
            f"{ERANK_API_URL}/best-tags",
            params={
                "q": base_tag,
                "n": min(n, ETSY_MAX_TAGS),
                "pages": pages,
                "country": country,
            },
            headers=headers,
            timeout=180,
        )
    except requests.exceptions.ConnectionError:
        print(
            f"  /!\\ API eRank injoignable a {ERANK_API_URL}. "
            "Demarre-la avec `python api.py` dans le dossier erank.",
            file=sys.stderr,
        )
        print("  Fallback : tag de base seul.", file=sys.stderr)
        return [_clean_tag(base_tag)]
    except requests.exceptions.Timeout:
        print(
            "  /!\\ API eRank trop lente (>180s). Fallback : tag de base seul.",
            file=sys.stderr,
        )
        return [_clean_tag(base_tag)]

    if resp.status_code != 200:
        print(
            f"  /!\\ API eRank : HTTP {resp.status_code} - {resp.text[:200]}",
            file=sys.stderr,
        )
        return [_clean_tag(base_tag)]

    data = resp.json()
    raw_terms = [item.get("term", "") for item in data.get("top", [])]

    # Le tag de base est garanti en tete
    candidates = [base_tag, *raw_terms]

    tags: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        cleaned = _clean_tag(term)
        if not cleaned or len(cleaned) > ETSY_TAG_MAX_CHARS or cleaned in seen:
            continue
        tags.append(cleaned)
        seen.add(cleaned)
        if len(tags) >= ETSY_MAX_TAGS:
            break

    return tags


def main(argv: list[str]) -> int:
    """CLI de test : python -m src.tags <tag_de_base>"""
    if len(argv) < 2:
        print("Usage : python -m src.tags <tag_de_base>")
        return 1

    base_tag = " ".join(argv[1:])
    print(f"Recherche eRank pour : '{base_tag}'...")
    tags = get_optimized_tags(base_tag)
    print(f"\n{len(tags)} tag(s) retenus pour Etsy :")
    for i, t in enumerate(tags, 1):
        print(f"  {i:>2}. {t}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
