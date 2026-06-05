"""FastAPI web app wrapping the Etsy auto-lister pipeline.

Run it with:
    ./.venv/bin/python -m web.app
or:
    ./.venv/bin/uvicorn web.app:app --reload --port 8000

Then open http://127.0.0.1:8000 in your browser.

Workflow exposed by the UI:
    pick a product folder -> set price / base_tag / language
    -> preview (vision + eRank + Claude copy, NO Etsy write)
    -> edit title / description / 13 tags / materials
    -> publish a draft listing on Etsy + upload its images
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path

import anthropic
import requests
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

# The project's .env is the source of truth. Load it with override=True BEFORE
# importing the pipeline modules, so a (possibly empty) ANTHROPIC_API_KEY that
# the surrounding shell exports can't shadow the real key from .env.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from src.config import DEFAULTS, load_batch_config
from src.etsy_client import (
    MAX_IMAGES_PER_LISTING,
    create_draft_listing,
    ensure_readiness_state_id,
    get_first_shipping_profile_id,
    get_shop_id,
    listing_admin_url,
    set_listing_attributes,
    upload_listing_image,
)
from src.flow import find_video, images_dir
from src.generator import generate_listing
from src.prompt_ideas import generate_image_prompts
from src.tags import get_optimized_tags
from src.taxonomy import find_taxonomy_id
from src.vision import IMAGE_MEDIA_TYPES, analyze_product_folder
from src import shops
from src.shops import use_shop

from . import competitors, easypic, jobs, niche, niche_tracker

ROOT = Path(__file__).resolve().parent.parent          # etsy-auto-lister/
FLOW_DIR = ROOT.parent / "flow-automation"             # Google Flow automation
SOURCE = FLOW_DIR / "output"                           # generated product folders
INPUT_DIR = FLOW_DIR / "input"                         # drop zone -> Flow inputs
PROMPTS_FILE = FLOW_DIR / "prompts_info.txt"           # one image prompt per line
CONFIG_PATH = ROOT / "batch.txt"
STATIC = Path(__file__).resolve().parent / "static"

ERANK_URL = os.environ.get("ERANK_API_URL", "http://127.0.0.1:8765").rstrip("/")
ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Etsy "Occasion" attribute applied to every draft. The user sells gift
# plushies, so this defaults to "Birthday" (= anniversaire). Editable per
# listing in the preview UI.
DEFAULT_OCCASION = "Birthday"

app = FastAPI(title="Etsy Auto-Lister")


@app.exception_handler(shops.ShopError)
async def _shop_error_handler(_request: Request, exc: shops.ShopError) -> JSONResponse:
    """Turn a shop-resolution error into a clean JSON response.

    UnknownShopError → 400 (the frontend sent a bad/stale shop key);
    NoShopsConfigured (and any other ShopError) → 503 (nothing is set up yet).
    """
    status = 400 if isinstance(exc, shops.UnknownShopError) else 503
    return JSONResponse(status_code=status, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _images(folder: Path) -> list[Path]:
    pics = images_dir(folder)
    if not pics.is_dir():
        return []
    return sorted(
        f for f in pics.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_MEDIA_TYPES
    )


def _safe_folder(name: str) -> Path:
    """Resolve `name` to a direct child folder of SOURCE (blocks traversal)."""
    src = SOURCE.resolve()
    folder = (src / name).resolve()
    if folder.parent != src or not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"Dossier introuvable : {name}")
    return folder


# Per-product state lives in a sidecar JSON inside the folder, so it travels
# with the folder on rename and disappears on delete. Deletions are *soft*: the
# folder is moved to output/.trash/ rather than hard-removed (recoverable).
STATUS_FILE = ".listing_status.json"
TRASH_DIRNAME = ".trash"


def _status_path(folder: Path) -> Path:
    return folder / STATUS_FILE


def _read_status(folder: Path) -> dict:
    """Read a product's {posted, posted_at} state; defaults to not-posted."""
    p = _status_path(folder)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {
                "posted": bool(data.get("posted")),
                "posted_at": data.get("posted_at"),
            }
        except Exception:
            pass
    return {"posted": False, "posted_at": None}


def _write_status(folder: Path, *, posted: bool) -> dict:
    """Persist a product's posted state. Returns the new state."""
    state = {
        "posted": bool(posted),
        "posted_at": int(time.time()) if posted else None,
    }
    _status_path(folder).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return state


def _safe_new_folder_name(name: str) -> str:
    """Validate a user-supplied product name into a safe direct child name.

    Allows spaces, dots and dashes (existing names look like
    'Screenshot 2026-05-13 at 15.29.36'), but blocks path separators,
    traversal and hidden/dot-leading names.
    """
    raw = unicodedata.normalize("NFKC", str(name)).strip()
    if not raw or raw in (".", ".."):
        raise HTTPException(status_code=400, detail="Nom de produit invalide.")
    if Path(raw).name != raw or "/" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="Le nom ne peut pas contenir « / » ou « \\ ».")
    if raw.startswith("."):
        raise HTTPException(status_code=400, detail="Le nom ne peut pas commencer par « . ».")
    if len(raw) > 120:
        raise HTTPException(status_code=400, detail="Nom trop long (120 caractères max).")
    return raw


def _safe_input_name(name: str) -> str:
    """Sanitize an uploaded filename into a safe `<stem>.<ext>` inside INPUT_DIR."""
    base = Path(name).name
    ext = Path(base).suffix.lower()
    if ext not in ALLOWED_IMG_EXT:
        raise HTTPException(status_code=400, detail=f"Format non supporté : {name}")
    stem = unicodedata.normalize("NFKC", Path(base).stem)
    stem = re.sub(r"[^\w\- ]+", "_", stem).strip()
    return f"{stem or 'image'}{ext}"


