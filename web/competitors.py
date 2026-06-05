"""Downloaded competitor listings — the "Analyse de listings concurrents" store.

When the user clicks "Analyser ce listing", we fetch the real listing through
the Etsy API (read-only), download its images locally, and cross-reference its
tags with eRank stats. Everything is persisted under data/competitors/ so the
"Analyse de listings concurrents téléchargés" tab can browse them offline.

This is for ANALYSIS, not copying. Nothing here writes to Etsy.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import requests

from src.etsy_client import fetch_listing_full

from . import niche

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = ROOT / "data" / "competitors"
MAX_IMAGES = 10
_DL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_LISTING_RE = re.compile(r"/listing/(\d+)")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_listing_id(ref: str | int) -> int:
    """Accept a bare id, an Etsy listing URL, or a '12345' string."""
    s = str(ref).strip()
    if s.isdigit():
        return int(s)
    m = _LISTING_RE.search(s)
    if m:
        return int(m.group(1))
    raise ValueError(f"Identifiant de listing introuvable dans : {ref!r}")


def _dir(listing_id: int) -> Path:
    return STORE_DIR / str(listing_id)


def _record_path(listing_id: int) -> Path:
    return _dir(listing_id) / "listing.json"


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _etsy_listing_url(listing_id: int, fallback: str | None) -> str:
    return fallback or f"https://www.etsy.com/listing/{listing_id}"


# --------------------------------------------------------------------------- #
# eRank enrichment (best-effort — never fails the import)
# --------------------------------------------------------------------------- #
def _erank_tag_stats(tags: list[str]) -> dict[str, dict]:
    """Per-tag stats via eRank: last-month searches + REAL Etsy competition, etc."""
    stats: dict[str, dict] = {}
    for chunk in _chunked([t for t in tags if t], 12):
        try:
            data = niche.get_json_sync(
                "/api/keywords/batch-stats",
                {"terms": ",".join(chunk)},
                timeout=60,
            )
        except Exception:
            continue
        for term, s in (data.get("stats") or {}).items():
            trend = s.get("search_trend") or []
            last_month = s.get("last_month_searches")
            if last_month is None and trend:
                last = trend[-1]
                last_month = last.get("value") if isinstance(last, dict) else None
            stats[term] = {
                "avg_searches": s.get("avg_searches"),
                "last_month_searches": last_month,
                "etsy_competition": s.get("etsy_competition"),
                "ctr": s.get("ctr"),
                "kd": s.get("kd"),
                "avg_clicks": s.get("avg_clicks"),
                "trend_last": last_month,
            }
    return stats


def _erank_spy(listing_id: int, url: str) -> dict:
    """est_sales / est_revenue / age / views from eRank's spy/resolve."""
    try:
        return niche.get_json_sync("/api/spy/resolve", {"url": url}, timeout=60) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def _download_images(listing_id: int, images: list[dict]) -> list[dict]:
    img_dir = _dir(listing_id) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    for i, img in enumerate(images[:MAX_IMAGES]):
        url = img.get("url_fullxfull") or img.get("url_570xN")
        if not url:
            continue
        ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
        dest = img_dir / f"{i}{ext}"
        try:
            r = requests.get(url, headers={"User-Agent": _DL_UA}, timeout=30)
            if r.ok and r.content:
                dest.write_bytes(r.content)
                saved.append({"index": i, "file": dest.name, "src": url})
        except Exception:
            continue
    return saved


