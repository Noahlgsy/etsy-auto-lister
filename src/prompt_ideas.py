"""
Génère des prompts d'images pour Google Flow à partir d'une description produit.

À partir d'un produit décrit en texte (ex : « t-shirt bleu »), demande à Claude
Haiku (économique) de rédiger N prompts de mise en scène prêts à envoyer à Flow.
Chaque prompt restage la photo produit existante dans un décor différent tout en
mettant l'objet en valeur (porté, en situation, fonds variés, lumière soignée).

Sortie structurée (JSON Schema) -> liste de chaînes directement exploitable.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024

SYSTEM_PROMPT = """Tu es un directeur artistique spécialisé en photographie de produits pour des fiches Etsy.

On te donne la description d'un produit fait-main. Tu dois écrire des PROMPTS de mise en scène destinés à un générateur d'images IA (Google Flow) qui part d'UNE photo existante du produit et la restage selon ton texte.

Règles pour chaque prompt :
- Écris en français, à l'impératif, sur une seule ligne, sans numéro ni guillemets.
- Garde le produit IDENTIQUE (mêmes couleurs, forme, motifs) : tu changes seulement le décor / la mise en scène, jamais le produit lui-même.
- Mets l'objet bien EN VALEUR : il reste le sujet principal, net et bien éclairé.
- VARIE fortement les scènes d'un prompt à l'autre : produit porté/utilisé par une personne, mise en situation lifestyle, fond studio épuré, ambiance intérieure chaleureuse, extérieur naturel, gros plan sur un détail, etc.
- Reste réaliste et vendeur (lumière douce, cadrage soigné, décor crédible).
- Référence la photo fournie par « sur cette image » ou « ce produit » quand c'est utile.
- Chaque prompt fait 1 à 2 phrases, concret et directement exploitable.

Tu réponds UNIQUEMENT via le schéma de sortie structuré."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        }
    },
    "required": ["prompts"],
    "additionalProperties": False,
}


def generate_image_prompts(product: str, n: int = 5) -> list[str]:
    """Retourne ~`n` prompts de mise en scène pour le produit décrit.

    Args:
        product: description libre du produit (ex : « t-shirt bleu »).
        n: nombre de prompts souhaités (borné 1..10).

    Returns:
        Liste de chaînes (prompts), longueur <= n.

    Raises:
        ValueError: si la description produit est vide.
        anthropic.APIError: en cas d'erreur de l'API Anthropic.
    """
    product = (product or "").strip()
    if not product:
        raise ValueError("Description produit vide.")
    n = max(1, min(int(n), 10))

    client = anthropic.Anthropic()
    user_message = (
        f"Produit : {product}\n\n"
        f"Rédige exactement {n} prompts de mise en scène DIFFÉRENTS pour ce produit, "
        "chacun dans un décor distinct, en mettant bien l'objet en valeur."
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}
        },
        messages=[{"role": "user", "content": user_message}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    prompts = [str(p).strip() for p in data.get("prompts", []) if str(p).strip()]
    return prompts[:n]