def _friendly_llm_error(e: Exception) -> tuple[int, str]:
    """(status, message) for an Anthropic API failure, in plain French.

    The vision analysis + listing copy run through Claude; when the key is out
    of credits / rate-limited / refused we surface a readable message instead of
    a bare 500 "Internal Server Error".
    """
    msg = str(getattr(e, "message", "") or e)
    low = msg.lower()
    status = getattr(e, "status_code", None)
    if "credit balance is too low" in low or "plans & billing" in low:
        return 402, (
            "Crédit API Anthropic épuisé : la rédaction de l'annonce utilise "
            "Claude. Recharge ton compte sur console.anthropic.com → Plans & "
            "Billing, puis réessaie."
        )
    if status == 429 or "rate limit" in low:
        return 429, (
            "Limite de l'API Anthropic atteinte. Patiente quelques secondes "
            "puis réessaie."
        )
    if status in (401, 403) or "authentication" in low or "x-api-key" in low:
        return 502, "Clé API Anthropic refusée. Vérifie ANTHROPIC_API_KEY dans le fichier .env."
    return 502, f"Erreur de l'API Anthropic : {msg}"


def _make_preview(folder: Path, base_tag: str, language: str) -> dict:
    """Run vision + eRank tags + Claude copy + taxonomy (no Etsy write)."""
    analysis = analyze_product_folder(folder)
    partial = get_optimized_tags(base_tag)
    listing = generate_listing(
        analysis, partial, language=language, base_tag=base_tag
    )
    taxonomy_id = find_taxonomy_id(
        analysis["top_level_category"], analysis.get("subcategory_hint")
    )
    return {
        "title": listing["title"],
        "description": listing["description"],
        "tags": listing["tags"],
        "materials": listing["materials"],
        "primary_color": listing.get("primary_color"),
        "secondary_color": listing.get("secondary_color"),
        "occasion": DEFAULT_OCCASION,
        "taxonomy_id": taxonomy_id,
        "top_level_category": analysis["top_level_category"],
        "product_type": analysis["product_type"],
        "erank_tag_count": len(partial),
        "image_count": len(_images(folder)),
    }


def _publish_listing(
    folder: Path,
    *,
    title: str,
    description: str,
    tags: list[str],
    materials: list[str],
    taxonomy_id: int,
    price: float,
    quantity: int,
    who_made: str,
    when_made: str,
    primary_color: str | None = None,
    secondary_color: str | None = None,
    occasion: str | None = None,
    log=lambda _m: None,
) -> dict:
    """Create the Etsy draft and upload its images. Returns ids + admin url."""
    shop_id = get_shop_id()
    shipping_profile_id = get_first_shipping_profile_id(shop_id)
    if shipping_profile_id is None:
        raise HTTPException(
            status_code=400,
            detail="Aucun profil d'expedition Etsy. Cree-en un dans Shop Manager.",
        )

    cfg = load_batch_config(CONFIG_PATH)
    readiness_state = (
        "made_to_order" if when_made == "made_to_order" else "ready_to_ship"
    )
    readiness_state_id = ensure_readiness_state_id(
        shop_id,
        readiness_state=readiness_state,
        min_processing_time=cfg.min_processing_time,
        max_processing_time=cfg.max_processing_time,
        processing_time_unit=cfg.processing_time_unit,
    )

    listing = create_draft_listing(
        shop_id,
        title=title,
        description=description,
        price=price,
        quantity=quantity,
        taxonomy_id=taxonomy_id,
        tags=tags,
        materials=materials,
        who_made=who_made,
        when_made=when_made,
        shipping_profile_id=shipping_profile_id,
        readiness_state_id=readiness_state_id,
    )
    listing_id = listing["listing_id"]

    # Best-effort taxonomy attributes (colors / occasion). Never fails the draft.
    attributes: dict = {}
    if primary_color or secondary_color or occasion:
        log("Attributs Etsy (couleurs / occasion)…")
        try:
            attributes = set_listing_attributes(
                shop_id,
                listing_id,
                taxonomy_id,
                primary_color=primary_color,
                secondary_color=secondary_color,
                occasion=occasion,
                log=log,
            )
        except Exception as e:  # noqa: BLE001 — optional, keep the draft
            log(f"  • attributs ignorés ({e})")

    images = _images(folder)[:MAX_IMAGES_PER_LISTING]
    for rank, img_path in enumerate(images, start=1):
        upload_listing_image(shop_id, listing_id, img_path, rank=rank)

    return {
        "listing_id": listing_id,
        "admin_url": listing_admin_url(listing_id),
        "images_uploaded": len(images),
        "attributes": attributes,
    }


def _build_listing(
    folder_name: str,
    log,
    auto_publish: bool,
    shop: str | None = None,
    *,
    base_tag_override: str | None = None,
) -> dict:
    """Job callback: generated folder -> preview (-> optional Etsy draft).

    `shop` is the registry slot key chosen in the UI ("1", "2", …); it selects
    which Etsy shop receives the draft when `auto_publish` is set. None falls
    back to the default shop. This runs inside the job thread, which does NOT
    inherit the request's contextvar, so the shop is re-applied here explicitly.

    `base_tag_override` (Easy Picture) replaces the eRank seed tag from
    batch.txt for THIS listing only, so the user can preview a fiche under a
    different tag before importing it. None falls back to `cfg.base_tag`.
    """
    folder = _safe_folder(folder_name)
    cfg = load_batch_config(CONFIG_PATH)
    base_tag = (base_tag_override or "").strip() or cfg.base_tag
    log(f"Analyse des images + tags eRank « {base_tag} » + rédaction…")
    try:
        preview = _make_preview(folder, base_tag, cfg.language)
    except anthropic.APIError as e:
        _, detail = _friendly_llm_error(e)
        raise RuntimeError(detail) from e
    log(f"Titre : {preview['title'][:64]} · {len(preview['tags'])} tags")

    result = {"folder": folder_name, "published": False, "preview": preview}
    if not auto_publish:
        return result

    with use_shop(shop) as sh:
        log(f"Boutique cible : {sh.label}")
        pub = _publish_listing(
            folder,
            title=preview["title"],
            description=preview["description"],
            tags=preview["tags"],
            materials=preview["materials"],
            taxonomy_id=preview["taxonomy_id"],
            price=cfg.price,
            quantity=cfg.quantity,
            who_made=cfg.who_made,
            when_made=cfg.when_made,
            primary_color=preview.get("primary_color"),
            secondary_color=preview.get("secondary_color"),
            occasion=preview.get("occasion"),
            log=log,
        )
    result.update(published=True, **pub)
    # `sh` survives the `with` block (Python scoping). Tag the result with the
    # target shop so the UI can show which shop got the draft + warn that the
    # "Ouvrir sur Etsy" link only resolves while that account is logged in.
    result.update(shop_key=sh.key, shop_label=sh.label, shop_id=str(sh.shop_id))
    return result


