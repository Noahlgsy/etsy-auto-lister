"""Fetch and cache Etsy seller taxonomy, look up taxonomy_id by category names.

The Etsy taxonomy is fetched once and cached locally to avoid repeated API
calls. The lookup tries to find the best match given a top-level category
(e.g. "Home & Living") and an optional sub-category hint (e.g. "Candles").
"""

from __future__ import annotations

import json
from pathlib import Path

import requests

from .auth import get_api_headers

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
CACHE_PATH = Path(__file__).resolve().parent.parent / "taxonomy_cache.json"


def _fetch_taxonomy() -> list[dict]:
    headers = get_api_headers()
    resp = requests.get(
        f"{ETSY_API_BASE}/seller-taxonomy/nodes",
        headers=headers,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch Etsy taxonomy: {resp.status_code} - {resp.text[:300]}"
        )
    return resp.json().get("results", [])


def _ensure_taxonomy() -> list[dict]:
    if CACHE_PATH.is_file():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    print("Fetching Etsy taxonomy (one-time, ~3000 nodes)...")
    nodes = _fetch_taxonomy()
    CACHE_PATH.write_text(
        json.dumps(nodes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  Cached to {CACHE_PATH.name}")
    return nodes


def _find_top_level(nodes: list[dict], name: str) -> dict | None:
    name_lower = name.lower().strip()
    for node in nodes:
        if node.get("name", "").lower() == name_lower:
            return node
    # Try a partial / contained match
    for node in nodes:
        if name_lower in node.get("name", "").lower():
            return node
    return None


def _walk(node: dict):
    yield node
    for child in node.get("children", []) or []:
        yield from _walk(child)


def _best_subcategory(top_level: dict, hint: str) -> dict | None:
    """Find the descendant node whose name best overlaps with the hint."""
    hint_words = {w for w in hint.lower().replace(">", " ").split() if len(w) > 2}
    if not hint_words:
        return None

    best_node = None
    best_score = 0
    for node in _walk(top_level):
        if node is top_level:
            continue
        node_words = {w for w in node.get("name", "").lower().split() if len(w) > 2}
        common = len(hint_words & node_words)
        # Tie-break: deeper nodes (more specific) win
        depth = node.get("level", 0)
        score = common * 10 + depth
        if common > 0 and score > best_score:
            best_node = node
            best_score = score
    return best_node


def find_taxonomy_id(top_level_name: str, subcategory_hint: str | None = None) -> int:
    """Find an Etsy taxonomy_id given category names.

    Args:
        top_level_name: top-level Etsy category (e.g. "Home & Living").
        subcategory_hint: optional sub-category guess (e.g. "Candles & Holders > Candles").

    Returns:
        Etsy taxonomy_id (int).

    Raises:
        RuntimeError: if the top-level category cannot be matched.
    """
    nodes = _ensure_taxonomy()
    top = _find_top_level(nodes, top_level_name)
    if not top:
        available = sorted({n.get("name", "") for n in nodes})
        raise RuntimeError(
            f"Top-level category not found in Etsy taxonomy: {top_level_name!r}. "
            f"Available top-level: {available}"
        )

    if subcategory_hint:
        match = _best_subcategory(top, subcategory_hint)
        if match:
            return match["id"]

    return top["id"]
