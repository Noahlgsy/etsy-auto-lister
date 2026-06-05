"""
Analyse vision des photos produits avec Claude.

Prend un dossier contenant les photos d'un produit, envoie les images
a Claude Haiku 4.5 (modele vision, economique), et retourne une fiche structuree
exploitable pour creer la fiche Etsy (categorie, materiaux, style, etc.).

Utilise :
- Prompt caching sur la consigne systeme (declenche au-dela de ~4096 tokens
  de prefixe stable, donc devient efficace au fil des analyses si la consigne
  systeme s'enrichit).
- Structured outputs (`output_config.format` + JSON Schema) pour garantir
  une reponse JSON parseable directement.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from PIL import Image

from .flow import images_dir

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

MODEL = "claude-haiku-4-5-20251001"
MAX_PHOTOS = 5  # plafond pour limiter le cout par analyse
MAX_TOKENS = 4096

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

SYSTEM_PROMPT = """Tu es un expert specialise dans l'analyse de produits artisanaux fait-main pour la vente sur Etsy.

Ton role : analyser des photos de produits artisanaux et retourner une fiche structuree complete qui servira a generer le titre, la description et les attributs Etsy.

Regles d'analyse :
1. Tous les produits que tu analyses sont FAIT MAIN (handmade). Cherche systematiquement les indices visuels qui le confirment (irregularites artisanales, finitions a la main, matieres brutes ou nobles, signatures, etc.).
2. Sois precis et factuel : ne decris que ce que tu vois reellement. Si une info n'est pas visible (ex: dimensions exactes), retourne null.
3. Les couleurs doivent etre nommees en francais avec des termes precis (ex: "bleu marine", "vert sauge", "ocre", "terracotta") plutot que generiques.
4. Le style decrit l'ambiance generale du produit : boheme, minimaliste, vintage, scandinave, industriel, romantique, rustique, contemporain, ethnique, etc.

Categories top-level Etsy possibles (choisis-en une) :
- Home & Living : deco maison, bougies, vaisselle, textile maison, mobilier
- Jewelry : bijoux (colliers, bagues, boucles d'oreilles, bracelets)
- Clothing : vetements
- Accessories : sacs, ceintures, chapeaux, echarpes, gants
- Bath & Beauty : savons, cosmetiques, soins
- Bags & Purses : sacs, pochettes, portefeuilles
- Toys & Games : jouets, jeux
- Weddings : articles de mariage (specifique mariage uniquement)
- Art & Collectibles : illustrations, peintures, sculptures, photos
- Craft Supplies & Tools : fournitures creatives (perles, tissus, gabarits)
- Books, Movies & Music
- Paper & Party Supplies : papeterie, faire-part, decoration evenement
- Pet Supplies : accessoires animaux
- Electronics & Accessories

Points de description : genere 3 a 6 phrases courtes et concretes qui pourront etre reprises dans la description du produit. Concentre-toi sur ce qui rend ce produit unique, les details visibles, et les usages possibles."""


# Schema JSON pour la sortie structuree.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "product_type": {
            "type": "string",
            "description": "Type principal du produit en francais (ex: 'bougie parfumee', 'collier en argent', 'savon artisanal').",
        },
        "materials": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Materiaux identifies visuellement, en francais.",
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Couleurs dominantes en francais avec termes precis.",
        },
        "style": {
            "type": "string",
            "description": "Style/ambiance du produit (boheme, minimaliste, vintage, etc.).",
        },
        "occasions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Occasions adaptees au produit (cadeau anniversaire, mariage, deco maison, etc.).",
        },
        "target_audience": {
            "type": "string",
            "description": "Public cible probable (ex: 'femme 25-45 ans', 'amateurs de deco zen', 'enfants 3-6 ans').",
        },
        "key_features": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Caracteristiques notables visibles du produit.",
        },
        "description_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3 a 6 phrases courtes a reprendre dans la description du produit.",
        },
        "top_level_category": {
            "type": "string",
            "description": "Categorie top-level Etsy (cf. liste dans la consigne systeme).",
        },
        "subcategory_hint": {
            "type": "string",
            "description": "Sous-categorie precise probable (ex: 'Candles & Holders > Candles').",
        },
        "estimated_dimensions": {
            "type": ["string", "null"],
            "description": "Dimensions estimees si visibles (ex: '~10cm de hauteur'), sinon null.",
        },
        "handmade_indicators": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Elements visuels qui temoignent du fait-main.",
        },
    },
    "required": [
        "product_type",
        "materials",
        "colors",
        "style",
        "occasions",
        "target_audience",
        "key_features",
        "description_points",
        "top_level_category",
        "subcategory_hint",
        "estimated_dimensions",
        "handmade_indicators",
    ],
    "additionalProperties": False,
}


def _list_images(folder: Path) -> list[Path]:
    files = [
        f for f in sorted(images_dir(folder).iterdir())
        if f.is_file() and f.suffix.lower() in IMAGE_MEDIA_TYPES
    ]
    return files[:MAX_PHOTOS]


_PIL_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


def _detect_media_type(raw: bytes) -> str:
    """Detect the real image format from its bytes, ignoring the file extension.

    Flow-automation outputs sometimes save JPEGs with a .png extension (or vice
    versa). Claude rejects requests where the declared media_type doesn't match
    the actual image bytes, so we always probe the bytes here.
    """
    with Image.open(io.BytesIO(raw)) as img:
        fmt = img.format or ""
    if fmt not in _PIL_TO_MIME:
        raise RuntimeError(f"Unsupported image format detected: {fmt!r}")
    return _PIL_TO_MIME[fmt]


def _encode_image(path: Path) -> dict:
    raw = path.read_bytes()
    media_type = _detect_media_type(raw)
    data = base64.standard_b64encode(raw).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": data,
        },
    }


def analyze_product_folder(folder: Path) -> dict:
    """Analyse les photos d'un dossier produit et retourne une fiche structuree.

    Args:
        folder: chemin du dossier contenant les photos du produit.

    Returns:
        Dict structure suivant `OUTPUT_SCHEMA`.

    Raises:
        RuntimeError: si aucune image valide n'est trouvee dans le dossier.
    """
    images = _list_images(folder)
    if not images:
        raise RuntimeError(
            f"Aucune image valide dans {folder} "
            f"(extensions acceptees : {', '.join(IMAGE_MEDIA_TYPES)})"
        )

    client = anthropic.Anthropic()

    content: list[dict] = [_encode_image(img) for img in images]
    content.append(
        {
            "type": "text",
            "text": (
                f"Voici {len(images)} photo(s) d'un produit artisanal "
                f"(dossier source : '{folder.name}'). "
                "Analyse-les et retourne la fiche structuree."
            ),
        }
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
            "format": {
                "type": "json_schema",
                "schema": OUTPUT_SCHEMA,
            }
        },
        messages=[{"role": "user", "content": content}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