def _bound_build_listing(base_tag: str | None):
    """Return the `build_listing` job callback, optionally pinning a base tag.

    jobs.py calls the callback positionally as `(name, log, auto_publish, shop)`,
    so an Easy Picture base-tag override is bound here via functools.partial
    (keyword-only) without changing that contract.
    """
    tag = (base_tag or "").strip()
    if not tag:
        return _build_listing
    return functools.partial(_build_listing, base_tag_override=tag)


def _write_batch_config(updates: dict[str, str]) -> None:
    """Update keys in batch.txt in place, preserving comments and order."""
    lines = CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            key = stripped.split(":", 1)[0].strip().lower()
            if key in updates:
                out.append(f"{key}: {updates[key]}")
                seen.add(key)
                continue
        out.append(raw)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}: {val}")
    CONFIG_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class PreviewReq(BaseModel):
    folder: str
    base_tag: str
    language: str = "en"


class PublishReq(BaseModel):
    folder: str
    title: str
    description: str
    tags: list[str]
    materials: list[str]
    taxonomy_id: int
    price: float
    quantity: int = 1
    who_made: str = "i_did"
    when_made: str = "made_to_order"
    primary_color: str | None = None
    secondary_color: str | None = None
    occasion: str | None = None
    # Registry slot key of the target shop ("1", "2", …). None = default shop.
    shop: str | None = None


class PostedReq(BaseModel):
    """Mark a product as 'annonce postée sur Etsy' (turns it green) or not."""
    posted: bool = True


class RenameReq(BaseModel):
    """Rename a product folder (the displayed product name)."""
    new_name: str


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/folders")
def list_folders() -> list[dict]:
    """All product folders that have at least one image."""
    if not SOURCE.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(SOURCE.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue  # skip hidden dirs like .trash
        imgs = _images(p)
        if not imgs:
            continue
        status = _read_status(p)
        out.append(
            {
                "name": p.name,
                "image_count": len(imgs),
                "has_video": find_video(p) is not None,
                "posted": status["posted"],
                "posted_at": status["posted_at"],
            }
        )
    return out


@app.get("/api/folders/{name}/image/{idx}")
def folder_image(name: str, idx: int, w: int | None = None):
    """Serve the idx-th image of a folder, optionally resized to width `w`."""
    folder = _safe_folder(name)
    imgs = _images(folder)
    if idx < 0 or idx >= len(imgs):
        raise HTTPException(status_code=404, detail="Image introuvable")
    path = imgs[idx]
    if w:
        w = max(32, min(w, 2000))
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((w, w), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=82)
        return Response(content=buf.getvalue(), media_type="image/jpeg")
    return FileResponse(path)


@app.post("/api/folders/{name}/posted")
def folder_set_posted(name: str, req: PostedReq) -> dict:
    """Mark a product as 'annonce postée sur Etsy' (green) — or un-mark it.

    Purely local bookkeeping: it writes a status sidecar so the product turns
    green in the UI, signalling it's safe to delete. Nothing is sent to Etsy.
    """
    folder = _safe_folder(name)
    state = _write_status(folder, posted=req.posted)
    return {"name": folder.name, **state}


@app.post("/api/folders/{name}/rename")
def folder_rename(name: str, req: RenameReq) -> dict:
    """Rename a product folder (its displayed name). Local file move only."""
    folder = _safe_folder(name)
    new_name = _safe_new_folder_name(req.new_name)
    if new_name == folder.name:
        return {"name": folder.name, "renamed": False}
    target = SOURCE.resolve() / new_name
    if target.exists():
        raise HTTPException(
            status_code=409, detail=f"Un produit nommé « {new_name} » existe déjà."
        )
    folder.rename(target)
    return {"name": new_name, "previous": name, "renamed": True}


@app.delete("/api/folders/{name}")
def folder_delete(name: str) -> dict:
    """Soft-delete a product: move its folder to output/.trash/ (recoverable).

    Local only — does NOT touch Etsy. The user deletes a product once its
    listing is posted; the folder is moved aside rather than hard-erased so an
    accidental click can be undone from the .trash folder.
    """
    folder = _safe_folder(name)
    trash = SOURCE.resolve() / TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{folder.name}__{int(time.time())}"
    shutil.move(str(folder), str(dest))
    return {"deleted": name, "trashed_to": dest.name}


@app.get("/api/config")
def get_config() -> dict:
    """Default config from batch.txt (falls back to library defaults)."""
    try:
        cfg = load_batch_config(CONFIG_PATH)
        return {
            "price": cfg.price,
            "base_tag": cfg.base_tag,
            "currency": cfg.currency,
            "language": cfg.language,
            "quantity": cfg.quantity,
            "who_made": cfg.who_made,
            "when_made": cfg.when_made,
        }
    except Exception:
        return {
            "price": 35,
            "base_tag": "",
            "currency": "EUR",
            "language": DEFAULTS["language"],
            "quantity": int(DEFAULTS["quantity"]),
            "who_made": DEFAULTS["who_made"],
            "when_made": DEFAULTS["when_made"],
        }


@app.post("/api/preview")
def preview(req: PreviewReq) -> dict:
    """Generate the listing copy without writing anything to Etsy."""
    folder = _safe_folder(req.folder)
    if not req.base_tag.strip():
        raise HTTPException(status_code=400, detail="base_tag requis")
    try:
        return _make_preview(folder, req.base_tag, req.language)
    except anthropic.APIError as e:
        status, detail = _friendly_llm_error(e)
        raise HTTPException(status_code=status, detail=detail) from e


@app.post("/api/publish")
def publish(req: PublishReq) -> dict:
    """Create a draft Etsy listing with the (possibly edited) copy + images.

    `req.shop` selects which configured shop the draft lands in; an unknown or
    missing shop is handled by the global ShopError handler (400 / 503).
    """
    folder = _safe_folder(req.folder)
    with use_shop(req.shop):
        return _publish_listing(
            folder,
            title=req.title,
            description=req.description,
            tags=req.tags,
            materials=req.materials,
            taxonomy_id=req.taxonomy_id,
            price=req.price,
            quantity=req.quantity,
            who_made=req.who_made,
            when_made=req.when_made,
            primary_color=req.primary_color,
            secondary_color=req.secondary_color,
            occasion=req.occasion,
        )


@app.get("/api/shops")
def list_configured_shops() -> dict:
    """The Etsy shops configured in .env, for the UI's shop picker.

    Only public fields are exposed (slot key, label, numeric shop_id) — never a
    refresh token. `default` is the slot key the picker should pre-select.
    """
    items = shops.list_shops()
    return {
        "shops": [
            {"key": s.key, "label": s.label, "shop_id": s.shop_id} for s in items
        ],
        "default": items[0].key if items else None,
    }


# --------------------------------------------------------------------------- #
# Réglages : prompts d'images + prix / tag de base
# --------------------------------------------------------------------------- #
class PromptsReq(BaseModel):
    text: str


class ConfigReq(BaseModel):
    price: float | None = None
    base_tag: str | None = None
    language: str | None = None
    currency: str | None = None
    quantity: int | None = None


def _prompt_lines() -> list[str]:
    """Non-empty image prompts, in order (one per line of prompts_info.txt)."""
    if not PROMPTS_FILE.is_file():
        return []
    text = PROMPTS_FILE.read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


@app.get("/api/prompts")
def get_prompts() -> dict:
    """The image prompts (flow-automation/prompts_info.txt), one per line."""
    lines = _prompt_lines()
    return {"text": "\n".join(lines), "prompts": lines, "count": len(lines)}


@app.put("/api/prompts")
def put_prompts(req: PromptsReq) -> dict:
    """Overwrite the image prompts file."""
    lines = [ln.strip() for ln in req.text.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="Au moins un prompt est requis.")
    PROMPTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"prompts": lines, "count": len(lines)}


