"""Generate Etsy listing copy (title, description, complete 13-tag list, materials).

Takes the vision analysis output and the partial tag list from eRank, and
produces the final listing content via Claude. Uses structured outputs so the
result is guaranteed to match the expected schema (notably: exactly 13 tags).
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .etsy_client import ETSY_COLORS, sanitize_materials

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are an expert Etsy seller copywriter who specializes in handmade products.

Your job: given a product analysis (extracted from photos) and a partial list of SEO-optimized tags, generate the final Etsy listing copy.

You must produce:

1. **title** — a compelling Etsy listing title, MAX 140 characters.
   - Front-load high-value keywords (best-converting terms first)
   - Use commas or pipes to separate phrases
   - Include the main product type, key materials/features, target use case
   - No clickbait, no all-caps

2. **description** — a full product description.
   - Start with a 1-2 sentence hook describing what this is
   - Add a "Features" or "Details" section as a short bulleted list (use `- ` bullets)
   - Mention materials, approximate dimensions if known, occasions, who it's for
   - End with a short reassurance line about handmade quality / care
   - Length: 600-1200 characters total
   - Plain text (no markdown headers like `##`, but `- ` bullets are fine)

3. **tags** — EXACTLY 13 tags.
   - Each tag MAX 20 characters
   - Lowercase, no special chars except spaces and hyphens
   - No commas inside a tag
   - ALL 13 tags MUST be relevant to the listing's core theme/niche (the "base theme" given in the user message). This is the most important rule.
   - But VARY the wording — do NOT repeat the exact base phrase in every tag. Etsy already combines words across different tags in search, so repeating the same words 13 times wastes tag space and hurts SEO.
   - Include the exact base phrase in only ~2-4 of the tags. For the rest, stay inside the same niche/fandom but use DIFFERENT words: related characters, themes, sub-styles, audiences, occasions, and gift angles tied to that niche.
   - Use the partial list as a starting base (it is already centered on the base theme).
   - Do NOT add generic tags that have no connection to the base theme, even if they accurately describe the product.
   - All 13 must be DIFFERENT (no duplicates)
   - Prefer multi-word tags over single words (Etsy SEO best practice)

4. **materials** — up to 13 material strings.
   - Derived from the visual analysis
   - Each max 45 characters
   - Lowercase
   - Etsy only accepts letters, numbers and spaces here: NO accents, NO symbols
     (%, /, -, parentheses, &…). Write "coton" not "coton 100%", "polyester
     recycle" not "polyester recyclé".

5. **primary_color** — the single dominant color of the product.
   - MUST be one of this exact Etsy palette: Beige, Black, Blue, Bronze, Brown,
     Clear, Copper, Gold, Gray, Green, Orange, Pink, Purple, Rainbow, Red,
     Rose gold, Silver, White, Yellow.
   - Map nuanced colors to the closest palette entry (e.g. "bleu marine" -> Blue,
     "vert sauge" -> Green, "terracotta" -> Orange, "écru" -> Beige).

6. **secondary_color** — the second most present color, same palette as above,
   or null if the product is essentially one color.

Be specific and concrete. Avoid generic filler phrases. The product is HANDMADE."""

LANGUAGE_INSTRUCTIONS = {
    "en": "Write the title, description, tags, and materials in English (US English).",
    "fr": "Rédige le titre, la description, les tags et les matériaux en français.",
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Etsy listing title (max 140 chars).",
        },
        "description": {
            "type": "string",
            "description": "Full Etsy listing description (600-1200 chars).",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exactly 13 Etsy tags, each lowercase and <=20 chars.",
        },
        "materials": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Materials list (up to 13).",
        },
        "primary_color": {
            "type": "string",
            "enum": ETSY_COLORS,
            "description": "Dominant color, from the fixed Etsy palette.",
        },
        "secondary_color": {
            "type": ["string", "null"],
            "description": (
                "Second color, using one of the exact Etsy palette names "
                f"({', '.join(ETSY_COLORS)}), or null if the product is one color."
            ),
        },
    },
    "required": [
        "title", "description", "tags", "materials",
        "primary_color", "secondary_color",
    ],
    "additionalProperties": False,
}


def _clean_tag(tag: str) -> str:
    cleaned = tag.strip().lower()
    if len(cleaned) > 20:
        cleaned = cleaned[:20].strip()
    return cleaned


_COLOR_BY_LOWER = {c.lower(): c for c in ETSY_COLORS}


def _normalize_color(value) -> str | None:
    """Snap a color to the canonical Etsy palette casing, or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    return _COLOR_BY_LOWER.get(value.strip().lower())


def generate_listing(
    analysis: dict,
    partial_tags: list[str],
    *,
    language: str = "en",
    base_tag: str = "",
) -> dict:
    """Generate the listing copy.

    Args:
        analysis: output of `vision.analyze_product_folder()`.
        partial_tags: tags returned by `tags.get_optimized_tags()` (1-13 items).
        language: 'en' (default) or 'fr'.
        base_tag: the batch's base theme/niche; ALL 13 tags must relate to it.

    Returns:
        Dict with keys `title`, `description`, `tags` (exactly 13), `materials`.
    """
    client = anthropic.Anthropic()

    lang_instr = LANGUAGE_INSTRUCTIONS.get(language, LANGUAGE_INSTRUCTIONS["en"])
    system_full = f"{SYSTEM_PROMPT}\n\n## Output language\n{lang_instr}"

    theme = (base_tag or "").strip()
    theme_block = (
        "## Base theme (ALL 13 tags must relate to this)\n"
        f'"{theme}"\n'
        "Every tag must be tied to this theme/niche, but VARY the wording — "
        "include the exact phrase in only a few tags and otherwise use different "
        "words from the same niche (related characters, themes, audiences, "
        "occasions). Avoid generic product tags that drop the theme.\n\n"
    ) if theme else ""

    user_message = (
        f"{theme_block}"
        "## Product analysis (extracted from photos)\n"
        f"```json\n{json.dumps(analysis, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Partial SEO tags (from eRank)\n"
        f"There are {len(partial_tags)} tags already optimized for Etsy SEO:\n"
        f"```json\n{json.dumps(partial_tags, ensure_ascii=False)}\n```\n\n"
        "Generate the final listing copy. The `tags` array must contain "
        "EXACTLY 13 tags, and every tag must relate to the base theme above — "
        "use the partial list as a base and add varied theme-related tags "
        "(different words from the same niche) as needed."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_full,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}
        },
        messages=[{"role": "user", "content": user_message}],
    )

    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)

    # Defensive normalisation
    result["title"] = result["title"].strip()[:140]
    cleaned_tags: list[str] = []
    seen: set[str] = set()
    for t in result.get("tags", []):
        c = _clean_tag(t)
        if c and c not in seen:
            cleaned_tags.append(c)
            seen.add(c)
    # Enforce exactly 13 tags here (the schema can't constrain array length).
    while len(cleaned_tags) < 13:
        cleaned_tags.append(f"handmade {len(cleaned_tags)}")
    result["tags"] = cleaned_tags[:13]
    # Etsy-safe materials (letters/numbers/spaces, deduped, max 13).
    result["materials"] = sanitize_materials(
        [m.lower() for m in result.get("materials", [])]
    )
    # Colors snapped to the canonical Etsy palette (or None if unrecognised).
    result["primary_color"] = _normalize_color(result.get("primary_color"))
    result["secondary_color"] = _normalize_color(result.get("secondary_color"))

    return result
