"""Etsy API client — create draft listings and upload images."""

from __future__ import annotations

import io
import os
import re
import unicodedata
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

from .auth import get_api_headers

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

MAX_IMAGES_PER_LISTING = 10
ETSY_RECOMMENDED_MAX_PX = 3000

# Etsy's fixed colour palette for the "Primary color" / "Secondary color"
# taxonomy properties. The listing copy generator must pick from this list so
# the values map to real Etsy property values.
ETSY_COLORS = [
    "Beige", "Black", "Blue", "Bronze", "Brown", "Clear", "Copper", "Gold",
    "Gray", "Green", "Orange", "Pink", "Purple", "Rainbow", "Red",
    "Rose gold", "Silver", "White", "Yellow",
]

_MATERIAL_MAX_LEN = 45


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def sanitize_material(material: str) -> str:
    """Return an Etsy-safe material string.

    Etsy's /materials field rejects anything other than letters, numbers and
    spaces (HTTP 400 "contains invalid characters"). Accented letters are
    stripped to ASCII, every other symbol (%, /, -, parentheses, commas…)
    becomes a space, runs of spaces collapse, and the result is capped at 45
    characters.
    """
    s = _strip_accents(str(material))
    s = re.sub(r"[^0-9A-Za-z ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:_MATERIAL_MAX_LEN].strip()


def sanitize_materials(materials: list[str]) -> list[str]:
    """Sanitize a list of materials, dropping empties/duplicates (max 13)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in materials or []:
        clean = sanitize_material(m)
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out[:13]

# Etsy allows ONE video per listing: 5-15s, MP4 recommended, <= 100 MB.
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mpeg", ".3gp", ".flv")
ETSY_MAX_VIDEO_BYTES = 100 * 1024 * 1024


def get_shop_id(shop=None) -> str:
    """Numeric shop_id for `shop` (a Shop, a shop key like "1"/"2", or None for
    the active shop of the current context)."""
    from . import shops  # lazy import keeps module import order simple

    if isinstance(shop, shops.Shop):
        return shop.shop_id
    if shop is None:
        return shops.active_shop().shop_id
    return shops.get_shop(shop).shop_id


def get_first_shipping_profile_id(shop_id: str) -> int | None:
    """Return the first shipping profile of the shop, or None if none exist."""
    headers = get_api_headers()
    resp = requests.get(
        f"{ETSY_API_BASE}/shops/{shop_id}/shipping-profiles",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to list shipping profiles: {resp.status_code} - {resp.text[:300]}"
        )
    results = resp.json().get("results", [])
    if not results:
        return None
    return results[0]["shipping_profile_id"]


# Etsy split "processing/readiness time" out of shipping profiles into
# "processing profiles" (a.k.a. readiness state definitions). Physical listings
# now require a readiness_state_id, created via the endpoint below.
_READINESS_ENDPOINT = "shops/{shop_id}/readiness-state-definitions"


def list_readiness_state_ids(shop_id: str) -> list[int]:
    """Return all existing readiness_state_id values for the shop (may be empty)."""
    headers = get_api_headers()
    resp = requests.get(
        f"{ETSY_API_BASE}/{_READINESS_ENDPOINT.format(shop_id=shop_id)}",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        return []
    ids: list[int] = []
    for item in resp.json().get("results", []):
        rid = item.get("readiness_state_id") or item.get("id")
        if rid:
            ids.append(rid)
    return ids


def create_readiness_state_definition(
    shop_id: str,
    *,
    readiness_state: str,
    min_processing_time: int,
    max_processing_time: int,
    processing_time_unit: str,
) -> int:
    """Create a processing profile (readiness state) and return its id."""
    headers = get_api_headers()
    payload = {
        "readiness_state": readiness_state,
        "min_processing_time": min_processing_time,
        "max_processing_time": max_processing_time,
        "processing_time_unit": processing_time_unit,
    }
    resp = requests.post(
        f"{ETSY_API_BASE}/{_READINESS_ENDPOINT.format(shop_id=shop_id)}",
        headers=headers,
        data=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to create readiness state: {resp.status_code} - {resp.text[:300]}"
        )
    body = resp.json()
    rid = body.get("readiness_state_id") or body.get("id")
    if not rid:
        raise RuntimeError(f"Readiness state created but no id in response: {body}")
    return rid


def ensure_readiness_state_id(
    shop_id: str,
    *,
    readiness_state: str,
    min_processing_time: int,
    max_processing_time: int,
    processing_time_unit: str,
) -> int:
    """Reuse an existing readiness state if present, otherwise create one."""
    existing = list_readiness_state_ids(shop_id)
    if existing:
        return existing[0]
    return create_readiness_state_definition(
        shop_id,
        readiness_state=readiness_state,
        min_processing_time=min_processing_time,
        max_processing_time=max_processing_time,
        processing_time_unit=processing_time_unit,
    )


def create_draft_listing(
    shop_id: str,
    *,
    title: str,
    description: str,
    price: float,
    quantity: int,
    taxonomy_id: int,
    tags: list[str],
    materials: list[str],
    who_made: str,
    when_made: str,
    shipping_profile_id: int,
    readiness_state_id: int,
) -> dict:
    """Create a draft physical listing on Etsy.

    Returns the listing object from the API response. The listing_id is at
    `result["listing_id"]`.
    """
    headers = get_api_headers()

    payload = {
        "quantity": quantity,
        "title": title[:140],
        "description": description,
        "price": price,
        "who_made": who_made,
        "when_made": when_made,
        "taxonomy_id": taxonomy_id,
        "shipping_profile_id": shipping_profile_id,
        "readiness_state_id": readiness_state_id,
        "tags": ",".join(tags[:13]),
        "materials": ",".join(sanitize_materials(materials)),
        "state": "draft",
        "is_supply": False,
        "type": "physical",
    }

    # legacy=false enables the new processing-profile params (readiness_state_id).
    resp = requests.post(
        f"{ETSY_API_BASE}/shops/{shop_id}/listings",
        headers=headers,
        params={"legacy": "false"},
        data=payload,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to create listing: {resp.status_code} - {resp.text[:500]}"
        )
    return resp.json()


def get_listing_tags(listing_id: int) -> list[str]:
    """Return the current tags of a listing."""
    headers = get_api_headers()
    resp = requests.get(
        f"{ETSY_API_BASE}/listings/{listing_id}",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch listing {listing_id}: {resp.status_code} - {resp.text[:300]}"
        )
    return resp.json().get("tags", []) or []


def fetch_listing_full(listing_id: int) -> dict:
    """Read a public Etsy listing for competitive analysis (NOT to copy).

    Pulls title / description / tags / materials / price / image URLs via the
    Etsy API. Read-only — never writes. Returns a normalised dict; raises
    RuntimeError on HTTP error.
    """
    headers = get_api_headers()
    resp = requests.get(
        f"{ETSY_API_BASE}/listings/{listing_id}",
        headers=headers,
        params={"includes": "Images"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch listing {listing_id}: {resp.status_code} - {resp.text[:300]}"
        )
    data = resp.json()
    price = data.get("price") or {}
    amount = price.get("amount")
    divisor = price.get("divisor") or 100
    price_value = (
        round(amount / divisor, 2)
        if isinstance(amount, (int, float)) and divisor
        else None
    )
    images = [
        {
            "listing_image_id": img.get("listing_image_id"),
            "url_fullxfull": img.get("url_fullxfull"),
            "url_570xN": img.get("url_570xN"),
            "rank": img.get("rank"),
        }
        for img in (data.get("images") or [])
    ]
    return {
        "listing_id": data.get("listing_id") or listing_id,
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "tags": data.get("tags") or [],
        "materials": data.get("materials") or [],
        "price": price_value,
        "currency": price.get("currency_code") or data.get("currency_code"),
        "quantity": data.get("quantity"),
        "url": data.get("url"),
        "state": data.get("state"),
        "shop_id": data.get("shop_id"),
        "taxonomy_id": data.get("taxonomy_id"),
        "num_favorers": data.get("num_favorers"),
        "views": data.get("views"),
        "images": images,
    }


def update_listing(shop_id: str, listing_id: int, **fields) -> dict:
    """Patch fields of an existing listing.

    `tags` and `materials` may be passed as lists; they are joined to the
    comma-separated form Etsy expects.
    """
    headers = get_api_headers()
    payload = dict(fields)
    for key in ("tags", "materials"):
        if isinstance(payload.get(key), (list, tuple)):
            payload[key] = ",".join(str(v) for v in payload[key][:13])
    resp = requests.patch(
        f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}",
        headers=headers,
        data=payload,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to update listing {listing_id}: {resp.status_code} - {resp.text[:500]}"
        )
    return resp.json()


def get_taxonomy_properties(taxonomy_id: int) -> list[dict]:
    """Return the property definitions Etsy exposes for a taxonomy node.

    Each property has a `property_id`, a `name` (e.g. "Primary color",
    "Occasion") and a list of `possible_values` (each with `value_id` + `name`).
    Returns an empty list on any error so callers can degrade gracefully.
    """
    headers = get_api_headers()
    try:
        resp = requests.get(
            f"{ETSY_API_BASE}/seller-taxonomy/nodes/{taxonomy_id}/properties",
            headers=headers,
            timeout=30,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    return resp.json().get("results", []) or []


def update_listing_property(
    shop_id: str,
    listing_id: int,
    property_id: int,
    *,
    value_ids: list[int],
    values: list[str],
) -> dict:
    """Set one taxonomy property (color, occasion, holiday…) on a listing."""
    headers = get_api_headers()
    payload = {"value_ids": value_ids, "values": values}
    resp = requests.put(
        f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}/properties/{property_id}",
        headers=headers,
        data=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to set property {property_id}: {resp.status_code} - {resp.text[:300]}"
        )
    return resp.json()


def _match_property_value(prop: dict, desired: str) -> tuple[int | None, str | None]:
    """Find the value_id whose name best matches `desired` (case-insensitive)."""
    want = (desired or "").strip().lower()
    if not want:
        return None, None
    values = prop.get("possible_values") or []
    for v in values:  # exact match first
        name = (v.get("name") or "").strip()
        if name.lower() == want:
            return v.get("value_id"), name
    for v in values:  # then a loose contains match
        name = (v.get("name") or "").strip()
        if name and (want in name.lower() or name.lower() in want):
            return v.get("value_id"), name
    return None, None


# Etsy property names (as returned by the taxonomy endpoint) we know how to fill.
_ATTR_PROPERTY_NAMES = {
    "primary_color": "primary color",
    "secondary_color": "secondary color",
    "occasion": "occasion",
    "holiday": "holiday",
}


def set_listing_attributes(
    shop_id: str,
    listing_id: int,
    taxonomy_id: int,
    *,
    primary_color: str | None = None,
    secondary_color: str | None = None,
    occasion: str | None = None,
    holiday: str | None = None,
    log=lambda _m: None,
) -> dict:
    """Best-effort: set color / occasion / holiday attributes on a listing.

    Never raises — a missing or unsupported attribute is logged and skipped so
    that a freshly created draft is never lost over an optional field. Returns a
    dict mapping each attempted attribute to the value applied (or a reason it
    was skipped).
    """
    desired = {
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "occasion": occasion,
        "holiday": holiday,
    }
    if not any(desired.values()):
        return {}

    props = get_taxonomy_properties(taxonomy_id)
    by_name = {(p.get("name") or "").strip().lower(): p for p in props}
    results: dict[str, str] = {}

    for key, value in desired.items():
        if not value:
            continue
        prop = by_name.get(_ATTR_PROPERTY_NAMES[key])
        if not prop:
            results[key] = "non supporté par cette catégorie"
            log(f"  • {key} : champ non disponible pour cette catégorie Etsy")
            continue
        property_id = prop.get("property_id")
        value_id, value_name = _match_property_value(prop, value)
        try:
            if value_id is not None:
                update_listing_property(
                    shop_id, listing_id, property_id,
                    value_ids=[value_id], values=[value_name],
                )
                results[key] = value_name
            else:
                # Freeform property with no predefined list: send the raw string.
                update_listing_property(
                    shop_id, listing_id, property_id,
                    value_ids=[], values=[value],
                )
                results[key] = value
            log(f"  • {key} = {results[key]}")
        except Exception as e:  # noqa: BLE001 — optional field, keep the draft
            results[key] = f"échec ({e})"
            log(f"  • {key} : échec ({e})")
    return results


def _prepare_image_for_etsy(path: Path) -> bytes:
    """Convert any input image to a clean JPEG suitable for Etsy upload.

    Avoids the "extension says PNG but bytes are JPEG" issue Claude flagged,
    and resizes oversized images down to Etsy's recommended max dimension.
    """
    with Image.open(path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail(
            (ETSY_RECOMMENDED_MAX_PX, ETSY_RECOMMENDED_MAX_PX),
            Image.Resampling.LANCZOS,
        )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        return buf.getvalue()


def upload_listing_image(
    shop_id: str, listing_id: int, image_path: Path, rank: int
) -> dict:
    """Upload one image to a listing at the given rank (1 = main image)."""
    headers = get_api_headers()
    img_bytes = _prepare_image_for_etsy(image_path)
    files = {"image": (f"image_{rank}.jpg", img_bytes, "image/jpeg")}
    data = {"rank": rank}

    resp = requests.post(
        f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}/images",
        headers=headers,
        files=files,
        data=data,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to upload image {image_path.name}: "
            f"{resp.status_code} - {resp.text[:300]}"
        )
    return resp.json()


def upload_listing_video(
    shop_id: str, listing_id: int, video_path: Path, *, name: str | None = None
) -> dict:
    """Upload a single video (MP4) to a listing. Etsy allows one video per listing."""
    size = video_path.stat().st_size
    if size > ETSY_MAX_VIDEO_BYTES:
        raise RuntimeError(
            f"Video {video_path.name} is {size / 1024 / 1024:.1f} MB, over Etsy's "
            f"100 MB limit."
        )
    headers = get_api_headers()
    files = {"video": (video_path.name, video_path.read_bytes(), "video/mp4")}
    data = {"name": name or video_path.name}

    resp = requests.post(
        f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}/videos",
        headers=headers,
        files=files,
        data=data,
        timeout=300,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to upload video {video_path.name}: "
            f"{resp.status_code} - {resp.text[:300]}"
        )
    return resp.json()


def listing_admin_url(listing_id: int) -> str:
    """URL to edit the listing in the Etsy seller dashboard."""
    return f"https://www.etsy.com/your/shops/me/tools/listings/{listing_id}"