class PromptGenReq(BaseModel):
    product: str
    count: int = 5


@app.post("/api/prompts/generate")
def suggest_prompts(req: PromptGenReq) -> dict:
    """Generate N Flow staging prompts from a product description (Claude Haiku).

    Does NOT save anything: returns suggestions the UI can preview, then the user
    chooses to replace or append them to the prompts list.
    """
    product = (req.product or "").strip()
    if not product:
        raise HTTPException(status_code=400, detail="Décris ton produit (ex : t-shirt bleu).")
    n = max(1, min(int(req.count or 5), 10))
    try:
        prompts = generate_image_prompts(product, n)
    except anthropic.APIError as e:
        status, detail = _friendly_llm_error(e)
        raise HTTPException(status_code=status, detail=detail) from e
    if not prompts:
        raise HTTPException(status_code=502, detail="Aucun prompt généré, réessaie.")
    return {"prompts": prompts, "product": product, "count": len(prompts)}


@app.put("/api/config")
def put_config(req: ConfigReq) -> dict:
    """Update price / base_tag (and a few optional fields) in batch.txt."""
    updates: dict[str, str] = {}
    if req.price is not None:
        if not req.price > 0:
            raise HTTPException(status_code=400, detail="Prix invalide.")
        updates["price"] = f"{req.price:g}"
    if req.base_tag is not None:
        if not req.base_tag.strip():
            raise HTTPException(status_code=400, detail="Le tag de base est vide.")
        updates["base_tag"] = req.base_tag.strip()
    if req.language is not None:
        updates["language"] = req.language.strip().lower()
    if req.currency is not None:
        updates["currency"] = req.currency.strip().upper()
    if req.quantity is not None:
        updates["quantity"] = str(int(req.quantity))
    if not updates:
        raise HTTPException(status_code=400, detail="Rien à mettre à jour.")
    _write_batch_config(updates)
    return get_config()


# --------------------------------------------------------------------------- #
# Inputs (drop zone) + génération automatique
# --------------------------------------------------------------------------- #
@app.get("/api/inputs")
def list_inputs() -> list[dict]:
    """Images currently sitting in the Flow input/ drop folder."""
    if not INPUT_DIR.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(INPUT_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in ALLOWED_IMG_EXT:
            stem = f.stem
            done = (SOURCE / stem / "picture").is_dir() or (SOURCE / stem).is_dir()
            out.append({"name": f.name, "generated": done})
    return out


@app.post("/api/inputs")
async def upload_inputs(files: list[UploadFile] = File(...)) -> dict:
    """Save dropped images into the Flow input/ folder."""
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for up in files:
        safe = _safe_input_name(up.filename or "image.png")
        dest = INPUT_DIR / safe
        data = await up.read()
        dest.write_bytes(data)
        saved.append(safe)
    return {"saved": saved}


@app.delete("/api/inputs/{name}")
def delete_input(name: str) -> dict:
    """Remove one uploaded image from the Flow input/ drop folder (the queue).

    Works whether or not the image has already been generated; only the input
    file is removed — generated product folders are managed under "Produits".
    """
    safe = Path(name).name
    target = INPUT_DIR / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"Fichier introuvable : {safe}")
    target.unlink()
    return {"deleted": safe}


class GenerateReq(BaseModel):
    filenames: list[str]
    auto_publish: bool = True
    # 1-based indices of the prompts to send to Flow (match prompt1.png…).
    # None or the full set => send every prompt (unchanged behaviour).
    prompts: list[int] | None = None
    # « Aperçu seulement » : ne PAS relancer Google Flow, construire le listing
    # directement à partir des images déjà présentes dans output/<nom>/picture/.
    skip_images: bool = False
    # Registry slot key of the shop to auto-publish into ("1", "2", …). Only
    # used when auto_publish is True. None = default shop.
    shop: str | None = None


