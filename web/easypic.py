"""Easy Picture — récupère les photos d'un produit depuis une URL (AliExpress &
autres), pour servir de RÉFÉRENCE à une génération d'images Flow.

Le but est l'apprentissage / l'analyse : on télécharge les photos publiques d'un
produit (typiquement une fiche AliExpress) pour comprendre comment est construit
un listing, puis on génère SES PROPRES images avec Google Flow et on rédige une
annonce neuve. On ne republie jamais les photos d'autrui telles quelles.

AliExpress n'a pas d'API publique comme Etsy : on lit donc le HTML de la page et
on en extrait les URLs d'images par plusieurs stratégies, de la plus fiable à la
plus générique :

  1. ``imagePathList`` — le tableau d'images de la galerie, embarqué en JSON
     dans ``window.runParams`` (AliExpress).
  2. JSON-LD (`<script type="application/ld+json">` → champ ``image``).
  3. ``og:image`` (balise meta Open Graph).
  4. Regex CDN AliExpress (`ae0N.alicdn.com/kf/...`).
  5. Repli générique : balises ``<img src>`` / ``data-src`` (autres sites).

Stockage local sous ``data/easypic/<id>/`` (``meta.json`` + ``images/``), même
schéma que ``web/competitors.py``.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
STORE_DIR = ROOT / "data" / "easypic"
MAX_IMAGES = 24
_TIMEOUT = 30

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_PAGE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
    "Cache-Control": "no-cache",
}

_ALI_ITEM_RE = re.compile(r"/item/(\d+)\.html")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


# --------------------------------------------------------------------------- #
# Id / chemins
# --------------------------------------------------------------------------- #
def make_id(url: str) -> str:
    """Identifiant stable pour une URL : l'id article AliExpress sinon un hash.

    Stable = re-récupérer la même URL écrase la précédente (rafraîchissement),
    comme pour les listings concurrents.
    """
    m = _ALI_ITEM_RE.search(url or "")
    if m:
        return m.group(1)
    digest = hashlib.sha1((url or "").strip().encode("utf-8")).hexdigest()
    return digest[:12]


def _dir(item_id: str) -> Path:
    return STORE_DIR / str(item_id)


def _meta_path(item_id: str) -> Path:
    return _dir(item_id) / "meta.json"


# --------------------------------------------------------------------------- #
# Extraction des URLs d'images
# --------------------------------------------------------------------------- #
def _abs_url(u: str, base: str) -> str:
    u = (u or "").strip().strip("'\"")
    if not u:
        return ""
    u = _html.unescape(u)
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/") and base:
        return urljoin(base, u)
    return u


def _normalize_cdn(u: str) -> str:
    """Coupe le suffixe de taille/qualité AliExpress pour viser la pleine image.

    ``…/kf/Sxxx.jpg_220x220q75.jpg_.webp`` → ``…/kf/Sxxx.jpg``. Si l'URL est déjà
    propre, elle est renvoyée telle quelle.
    """
    m = re.search(r"^(https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp))", u, re.I)
    return m.group(1) if m else u


def _looks_like_icon(u: str) -> bool:
    low = u.lower()
    if low.startswith("data:"):
        return True
    bad = ("sprite", "icon", "logo", "avatar", "placeholder", "blank.gif", "loading")
    return any(b in low for b in bad)


def _from_image_path_list(html: str) -> list[str]:
    """Stratégie 1 : tableaux ``imagePathList`` (galerie AliExpress)."""
    out: list[str] = []
    for m in re.finditer(r'"imagePathList"\s*:\s*\[(.*?)\]', html, re.S):
        for raw in re.findall(r'"((?:https?:)?//[^"]+)"', m.group(1)):
            out.append(raw)
    # Variante : "imageModule":{...,"imagePathList":[...]} déjà couverte ci-dessus.
    return out


def _from_json_ld(html: str, base: str) -> list[str]:
    """Stratégie 2 : champ ``image`` des blocs JSON-LD."""
    out: list[str] = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        block = m.group(1).strip()
        try:
            data = json.loads(block)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            img = node.get("image")
            if isinstance(img, str):
                out.append(img)
            elif isinstance(img, list):
                out.extend(x for x in img if isinstance(x, str))
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                out.append(img["url"])
    return out


def _from_og(html: str) -> list[str]:
    """Stratégie 3 : balises ``og:image`` / ``twitter:image``."""
    out: list[str] = []
    for prop in ("og:image", "og:image:secure_url", "twitter:image"):
        out += re.findall(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            re.I,
        )
    return out


def _from_alicdn_regex(html: str) -> list[str]:
    """Stratégie 4 : toute URL CDN AliExpress présente dans le HTML."""
    return re.findall(
        r'(?:https?:)?//ae\d{1,2}\.alicdn\.com/kf/[^\s"\'<>\\]+?\.(?:jpg|jpeg|png|webp)',
        html,
        re.I,
    )


def _from_img_tags(html: str, base: str) -> list[str]:
    """Stratégie 5 (repli générique) : ``<img src>`` / ``data-src``."""
    out: list[str] = []
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        for attr in ("data-src", "data-lazy-src", "src"):
            am = re.search(rf'{attr}=["\']([^"\']+)["\']', tag, re.I)
            if am:
                out.append(am.group(1))
                break
    return out


def _extract_title(html: str) -> str | None:
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return _html.unescape(m.group(1)).strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        return _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() or None
    return None


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """URLs d'images produit, dédoublonnées, dans l'ordre de découverte.

    Les stratégies fiables (galerie AliExpress / JSON-LD / og:image / CDN) sont
    essayées d'abord ; le repli ``<img>`` ne sert que si rien n'a été trouvé,
    car il ramène beaucoup de bruit (icônes, bannières…).
    """
    candidates: list[str] = []
    candidates += _from_image_path_list(html)
    candidates += _from_json_ld(html, base_url)
    candidates += _from_og(html)
    candidates += _from_alicdn_regex(html)
    if not candidates:
        candidates += _from_img_tags(html, base_url)

    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        u = _abs_url(raw, base_url)
        if not u or _looks_like_icon(u):
            continue
        u = _normalize_cdn(u)
        low = u.lower().split("?")[0]
        if not low.endswith(_IMG_EXTS):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= MAX_IMAGES:
            break
    return out


# --------------------------------------------------------------------------- #
# Téléchargement
# --------------------------------------------------------------------------- #
def _ext_for(url: str, content_type: str | None) -> str:
    low = url.lower().split("?")[0]
    for ext in _IMG_EXTS:
        if low.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    if content_type:
        ct = content_type.lower()
        if "png" in ct:
            return ".png"
        if "webp" in ct:
            return ".webp"
    return ".jpg"


def _download_images(item_id: str, urls: list[str], referer: str) -> list[dict]:
    img_dir = _dir(item_id) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _UA, "Referer": referer, "Accept": "image/*,*/*;q=0.8"}
    saved: list[dict] = []
    for i, url in enumerate(urls):
        try:
            r = requests.get(url, headers=headers, timeout=_TIMEOUT)
        except Exception:
            continue
        if not (r.ok and r.content) or len(r.content) < 1024:
            continue  # ignore les réponses vides / minuscules (anti-hotlink, 1px)
        ext = _ext_for(url, r.headers.get("Content-Type"))
        dest = img_dir / f"{len(saved)}{ext}"
        try:
            dest.write_bytes(r.content)
        except Exception:
            continue
        saved.append({"index": len(saved), "file": dest.name, "src": url})
    return saved


# --------------------------------------------------------------------------- #
# Fetch (point d'entrée principal)
# --------------------------------------------------------------------------- #
def fetch(url: str) -> dict:
    """Récupère la page, extrait + télécharge ses images, renvoie le résumé.

    Lève ValueError (URL/extraction) ou RuntimeError (réseau) avec un message
    clair en français pour l'UI.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("URL vide.")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"URL invalide : {url!r}")

    try:
        resp = requests.get(url, headers=_PAGE_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"Page inaccessible : {e}") from e
    if resp.status_code != 200 or not resp.text:
        raise RuntimeError(
            f"La page a répondu {resp.status_code}. Le site bloque peut-être "
            f"la récupération — colle directement les URLs des images."
        )

    page_html = resp.text
    image_urls = extract_image_urls(page_html, url)
    if not image_urls:
        raise ValueError(
            "Aucune image trouvée sur cette page (site protégé ou format "
            "inattendu). Essaie une autre URL, ou colle les URLs des images."
        )

    item_id = make_id(url)
    return _store(item_id, url, _extract_title(page_html), image_urls)