def import_listing(ref: str | int, *, fetch_erank: bool = True) -> dict:
    """Fetch a listing via the Etsy API, download images, enrich with eRank.

    Read-only on Etsy. Overwrites any previous import of the same listing.
    Returns the stored record summary.
    """
    listing_id = parse_listing_id(ref)
    etsy = fetch_listing_full(listing_id)  # raises on HTTP error

    # Fresh directory (re-import = refresh)
    d = _dir(listing_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)

    saved_images = _download_images(listing_id, etsy.get("images") or [])

    erank: dict[str, Any] = {}
    if fetch_erank:
        url = _etsy_listing_url(listing_id, etsy.get("url"))
        spy = _erank_spy(listing_id, url)
        erank["spy"] = spy
        erank["tag_stats"] = _erank_tag_stats(etsy.get("tags") or [])
        # Surface the headline competitive metrics. spy/resolve nests the live
        # numbers (sales/revenue/age/views) under "stats", with shop info at the
        # top level — merge both so the library summary shows real figures.
        metric_src: dict[str, Any] = {}
        if isinstance(spy, dict):
            metric_src.update(
                {k: v for k, v in spy.items() if not isinstance(v, (dict, list))}
            )
            if isinstance(spy.get("stats"), dict):
                metric_src.update(spy["stats"])
        for key in ("est_sales", "est_revenue", "age_days", "age_in_days",
                    "conv_rate", "daily_views", "total_views", "views_month",
                    "views", "num_favorers", "favorites", "price",
                    "shop_name", "shop_id"):
            if metric_src.get(key) is not None:
                erank.setdefault("metrics", {})[key] = metric_src[key]

    record = {
        "listing_id": listing_id,
        "title": etsy.get("title"),
        "description": etsy.get("description"),
        "tags": etsy.get("tags") or [],
        "materials": etsy.get("materials") or [],
        "price": etsy.get("price"),
        "currency": etsy.get("currency"),
        "quantity": etsy.get("quantity"),
        "url": _etsy_listing_url(listing_id, etsy.get("url")),
        "shop_id": etsy.get("shop_id"),
        "taxonomy_id": etsy.get("taxonomy_id"),
        "num_favorers": etsy.get("num_favorers"),
        "views": etsy.get("views"),
        "images": saved_images,
        "erank": erank,
        "imported_at": int(time.time()),
    }
    _record_path(listing_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary(record)


# --------------------------------------------------------------------------- #
# Read / list / delete
# --------------------------------------------------------------------------- #
def summary(record: dict) -> dict:
    """Compact form for the library list."""
    metrics = (record.get("erank") or {}).get("metrics") or {}
    return {
        "listing_id": record.get("listing_id"),
        "title": record.get("title"),
        "price": record.get("price"),
        "currency": record.get("currency"),
        "shop_name": metrics.get("shop_name"),
        "image_count": len(record.get("images") or []),
        "tag_count": len(record.get("tags") or []),
        "est_sales": metrics.get("est_sales"),
        "est_revenue": metrics.get("est_revenue"),
        "age_days": metrics.get("age_days") or metrics.get("age_in_days"),
        "url": record.get("url"),
        "imported_at": record.get("imported_at"),
    }


def get_one(listing_id: int) -> dict | None:
    p = _record_path(listing_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_all() -> list[dict]:
    if not STORE_DIR.is_dir():
        return []
    out: list[dict] = []
    for sub in STORE_DIR.iterdir():
        if not sub.is_dir():
            continue
        rec = get_one_from_dir(sub)
        if rec:
            out.append(summary(rec))
    out.sort(key=lambda r: r.get("imported_at") or 0, reverse=True)
    return out


def get_one_from_dir(sub: Path) -> dict | None:
    p = sub / "listing.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def image_path(listing_id: int, idx: int) -> Path | None:
    rec = get_one(listing_id)
    if not rec:
        return None
    imgs = rec.get("images") or []
    if idx < 0 or idx >= len(imgs):
        return None
    p = _dir(listing_id) / "images" / imgs[idx]["file"]
    return p if p.is_file() else None


def image_paths(listing_id: int) -> list[Path]:
    """Local paths of a downloaded listing's images, in saved order.

    Used to re-upload the photos when importing a competitor listing into the
    user's own Etsy DRAFT (private analysis — never published).
    """
    rec = get_one(listing_id)
    if not rec:
        return []
    base = _dir(listing_id) / "images"
    out: list[Path] = []
    for img in rec.get("images") or []:
        fname = img.get("file")
        if not fname:
            continue
        p = base / fname
        if p.is_file():
            out.append(p)
    return out


def delete_one(listing_id: int) -> bool:
    """Remove a downloaded listing's LOCAL cache (re-importable). Not an Etsy op."""
    d = _dir(listing_id)
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


# --------------------------------------------------------------------------- #
# Tag analysis for a stored listing (eRank: rate the existing tags + suggest)
# --------------------------------------------------------------------------- #
def analyze_tags(listing_id: int) -> dict:
    """For a stored competitor listing, rate its tags + suggest better ones.

    Uses the niche-detector: batch-stats for the existing tags + suggest-tags
    seeded from them. Raises niche.NicheError if the service is unreachable.
    """
    rec = get_one(listing_id)
    if not rec:
        raise FileNotFoundError(listing_id)
    tags = rec.get("tags") or []

    existing = []
    stats = _erank_tag_stats(tags)
    for t in tags:
        s = stats.get(t, {})
        existing.append({"tag": t, **s})

    suggestions: list[dict] = []
    if tags:
        data = niche.get_json_sync(
            "/api/optimize/suggest-tags",
            {"seeds": ",".join(tags[:8]), "existing": ",".join(tags)},
            timeout=120,
        )
        suggestions = data.get("suggestions") or data.get("tags") or []

    return {
        "listing_id": listing_id,
        "title": rec.get("title"),
        "existing": existing,
        "suggestions": suggestions,
    }