@app.post("/api/generate")
def generate(req: GenerateReq) -> dict:
    """Start the background pipeline: Flow images -> listing (-> draft)."""
    if jobs.is_busy():
        raise HTTPException(status_code=409, detail="Une génération est déjà en cours.")
    names: list[str] = []
    for raw in req.filenames:
        name = Path(raw).name
        if not (INPUT_DIR / name).is_file():
            raise HTTPException(status_code=404, detail=f"Input introuvable : {name}")
        names.append(name)
    if not names:
        raise HTTPException(status_code=400, detail="Aucun fichier sélectionné.")

    # Validate the target shop NOW (in the request context) when we'll auto-
    # publish, so a bad/stale key fails fast with a clean error instead of
    # blowing up deep inside the background thread. The ShopError handler maps
    # it to 400/503. The job thread re-resolves the same key via use_shop().
    if req.auto_publish:
        shops.get_shop(req.shop) if req.shop else shops.default_shop()

    # Optional subset of prompts (1-based, matching prompt1.png…). An empty
    # selection is rejected; selecting every prompt is treated as "all" (None)
    # so the unchanged code path runs. Ignored entirely in « aperçu seulement »
    # (no Flow generation happens there).
    prompt_indices: list[int] | None = None
    if not req.skip_images and req.prompts is not None:
        count = len(_prompt_lines())
        idx = sorted({int(i) for i in req.prompts})
        if not idx:
            raise HTTPException(status_code=400, detail="Aucun prompt sélectionné.")
        if idx[0] < 1 or idx[-1] > count:
            raise HTTPException(status_code=400, detail="Sélection de prompts invalide.")
        prompt_indices = None if len(idx) == count else idx

    job_id = jobs.create_job()
    jobs.start_pipeline(
        job_id,
        flow_dir=FLOW_DIR,
        input_files=names,
        build_listing=_build_listing,
        auto_publish=req.auto_publish,
        prompt_indices=prompt_indices,
        skip_images=req.skip_images,
        shop=req.shop,
    )
    return {"job_id": job_id}


@app.get("/api/jobs/current")
def job_current() -> dict:
    """The job currently running/pending (or null), so the UI can re-attach."""
    return {"job": jobs.current_job()}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str) -> dict:
    """Request a stop: flags the job and kills its running image generation."""
    if not jobs.request_cancel(job_id):
        raise HTTPException(status_code=409, detail="Aucune génération active à annuler.")
    return {"cancelled": True}


# --------------------------------------------------------------------------- #
# eRank (outil de tags concurrents, tourne en local sur :8765)
# --------------------------------------------------------------------------- #
@app.get("/api/erank/health")
def erank_health() -> dict:
    """Is the local eRank tag API reachable?"""
    try:
        r = requests.get(f"{ERANK_URL}/health", timeout=4)
        data = r.json() if r.ok else {}
        return {"up": bool(r.ok), "url": ERANK_URL, **data}
    except Exception:
        return {"up": False, "url": ERANK_URL}


@app.get("/api/flow/health")
def flow_health() -> dict:
    """Is the Chrome debug port (Google Flow) reachable?"""
    up = jobs.flow_is_up()
    return {
        "up": up,
        "browser": jobs.flow_browser() if up else "",
        "labs_tab": jobs.flow_has_labs_tab() if up else False,
    }


@app.post("/api/flow/start")
def flow_start() -> dict:
    """Launch Chrome + Google Flow if it isn't already running (blocks up to ~60s)."""
    if jobs.flow_is_up() and jobs.flow_has_labs_tab():
        return {"up": True, "already": True}
    try:
        jobs.launch_flow(FLOW_DIR, timeout=60)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
    return {"up": True, "already": False}


@app.get("/api/erank/tags")
def erank_tags(q: str, n: int = 13, country: str = "USA") -> dict:
    """Preview the best Etsy tags eRank returns for a keyword."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="Mot-clé requis.")
    try:
        tags = get_optimized_tags(q.strip(), country=country, n=n)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"eRank indisponible : {e}")
    return {"keyword": q.strip(), "tags": tags, "count": len(tags)}


# --------------------------------------------------------------------------- #
# Niche-detector : intelligence concurrentielle eRank (service local sur :8770)
#   - tag searcher (recherches / competition / CTR)
#   - espion : boutiques & listings triés par revenu / ventes / ancienneté
#   - suggestions de tags
# Tout est en LECTURE SEULE : aucun écrit Etsy, aucune génération Flow.
# --------------------------------------------------------------------------- #
def _niche_http(err: niche.NicheError) -> HTTPException:
    status = err.status if err.status in (400, 401, 403, 404, 429, 503) else 502
    return HTTPException(status_code=status, detail=err.detail)


class NicheScanReq(BaseModel):
    max_seconds: int = niche_tracker.DEFAULT_MAX_SECONDS
    min_searches: int = niche_tracker.MIN_SEARCHES
    ratio_pct: int = niche_tracker.RATIO_PCT
    # Mondes produits à explorer (plush, figurine, gaming, home). None/vide = tous.
    verticals: list[str] | None = None


class CompetitorImportReq(BaseModel):
    ref: str  # listing id or Etsy listing URL


@app.get("/api/niche/health")
def niche_health() -> dict:
    """Is the niche-detector (eRank intel) service up, and is the cookie valid?"""
    return niche.health()


@app.post("/api/niche/start")
def niche_start() -> dict:
    """Launch the niche-detector service on :8770 if it isn't already running."""
    try:
        return niche.launch(timeout=45)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/niche/keyword-stats")
async def niche_keyword_stats(terms: str) -> dict:
    """Per-keyword stats (avg searches, competition, CTR, KD, trend)."""
    if not terms.strip():
        raise HTTPException(status_code=400, detail="terms requis")
    try:
        return await niche.get_json(
            "/api/keywords/batch-stats", {"terms": terms.strip()}, timeout=120
        )
    except niche.NicheError as e:
        raise _niche_http(e)


@app.get("/api/niche/top-listings")
async def niche_top_listings(
    term: str,
    n: int = 24,
    sort: str = "revenue",
    min_sales: int = 1,
    include_no_sales: bool = False,
) -> dict:
    """Top listings for a keyword — shops sorted by revenue/sales/age + Etsy links."""
    if not term.strip():
        raise HTTPException(status_code=400, detail="term requis")
    path = f"/api/niche/{requests.utils.quote(term.strip(), safe='')}/top-listings"
    try:
        return await niche.get_json(
            path,
            {
                "n": n,
                "sort": sort,
                "min_sales": min_sales,
                "include_no_sales": str(include_no_sales).lower(),
            },
            timeout=90,
        )
    except niche.NicheError as e:
        raise _niche_http(e)