def fetch_manual(urls: list[str], *, title: str | None = None) -> dict:
    """Repli : l'utilisateur colle directement des URLs d'images.

    Utile quand le site bloque le scraping de la page produit.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        u = _normalize_cdn(_abs_url(raw, ""))
        if not u or _looks_like_icon(u):
            continue
        if not u.lower().split("?")[0].endswith(_IMG_EXTS):
            continue
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    if not cleaned:
        raise ValueError("Aucune URL d'image valide (formats : jpg, png, webp).")
    key = "manual:" + "|".join(cleaned[:3])
    return _store(make_id(key), key, title, cleaned[:MAX_IMAGES], referer="")


def _store(
    item_id: str,
    url: str,
    title: str | None,
    image_urls: list[str],
    *,
    referer: str | None = None,
) -> dict:
    d = _dir(item_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)

    saved = _download_images(item_id, image_urls, referer if referer is not None else url)
    if not saved:
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError(
            "Images trouvées mais impossible de les télécharger (anti-hotlink "
            "du site). Réessaie ou colle les URLs des images directement."
        )

    record = {
        "id": item_id,
        "url": url,
        "title": title,
        "images": saved,
        "fetched_at": int(time.time()),
    }
    _meta_path(item_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary(record)


# --------------------------------------------------------------------------- #
# Lecture / liste / suppression
# --------------------------------------------------------------------------- #
def summary(record: dict) -> dict:
    return {
        "id": record.get("id"),
        "url": record.get("url"),
        "title": record.get("title"),
        "image_count": len(record.get("images") or []),
        "fetched_at": record.get("fetched_at"),
    }


def get_one(item_id: str) -> dict | None:
    p = _meta_path(item_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def detail(item_id: str) -> dict | None:
    """Record complet + chemins d'images servables par l'API."""
    rec = get_one(item_id)
    if rec is None:
        return None
    images = []
    for img in rec.get("images") or []:
        images.append({"index": img.get("index"), "src": img.get("src")})
    return {**summary(rec), "images": images}


def list_all() -> list[dict]:
    if not STORE_DIR.is_dir():
        return []
    out: list[dict] = []
    for sub in STORE_DIR.iterdir():
        if not sub.is_dir():
            continue
        p = sub / "meta.json"
        if not p.is_file():
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append(summary(rec))
    out.sort(key=lambda r: r.get("fetched_at") or 0, reverse=True)
    return out


def image_path(item_id: str, idx: int) -> Path | None:
    rec = get_one(item_id)
    if not rec:
        return None
    imgs = rec.get("images") or []
    if idx < 0 or idx >= len(imgs):
        return None
    p = _dir(item_id) / "images" / imgs[idx]["file"]
    return p if p.is_file() else None


def image_paths(item_id: str, indices: list[int] | None = None) -> list[Path]:
    """Chemins locaux des images.

    - ``indices=None`` : toutes les images, dans l'ordre stocké.
    - ``indices=[...]`` : uniquement celles-ci, **dans l'ordre demandé** — la
      1ʳᵉ sert de référence à Flow (``paths[0]``). Doublons, index inconnus et
      fichiers manquants sont ignorés.
    """
    rec = get_one(item_id)
    if not rec:
        return []
    imgs = rec.get("images") or []
    base = _dir(item_id) / "images"

    if indices is None:
        ordered = list(imgs)
    else:
        by_index = {img.get("index"): img for img in imgs}
        ordered = []
        seen: set[int] = set()
        for idx in indices:
            if idx in seen:
                continue
            seen.add(idx)
            img = by_index.get(idx)
            if img is not None:
                ordered.append(img)

    out: list[Path] = []
    for img in ordered:
        fname = img.get("file")
        if not fname:
            continue
        p = base / fname
        if p.is_file():
            out.append(p)
    return out


def delete_one(item_id: str) -> bool:
    d = _dir(item_id)
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True