@app.get("/api/niche/spy")
async def niche_spy(url: str) -> dict:
    """Resolve an Etsy listing OR shop URL → eRank stats (revenue/sales/age/tags)."""
    if not url.strip():
        raise HTTPException(status_code=400, detail="url requise")
    try:
        return await niche.get_json("/api/spy/resolve", {"url": url.strip()}, timeout=90)
    except niche.NicheError as e:
        raise _niche_http(e)


@app.get("/api/niche/suggest-tags")
async def niche_suggest_tags(seeds: str, existing: str = "") -> dict:
    """Suggest better tags from seed tags (eRank related-search expansion)."""
    if not seeds.strip():
        raise HTTPException(status_code=400, detail="seeds requis")
    try:
        return await niche.get_json(
            "/api/optimize/suggest-tags",
            {"seeds": seeds.strip(), "existing": existing.strip()},
            timeout=120,
        )
    except niche.NicheError as e:
        raise _niche_http(e)


# --------------------------------------------------------------------------- #
# Analyse de listings concurrents téléchargés
# --------------------------------------------------------------------------- #
def _import_competitor_as_draft(listing_id: int, log=lambda _m: None) -> dict:
    """Recrée un listing concurrent téléchargé en BROUILLON Etsy privé.

    Copie titre / description / tags / matériaux / images du listing analysé
    dans un brouillon de la boutique de l'utilisateur (state=draft, jamais
    publié) afin d'étudier la concurrence depuis l'éditeur Etsy. C'est une
    action déclenchée explicitement par l'utilisateur. À usage d'analyse
    uniquement : ne publie pas le travail d'autrui tel quel.
    """
    rec = competitors.get_one(listing_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Listing non téléchargé.")

    title = (rec.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Listing sans titre — réimporte-le.")
    taxonomy_id = rec.get("taxonomy_id")
    if not taxonomy_id:
        raise HTTPException(
            status_code=400,
            detail="Catégorie Etsy (taxonomy_id) absente du listing téléchargé — réimporte-le.",
        )

    description = rec.get("description") or ""
    tags = [t.strip() for t in (rec.get("tags") or []) if t and t.strip()][:13]
    materials = [m.strip() for m in (rec.get("materials") or []) if m and m.strip()][:13]

    cfg = load_batch_config(CONFIG_PATH)
    try:
        price = float(rec.get("price"))
    except (TypeError, ValueError):
        price = 0.0
    if price <= 0:
        price = float(cfg.price)  # le concurrent a presque toujours un prix ; sinon, défaut boutique

    shop_id = get_shop_id()
    shipping_profile_id = get_first_shipping_profile_id(shop_id)
    if shipping_profile_id is None:
        raise HTTPException(
            status_code=400,
            detail="Aucun profil d'expédition Etsy. Crée-en un dans Shop Manager puis réessaie.",
        )
    readiness_state = "made_to_order" if cfg.when_made == "made_to_order" else "ready_to_ship"
    readiness_state_id = ensure_readiness_state_id(
        shop_id,
        readiness_state=readiness_state,
        min_processing_time=cfg.min_processing_time,
        max_processing_time=cfg.max_processing_time,
        processing_time_unit=cfg.processing_time_unit,
    )

    log(f"Création du brouillon « {title[:60]} »…")
    listing = create_draft_listing(
        shop_id,
        title=title,
        description=description,
        price=price,
        quantity=int(cfg.quantity),
        taxonomy_id=int(taxonomy_id),
        tags=tags,
        materials=materials,
        who_made=cfg.who_made,
        when_made=cfg.when_made,
        shipping_profile_id=shipping_profile_id,
        readiness_state_id=readiness_state_id,
    )
    new_id = listing["listing_id"]

    images = competitors.image_paths(listing_id)[:MAX_IMAGES_PER_LISTING]
    uploaded = 0
    for rank, img_path in enumerate(images, start=1):
        try:
            upload_listing_image(shop_id, new_id, img_path, rank=rank)
            uploaded += 1
        except Exception as e:  # noqa: BLE001 — garder le brouillon même si une image échoue
            log(f"  • image {img_path.name} ignorée ({e})")

    target = shops.active_shop()
    log(f"✓ Brouillon Etsy {new_id} créé ({uploaded} image(s)) dans {target.label}.")
    return {
        "source_listing_id": listing_id,
        "draft_listing_id": new_id,
        "title": title,
        "images_uploaded": uploaded,
        "image_total": len(images),
        "admin_url": listing_admin_url(new_id),
        "shop_key": target.key,
        "shop_label": target.label,
        "shop_id": str(target.shop_id),
    }


@app.get("/api/competitors")
def competitors_list() -> list[dict]:
    """All competitor listings already downloaded for analysis."""
    return competitors.list_all()


@app.post("/api/competitors/import")
def competitors_import(req: CompetitorImportReq) -> dict:
    """Fetch a listing via the Etsy API + download its images + eRank stats.

    Triggered by the user's explicit "Analyser ce listing" click. Read-only on
    Etsy (getListing); it does NOT modify or copy anything on Etsy.
    """
    if not req.ref.strip():
        raise HTTPException(status_code=400, detail="Référence de listing requise.")
    try:
        return competitors.import_listing(req.ref.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/competitors/{listing_id}")
def competitors_get(listing_id: int) -> dict:
    rec = competitors.get_one(listing_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Listing non téléchargé.")
    return rec


@app.delete("/api/competitors/{listing_id}")
def competitors_delete(listing_id: int) -> dict:
    """Remove a downloaded listing's LOCAL analysis cache (re-importable)."""
    if not competitors.delete_one(listing_id):
        raise HTTPException(status_code=404, detail="Listing non téléchargé.")
    return {"deleted": listing_id}


@app.get("/api/competitors/{listing_id}/image/{idx}")
def competitors_image(listing_id: int, idx: int):
    path = competitors.image_path(listing_id, idx)
    if path is None:
        raise HTTPException(status_code=404, detail="Image introuvable")
    return FileResponse(path)


@app.get("/api/competitors/{listing_id}/tag-analysis")
def competitors_tag_analysis(listing_id: int) -> dict:
    """eRank analysis of a downloaded listing's tags + better-tag suggestions."""
    try:
        return competitors.analyze_tags(listing_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Listing non téléchargé.")
    except niche.NicheError as e:
        raise _niche_http(e)


@app.post("/api/competitors/{listing_id}/import-draft")
def competitors_import_draft(listing_id: int, shop: str | None = None) -> dict:
    """Recrée le listing concurrent téléchargé en BROUILLON Etsy privé.

    Action explicite de l'utilisateur : copie titre / description / tags /
    matériaux / images dans un brouillon (state=draft, jamais publié) pour
    étudier la concurrence depuis l'éditeur Etsy. Aucune mise en ligne.

    `shop` (clé de slot "1", "2", …) choisit la boutique cible ; absent = la
    boutique par défaut. Une clé inconnue est gérée par le handler ShopError.
    """
    try:
        with use_shop(shop):
            return _import_competitor_as_draft(listing_id)
    except HTTPException:
        raise
    except shops.ShopError:
        # Laisse le handler global formater (400 boutique inconnue / 503 aucune).
        raise
    except RuntimeError as e:
        # Erreurs Etsy (token absent/expiré, profil d'expédition, quota…).
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001 — surface lisible côté UI
        raise HTTPException(status_code=502, detail=f"Échec de la création du brouillon : {e}")


# --------------------------------------------------------------------------- #
# Niche tracker : meilleures niches du moment (tendances + validation eRank)
# --------------------------------------------------------------------------- #
@app.post("/api/niche-tracker/scan")
def niche_tracker_scan(req: NicheScanReq) -> dict:
    """Start a background niche hunt (trends → eRank validation). Returns scan_id."""
    if not niche.is_up():
        raise HTTPException(
            status_code=503,
            detail="Service eRank (niche-detector) hors ligne. Lance-le d'abord.",
        )
    scan_id = niche_tracker.start_scan(
        max_seconds=req.max_seconds,
        min_searches=req.min_searches,
        ratio_pct=req.ratio_pct,
        verticals=req.verticals,
    )
    return {"scan_id": scan_id}


@app.get("/api/niche-tracker/verticals")
def niche_tracker_verticals() -> dict:
    """Catalogue des mondes produits explorables (clé + libellé)."""
    return {"verticals": niche_tracker.list_verticals()}


@app.get("/api/niche-tracker/scan/{scan_id}")
def niche_tracker_status(scan_id: str) -> dict:
    rec = niche_tracker.get_scan(scan_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Scan introuvable")
    return rec


@app.post("/api/niche-tracker/scan/{scan_id}/cancel")
def niche_tracker_cancel(scan_id: str) -> dict:
    if not niche_tracker.cancel_scan(scan_id):
        raise HTTPException(status_code=409, detail="Aucun scan actif à annuler.")
    return {"cancelled": True}


class SaveNicheReq(BaseModel):
    term: str
    last_month_searches: int | None = None
    avg_searches: int | None = None
    etsy_competition: int | None = None
    ctr: float | None = None
    kd: float | None = None
    momentum: float | None = None
    total_est_revenue: float | None = None
    vertical: str | None = None
    vertical_label: str | None = None


@app.get("/api/niche-tracker/saved")
def niche_saved_list() -> dict:
    """The user's memorised « meilleures niches » (excluded from future scans)."""
    items = niche_tracker.load_saved()
    return {"niches": items, "count": len(items)}


@app.post("/api/niche-tracker/saved")
def niche_saved_add(req: SaveNicheReq) -> dict:
    """Memorise a niche so the tracker never resurfaces it again."""
    try:
        rec = niche_tracker.save_niche(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"saved": rec, "count": len(niche_tracker.load_saved())}


@app.delete("/api/niche-tracker/saved/{term}")
def niche_saved_delete(term: str) -> dict:
    """Forget a memorised niche (it can resurface in future scans again)."""
    if not niche_tracker.delete_saved(term):
        raise HTTPException(status_code=404, detail="Niche non mémorisée.")
    return {"deleted": term, "count": len(niche_tracker.load_saved())}


# --------------------------------------------------------------------------- #
# Easy Picture : photos d'une URL produit (AliExpress…) → réf. Flow → listing
# --------------------------------------------------------------------------- #
class EasyPicFetchReq(BaseModel):
    url: str


class EasyPicManualReq(BaseModel):
    urls: list[str]
    title: str | None = None


class EasyPicGenerateReq(BaseModel):
    # Index (0-based) des photos sélectionnées dans la référence.
    indices: list[int]
    # True : générer de NOUVELLES images via Flow (1re photo = référence).
    # False : aperçu direct du listing à partir des photos sélectionnées.
    use_flow: bool = True
    # Sous-ensemble de prompts (1-based) — None = tous. Ignoré si use_flow=False.
    prompts: list[int] | None = None
    product_name: str | None = None
    auto_publish: bool = True
    shop: str | None = None
    # Tag eRank de base pour CE listing (remplace batch.txt). None/"" = défaut.
    base_tag: str | None = None
    # True : CHAQUE photo sélectionnée devient une référence Flow indépendante ;
    # toutes les images produites sont regroupées dans UN seul listing
    # (N photos → ≥ N images). Ignoré si use_flow=False ou < 2 photos.
    per_image: bool = False


def _easypic_slug(name: str | None, fallback: str) -> str:
    """Nom de produit → dossier/fichier sûr (sert de nom de dossier output/)."""
    raw = unicodedata.normalize("NFKC", (name or "").strip())
    raw = re.sub(r"[^\w\- ]+", "_", raw).strip()
    raw = re.sub(r"[\s_]+", "_", raw).strip("_")
    return raw[:80] or fallback


def _resolve_prompt_indices(prompts: list[int] | None) -> list[int] | None:
    """Valide une sélection de prompts (1-based) ; None/tous => None (= tous)."""
    if prompts is None:
        return None
    count = len(_prompt_lines())
    idx = sorted({int(i) for i in prompts})
    if not idx:
        raise HTTPException(status_code=400, detail="Aucun prompt sélectionné.")
    if idx[0] < 1 or idx[-1] > count:
        raise HTTPException(status_code=400, detail="Sélection de prompts invalide.")
    return None if len(idx) == count else idx


@app.post("/api/easypic/fetch")
def easypic_fetch(req: EasyPicFetchReq) -> dict:
    """Récupère + télécharge les photos d'une URL produit (AliExpress…).

    Lecture seule du site distant ; rien n'est écrit sur Etsy. Pour l'analyse /
    la génération de SES propres images, jamais pour republier les photos brutes.
    """
    try:
        return easypic.fetch(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/easypic/fetch-manual")
def easypic_fetch_manual(req: EasyPicManualReq) -> dict:
    """Repli : l'utilisateur colle directement des URLs d'images."""
    try:
        return easypic.fetch_manual(req.urls, title=req.title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/easypic")
def easypic_list() -> list[dict]:
    """Toutes les références d'images récupérées (les plus récentes d'abord)."""
    return easypic.list_all()


@app.get("/api/easypic/{item_id}")
def easypic_get(item_id: str) -> dict:
    rec = easypic.detail(item_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Référence introuvable.")
    return rec


@app.get("/api/easypic/{item_id}/image/{idx}")
def easypic_image(item_id: str, idx: int):
    p = easypic.image_path(item_id, idx)
    if p is None:
        raise HTTPException(status_code=404, detail="Image introuvable.")
    return FileResponse(p)


@app.delete("/api/easypic/{item_id}")
def easypic_delete(item_id: str) -> dict:
    """Supprime le cache local d'une référence (re-récupérable)."""
    if not easypic.delete_one(item_id):
        raise HTTPException(status_code=404, detail="Référence introuvable.")
    return {"deleted": item_id}


@app.post("/api/easypic/{item_id}/generate")
def easypic_generate(item_id: str, req: EasyPicGenerateReq) -> dict:
    """Photos sélectionnées → génération Flow (ou aperçu direct) → listing.

    use_flow=True  : la 1re photo sélectionnée sert de RÉFÉRENCE à Google Flow,
                     qui génère de NOUVELLES images ; le listing est ensuite bâti.
    use_flow=False : construit le listing directement depuis les photos
                     sélectionnées (aperçu, sans Flow) — pour étudier la fiche.
    Réutilise le même moteur de jobs que l'Atelier (une génération à la fois).
    """
    rec = easypic.get_one(item_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Référence introuvable.")
    if jobs.is_busy():
        raise HTTPException(status_code=409, detail="Une génération est déjà en cours.")

    paths = easypic.image_paths(item_id, req.indices)
    if not paths:
        raise HTTPException(status_code=400, detail="Sélectionne au moins une photo.")

    slug = _easypic_slug(req.product_name or rec.get("title"), f"easypic_{item_id}")

    # Valide la boutique cible dès maintenant (contexte requête) si on publie.
    if req.auto_publish:
        shops.get_shop(req.shop) if req.shop else shops.default_shop()

    def _ext(p: Path) -> str:
        e = p.suffix.lower()
        return e if e in ALLOWED_IMG_EXT else ".jpg"

    # Le callback de listing, avec le tag eRank choisi épinglé (Feature 1).
    build = _bound_build_listing(req.base_tag)

    if req.use_flow:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        prompt_indices = _resolve_prompt_indices(req.prompts)

        if req.per_image and len(paths) > 1:
            # Mode « 1 référence par photo → 1 seul listing » : CHAQUE photo
            # sélectionnée part dans input/ sous un nom distinct ; run.js génère
            # pour chacune, puis jobs regroupe TOUTES les images produites dans
            # output/<slug>/picture/ avant de bâtir UN listing (merge_into).
            input_files: list[str] = []
            for i, p in enumerate(paths, start=1):
                nm = f"{slug}__r{i}{_ext(p)}"
                shutil.copyfile(p, INPUT_DIR / nm)
                input_files.append(nm)
            job_id = jobs.create_job()
            jobs.start_pipeline(
                job_id,
                flow_dir=FLOW_DIR,
                input_files=input_files,
                build_listing=build,
                auto_publish=req.auto_publish,
                prompt_indices=prompt_indices,
                skip_images=False,
                shop=req.shop,
                merge_into=slug,
            )
            return {
                "job_id": job_id,
                "mode": "flow_multi",
                "folder": slug,
                "references": len(input_files),
            }

        # La 1re photo sélectionnée → input/<slug>.<ext> : run.js génère dans
        # output/<slug>/picture/, puis le listing est construit.
        src_name = f"{slug}{_ext(paths[0])}"
        shutil.copyfile(paths[0], INPUT_DIR / src_name)
        job_id = jobs.create_job()
        jobs.start_pipeline(
            job_id,
            flow_dir=FLOW_DIR,
            input_files=[src_name],
            build_listing=build,
            auto_publish=req.auto_publish,
            prompt_indices=prompt_indices,
            skip_images=False,
            shop=req.shop,
        )
        return {"job_id": job_id, "mode": "flow", "folder": slug}

    # Aperçu direct : copie les photos sélectionnées dans output/<slug>/picture/
    # puis construit le listing en mode « aperçu » (skip_images, sans Flow).
    pic_dir = SOURCE / slug / "picture"
    pic_dir.mkdir(parents=True, exist_ok=True)
    for old in pic_dir.glob("*"):
        if old.is_file():
            old.unlink()
    for i, p in enumerate(paths):
        shutil.copyfile(p, pic_dir / f"{i}{_ext(p)}")

    job_id = jobs.create_job()
    jobs.start_pipeline(
        job_id,
        flow_dir=FLOW_DIR,
        input_files=[f"{slug}{_ext(paths[0])}"],
        build_listing=build,
        auto_publish=req.auto_publish,
        prompt_indices=None,
        skip_images=True,
        shop=req.shop,
    )
    return {"job_id": job_id, "mode": "preview", "folder": slug}


# Serve the single-page frontend for everything else.
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
