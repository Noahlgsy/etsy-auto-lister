"""Full-vision niche tracker — multi-verticaux.

One click → spend up to ~5 minutes hunting for the best niches *right now*,
across several product worlds the user actually sells:

    • plush    — peluches (fandoms × plush/plushie)
    • figurine — figurines & collectibles (fandoms × figure/figurine/statue)
    • gaming   — monde gamer/geek : nouvelles sorties de jeux, consoles et
                 matériel électronique × merch (poster, porte-clés, sticker,
                 lampe, mug, tapis de souris, pin's)
    • home     — meuble / mobilier / étagère / déco maison : esthétiques
                 (cottagecore, dark academia, boho…) × types de produits
                 (étagère, lampe, miroir…) + fandoms × formats déco

Pipeline:
  1. Mine fresh cultural trends from public sources (Google Trends RSS, Reddit
     top posts — par sous-reddits propres à chaque vertical sélectionné,
     Wikipedia most-read) plus eRank-native trending keywords.
  2. For each selected vertical, normalise trends + curated seeds into
     candidate shoppable Etsy keywords (interleaved fairly across verticals so a
     timeout still yields diverse winners, not just one category).
  3. Validate every candidate against eRank (the source of truth) in batches.
  4. Keep only the ones that meet the user's strict bar (demand = LAST MONTH's
     searches, the live number — not the 12-month average):
        last_month_searches >= MIN_SEARCHES (2000)  AND
        last_month_searches >= RATIO% of the REAL etsy_competition (1000% → 10×)
  5. For the winners, pull top listings by revenue/day so the user sees the
     shops earning the most in the least time + money.

Each winner/near-miss carries its `vertical` so the UI can badge + group them.
Runs in a background thread with live progress; results are polled by the UI.
Read-only: it only reads trends + eRank, never writes to Etsy.
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import niche

# Strict defaults (the user's criteria) — overridable per scan.
MIN_SEARCHES = 2000
RATIO_PCT = 1000          # last-month searches must be >= 1000% of competition (= 10×)
DEFAULT_MAX_SECONDS = 300
MAX_CANDIDATES = 200      # global cap, split fairly across selected verticals

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TREND_GEOS = ["US", "FR"]


# --------------------------------------------------------------------------- #
# Verticals — chaque "monde" produit ses propres mots-clés Etsy achetables.
#
#   mode == "fandom" : seed = une licence/marque (ex. "demon slayer"). On émet
#       la tête nue + ses variantes suffixées (ex. "demon slayer plush"). Les
#       tendances fraîches (Google/Reddit/Wiki/eRank) alimentent AUSSI ces
#       verticaux : <tendance> + suffixe.
#   mode == "home"   : pas de suffixe ; on croise des esthétiques × des types de
#       produits (ex. "cottagecore shelf") + des fandoms × des formats déco
#       (ex. "stardew valley shelf"). Concurrence Etsy énorme sur les meubles
#       génériques → seul ce long-tail croisé peut passer le filtre strict.
#
# `subreddits` ne sert qu'aux verticaux "fandom" (les titres alimentent le pool
# de tendances partagé). Recherche en lecture seule uniquement.
# --------------------------------------------------------------------------- #
VERTICALS: dict[str, dict] = {
    "plush": {
        "label": "Peluches",
        "mode": "fandom",
        "suffixes": ("plush", "plushie"),
        "subreddits": ("anime", "movies", "television", "popculture", "Marvel"),
        "seeds": [
            # viral toys / collectibles
            "labubu", "sonny angel", "smiski", "blahaj", "jellycat", "squishmallow",
            "sanrio", "hello kitty", "kuromi", "cinnamoroll", "my melody",
            "pompompurin",
            # games
            "dandys world", "roblox", "minecraft", "stardew valley", "hollow knight",
            "undertale", "deltarune", "fnaf", "genshin impact", "palworld", "balatro",
            "cult of the lamb", "animal crossing", "splatoon", "kirby", "sonic",
            "super mario", "zelda", "pokemon", "omori", "poppy playtime",
            # anime / séries
            "demon slayer", "jujutsu kaisen", "one piece", "chainsaw man",
            "spy x family", "frieren", "bocchi the rock", "studio ghibli", "totoro",
            "bluey", "gabbys dollhouse", "wicked", "moana", "stitch", "inside out",
            # animaux / cute trends
            "capybara", "axolotl", "highland cow", "strawberry cow", "red panda",
            "frog", "duck", "goose", "shark", "jellyfish", "penguin", "possum",
            "moth", "mushroom", "bee", "cottagecore", "mallard duck",
        ],
    },
    "figurine": {
        "label": "Figurines",
        "mode": "fandom",
        "suffixes": ("figure", "figurine", "statue"),
        "subreddits": ("funkopop", "Gunpla", "actionfigures", "figurecollecting",
                       "anime"),
        "seeds": [
            # collectible lines / marques
            "funko pop", "nendoroid", "gundam", "gunpla", "amiibo", "pop mart",
            "hot toys", "bearbrick",
            # anime / manga
            "demon slayer", "jujutsu kaisen", "one piece", "naruto", "dragon ball",
            "chainsaw man", "spy x family", "my hero academia", "berserk",
            "evangelion", "bleach", "attack on titan", "frieren",
            # jeux
            "genshin impact", "honkai star rail", "league of legends",
            "final fantasy", "elden ring", "zelda", "pokemon", "sonic",
            "super mario", "kirby", "overwatch",
            # pop / films
            "transformers", "star wars", "marvel", "batman", "spiderman",
            "godzilla", "harry potter", "lord of the rings",
        ],
    },
    "gaming": {
        "label": "Gaming / Geek",
        "mode": "fandom",
        # merch achetable autour d'une sortie de jeu/console/hardware
        "suffixes": ("poster", "keychain", "sticker", "lamp", "mug", "mousepad",
                     "pin"),
        "subreddits": ("gaming", "Games", "pcgaming", "NintendoSwitch", "PS5",
                       "XboxSeriesX", "SteamDeck", "hardware", "gadgets"),
        "seeds": [
            # consoles / matériel (sorties récentes & à venir)
            "nintendo switch 2", "ps5 pro", "ps5", "steam deck", "rog ally",
            "xbox series x", "meta quest 3", "playdate", "analogue pocket",
            "nvidia rtx 5090", "legion go",
            # gros titres / sorties
            "gta 6", "elden ring", "shadow of the erdtree", "silksong",
            "hollow knight", "metroid prime 4", "pokemon legends za",
            "death stranding 2", "monster hunter wilds", "assassins creed shadows",
            "baldurs gate 3", "helldivers 2", "expedition 33", "black myth wukong",
            "palworld", "stardew valley", "minecraft", "roblox", "fortnite",
            "valorant", "league of legends", "genshin impact", "honkai star rail",
            # licences gamer evergreen
            "zelda", "super mario", "sonic", "kirby", "splatoon", "animal crossing",
            "cyberpunk 2077", "the witcher", "doom", "halo", "fallout", "skyrim",
            "balatro", "dandys world", "fnaf", "undertale", "deltarune", "omori",
            "dark souls", "sekiro", "terraria", "hades",
        ],
    },
    "home": {
        "label": "Maison / Mobilier",
        "mode": "home",
        "subreddits": ("CozyPlaces", "malelivingspace", "furniture",
                       "HomeDecorating", "InteriorDesign"),
        # esthétiques + motifs porteurs (faible concurrence en long-tail)
        "modifiers": [
            "cottagecore", "dark academia", "boho", "kawaii", "gothic", "fairycore",
            "mushroom", "cat", "frog", "moon", "celestial", "crystal", "vintage",
            "rustic", "scandinavian", "japandi", "coquette", "mid century",
            "goblincore", "witchy",
        ],
        # types de produits maison / mobilier / déco
        "products": [
            "shelf", "wall shelf", "floating shelf", "lamp", "night light", "mirror",
            "jewelry holder", "side table", "coat rack", "key holder", "bookends",
            "wall art", "candle holder", "plant stand", "trinket dish",
            "desk organizer", "incense holder", "wall hooks",
        ],
        # fandoms qui se déclinent bien en déco
        "fandoms": [
            "stardew valley", "zelda", "animal crossing", "minecraft",
            "studio ghibli", "totoro", "hello kitty", "sanrio", "pokemon",
            "super mario", "sonic", "harry potter", "lord of the rings",
            "star wars", "labubu", "jellycat",
        ],
        "fandom_forms": ("shelf", "lamp", "shelves", "night light", "wall art"),
    },
}

DEFAULT_VERTICALS = list(VERTICALS.keys())


def list_verticals() -> list[dict]:
    """Public catalogue for the UI: key + label + default-on flag."""
    return [
        {"key": k, "label": v["label"], "default": True}
        for k, v in VERTICALS.items()
    ]


def _norm_verticals(sel) -> list[str]:
    """Validate a requested vertical list; keep order; fall back to all."""
    if not sel:
        return list(DEFAULT_VERTICALS)
    out = [k for k in DEFAULT_VERTICALS if k in set(sel)]
    return out or list(DEFAULT_VERTICALS)


_STOP = {
    "the", "and", "for", "with", "your", "you", "this", "that", "from", "new",
    "best", "how", "what", "why", "are", "was", "his", "her", "their", "they",
    "des", "les", "une", "pour", "avec", "dans", "qui", "que", "sur",
    "official", "trailer", "season", "episode", "review", "video", "live",
}

_SCANS: dict[str, dict] = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Saved « meilleures niches » — the user's personal blacklist. Once a niche is
# memorised it is EXCLUDED from every future scan, so the tracker keeps
# surfacing NEW ideas that match the criteria instead of repeating winners.
# --------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_SAVED_FILE = _ROOT / "data" / "best_niches.json"
_SAVED_LOCK = threading.Lock()

# Stat fields kept alongside the term when a niche is memorised (what the UI
# needs to display the saved card).
_SAVED_FIELDS = (
    "last_month_searches", "avg_searches", "etsy_competition",
    "ctr", "kd", "momentum", "total_est_revenue", "vertical", "vertical_label",
)


def load_saved() -> list[dict]:
    """Return memorised niches (most recent first). Never raises."""
    try:
        with _SAVED_LOCK:
            if not _SAVED_FILE.exists():
                return []
            data = json.loads(_SAVED_FILE.read_text(encoding="utf-8"))
        items = data.get("niches") if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []
    except Exception:
        return []


def _write_saved(items: list[dict]) -> None:
    with _SAVED_LOCK:
        _SAVED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SAVED_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"niches": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_SAVED_FILE)


def saved_terms() -> set[str]:
    """Normalised set of memorised terms, used to exclude them from scans."""
    out: set[str] = set()
    for it in load_saved():
        t = _normalize(it.get("term") or "")
        if t:
            out.add(t)
    return out


def save_niche(payload: dict) -> dict:
    """Memorise one niche (idempotent by normalised term). Returns the record."""
    term = (payload.get("term") or "").strip()
    norm = _normalize(term)
    if not norm:
        raise ValueError("Terme de niche invalide.")
    record = {"term": norm, "saved_at": time.time()}
    for f in _SAVED_FIELDS:
        if payload.get(f) is not None:
            record[f] = payload[f]
    items = load_saved()
    # Replace any existing entry with the same normalised term, then prepend.
    items = [it for it in items if _normalize(it.get("term") or "") != norm]
    items.insert(0, record)
    _write_saved(items)
    return record


def delete_saved(term: str) -> bool:
    """Forget a memorised niche. Returns True if something was removed."""
    norm = _normalize(term or "")
    if not norm:
        return False
    items = load_saved()
    kept = [it for it in items if _normalize(it.get("term") or "") != norm]
    if len(kept) == len(items):
        return False
    _write_saved(kept)
    return True


# --------------------------------------------------------------------------- #
# Candidate normalisation
# --------------------------------------------------------------------------- #
def _normalize(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    s = s.replace("_", " ")
    s = re.sub(r"[^0-9a-zà-ÿ \-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    words = s.split()
    if not (1 <= len(words) <= 4):
        return None
    if len(s) < 3 or len(s) > 32:
        return None
    if all(w in _STOP for w in words):
        return None
    # Drop pure numbers / years
    if re.fullmatch(r"[\d \-]+", s):
        return None
    return s


# --------------------------------------------------------------------------- #
# Trend sources (each best-effort; failures are swallowed)
# --------------------------------------------------------------------------- #
def _google_trends() -> list[str]:
    out: list[str] = []
    for geo in _TREND_GEOS:
        try:
            r = requests.get(
                "https://trends.google.com/trending/rss",
                params={"geo": geo},
                headers={"User-Agent": _UA},
                timeout=15,
            )
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
            for item in root.iter("item"):
                t = item.findtext("title")
                if t:
                    out.append(t)
        except Exception:
            continue
    return out


def _reddit(subs: list[str], deadline: float) -> list[str]:
    out: list[str] = []
    for sub in subs:
        if time.time() > deadline:
            break
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/top.json",
                params={"t": "week", "limit": 20},
                headers={"User-Agent": "etsy-niche-tracker/1.0 (by /u/local)"},
                timeout=12,
            )
            if not r.ok:
                continue
            for child in (r.json().get("data", {}).get("children") or []):
                title = (child.get("data") or {}).get("title")
                if title:
                    out.append(title)
        except Exception:
            continue
    return out


def _wikipedia() -> list[str]:
    out: list[str] = []
    day = datetime.now(timezone.utc) - timedelta(days=1)
    try:
        r = requests.get(
            "https://en.wikipedia.org/api/rest_v1/feed/featured/"
            f"{day.year}/{day.month:02d}/{day.day:02d}",
            headers={"User-Agent": _UA},
            timeout=15,
        )
        if r.ok:
            for art in (r.json().get("mostread", {}).get("articles") or []):
                t = art.get("normalizedtitle") or art.get("title")
                if t and ":" not in t:  # skip Special:/Wikipedia: pages
                    out.append(t)
    except Exception:
        pass
    return out


def _erank_trending() -> list[str]:
    out: list[str] = []
    try:
        data = niche.get_json_sync("/api/trending", timeout=30)
    except Exception:
        return out

    def _walk(obj):
        if isinstance(obj, dict):
            for k in ("keyword", "term", "query", "name", "title"):
                v = obj.get(k)
                if isinstance(v, str):
                    out.append(v)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str):
            out.append(obj)

    _walk(data)
    return out


# --------------------------------------------------------------------------- #
# Candidate building (per vertical + interleaving)
# --------------------------------------------------------------------------- #
def _collect_trend_heads(rec: dict, deadline: float, verts: list[str]) -> list[str]:
    """Fresh, normalised head terms shared by the fandom verticals.

    Skipped entirely when no fandom vertical is selected (home is self-contained,
    so a "home-only" scan never spends time on trend feeds).
    """
    fandom_verts = [v for v in verts if VERTICALS[v]["mode"] == "fandom"]
    if not fandom_verts:
        return []

    subs: list[str] = []
    for v in fandom_verts:
        subs += list(VERTICALS[v].get("subreddits") or ())
    subs = list(dict.fromkeys(subs))[:8]  # de-dupe, bound request time

    sources = {
        "google_trends": _google_trends,
        "reddit": lambda: _reddit(subs, deadline),
        "wikipedia": _wikipedia,
        "erank_trending": _erank_trending,
    }
    seen: set[str] = set()
    heads: list[str] = []
    for name, fn in sources.items():
        if time.time() > deadline or rec.get("cancel"):
            break
        rec["phase"] = f"Collecte des tendances · {name}"
        raw = fn()
        kept = 0
        for item in raw:
            norm = _normalize(item)
            if norm and norm not in seen:
                seen.add(norm)
                heads.append(norm)
                kept += 1
        rec.setdefault("sources", {})[name] = {"raw": len(raw), "kept": kept}
    return heads


def _candidates_for_vertical(vkey: str, trend_heads: list[str],
                             per_v: int) -> list[str]:
    """Build up to ~`per_v` shoppable, normalised candidates for one vertical."""
    cfg = VERTICALS[vkey]
    out: list[str] = []
    seen: set[str] = set()
    soft_cap = max(per_v * 2, per_v + 20)

    def add(term: str) -> None:
        norm = _normalize(term)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)

    if cfg["mode"] == "fandom":
        suffixes = list(cfg["suffixes"])
        k_own = min(2, len(suffixes))      # bare head + up to 2 suffixed forms
        # 1) curated seeds : bare head + a sample of suffixed variants
        own = list(cfg["seeds"])
        random.shuffle(own)
        for s in own:
            ns = _normalize(s)
            if not ns:
                continue
            add(ns)
            for suf in random.sample(suffixes, k_own):
                if suf not in ns:
                    add(f"{ns} {suf}")
            if len(out) >= soft_cap:
                break
        # 2) fresh trends : suffixed only (a bare trend is rarely shoppable)
        th = list(trend_heads)
        random.shuffle(th)
        for h in th:
            if len(out) >= soft_cap:
                break
            suf = random.choice(suffixes)
            if suf not in h:
                add(f"{h} {suf}")
    else:  # home
        mods = list(cfg.get("modifiers") or [])
        prods = list(cfg.get("products") or [])
        fandoms = list(cfg.get("fandoms") or [])
        forms = list(cfg.get("fandom_forms") or [])
        combos: list[str] = []
        for m in mods:
            for p in prods:
                combos.append(f"{m} {p}")
        for f in fandoms:
            for fm in forms:
                combos.append(f"{f} {fm}")
        random.shuffle(combos)
        for c in combos:
            add(c)
            if len(out) >= soft_cap:
                break

    random.shuffle(out)
    return out[:per_v]


def _collect_candidates(rec: dict, deadline: float) -> tuple[list[str], dict[str, str]]:
    """Return (interleaved candidate terms, term→vertical map)."""
    verts = rec["verticals"]
    trend_heads = _collect_trend_heads(rec, deadline, verts)

    n = max(1, len(verts))
    per_v = max(24, MAX_CANDIDATES // n)
    exclude = rec.get("_exclude") or set()

    per_lists: dict[str, list[str]] = {}
    excluded_total = 0
    for vkey in verts:
        rec["phase"] = f"Idées · {VERTICALS[vkey]['label']}"
        lst = _candidates_for_vertical(vkey, trend_heads, per_v)
        if exclude:
            before = len(lst)
            lst = [t for t in lst if t not in exclude]
            excluded_total += before - len(lst)
        per_lists[vkey] = lst
        rec.setdefault("verticals_count", {})[vkey] = len(lst)

    # Round-robin interleave so the validation budget is spread evenly: if the
    # scan times out, every selected vertical still got a fair share.
    cands: list[str] = []
    vmap: dict[str, str] = {}
    seen: set[str] = set()
    i = 0
    while len(cands) < MAX_CANDIDATES:
        added = False
        for vkey in verts:
            lst = per_lists[vkey]
            if i < len(lst):
                added = True
                t = lst[i]
                if t not in seen:
                    seen.add(t)
                    vmap[t] = vkey
                    cands.append(t)
                    if len(cands) >= MAX_CANDIDATES:
                        break
        if not added:
            break
        i += 1

    rec["excluded_saved"] = excluded_total
    rec["candidates_total"] = len(cands)
    return cands, vmap


# --------------------------------------------------------------------------- #
# eRank validation
# --------------------------------------------------------------------------- #
def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _trend_momentum(trend: list[dict]) -> float:
    """Last month's value relative to the median of the series (1.0 = flat)."""
    vals = [t.get("value") or 0 for t in (trend or []) if isinstance(t, dict)]
    vals = [v for v in vals if v]
    if len(vals) < 3:
        return 1.0
    s = sorted(vals)
    median = s[len(s) // 2] or 1
    return round(vals[-1] / median, 2)


def _passes(searches, competition, min_searches: int, ratio_pct: int) -> bool:
    """`searches` is last-month demand; `competition` is the real Etsy listing count."""
    if not searches or searches < min_searches:
        return False
    comp = competition or 0
    if comp <= 0:
        return True  # genuinely ~0 competition → infinite ratio
    return searches >= (ratio_pct / 100.0) * comp


def _validate(cands: list[str], vmap: dict[str, str], rec: dict, deadline: float,
              min_searches: int, ratio_pct: int) -> tuple[list[dict], list[dict]]:
    winners: list[dict] = []
    near: list[dict] = []
    done = 0
    for chunk in _chunks(cands, 12):
        if time.time() > deadline or rec.get("cancel"):
            break
        rec["phase"] = f"Validation eRank · {done}/{len(cands)}"
        try:
            data = niche.get_json_sync(
                "/api/keywords/batch-stats",
                {"terms": ",".join(chunk)},
                timeout=90,
            )
        except Exception:
            done += len(chunk)
            rec["validated"] = done
            continue
        for term, s in (data.get("stats") or {}).items():
            avg = s.get("avg_searches")
            last_month = s.get("last_month_searches")
            # Demand = last month's searches (the live number). Fall back to the
            # average only when eRank has no dated trend for the term.
            searches = last_month if last_month is not None else avg
            comp = s.get("etsy_competition")
            trend = s.get("search_trend") or []
            vkey = vmap.get(term, "")
            row = {
                "term": term,
                "vertical": vkey,
                "vertical_label": VERTICALS.get(vkey, {}).get("label", ""),
                "searches": searches,                  # gate value (last month)
                "last_month_searches": last_month,
                "avg_searches": avg,
                "etsy_competition": comp,
                "ctr": s.get("ctr"),
                "kd": s.get("kd"),
                "avg_clicks": s.get("avg_clicks"),
                "momentum": _trend_momentum(trend),
                "trend": [t.get("value") for t in trend][-12:],
            }
            if _passes(searches, comp, min_searches, ratio_pct):
                winners.append(row)
            elif (searches and searches >= min_searches) or (avg and avg >= min_searches):
                near.append(row)  # real demand, but failed the gold bar
        done += len(chunk)
        rec["validated"] = done
        rec["winners_count"] = len(winners)

    def _ratio(r: dict) -> float:
        s = float(r.get("searches") or 0)
        c = float(r.get("etsy_competition") or 0)
        if s <= 0:
            return 0.0
        return 9999.0 if c <= 0 else s / c

    winners.sort(key=lambda r: (r.get("searches") or 0) * (r.get("momentum") or 1),
                 reverse=True)
    near.sort(key=lambda r: (_ratio(r), r.get("searches") or 0, r.get("avg_searches") or 0),
              reverse=True)
    return winners, near


# --------------------------------------------------------------------------- #
# Monetisation enrichment (top shops/listings for the winners)
# --------------------------------------------------------------------------- #
def _enrich(winners: list[dict], rec: dict, deadline: float, top_n: int = 8) -> None:
    for w in winners[:top_n]:
        if time.time() > deadline or rec.get("cancel"):
            break
        rec["phase"] = f"Monétisation · {w['term']}"
        try:
            data = niche.get_json_sync(
                f"/api/niche/{requests.utils.quote(w['term'])}/top-listings",
                {"n": 5, "sort": "ratio", "min_sales": 1},
                timeout=60,
            )
        except Exception:
            continue
        items = data.get("items") or []
        w["top_shops"] = [
            {
                "shop_name": it.get("shop_name"),
                "title": it.get("title"),
                "est_revenue": it.get("est_revenue"),
                "est_sales": it.get("est_sales"),
                "age_in_days": it.get("age_in_days"),
                "rev_per_day": it.get("rev_per_day"),
                "price": it.get("price"),
                "etsy_url": it.get("etsy_url") or it.get("url"),
            }
            for it in items[:5]
        ]
        w["total_est_revenue"] = round(
            sum(float(it.get("est_revenue") or 0) for it in items), 2
        )
        w["listings_with_sales"] = data.get("with_sales")


# --------------------------------------------------------------------------- #
# Job control
# --------------------------------------------------------------------------- #
def _worker(scan_id: str) -> None:
    rec = _SCANS[scan_id]
    deadline = rec["started_at"] + rec["max_seconds"]
    try:
        cands, vmap = _collect_candidates(rec, deadline)
        if not cands:
            rec["phase"] = "Aucune idée collectée"
        winners, near = _validate(
            cands, vmap, rec, deadline, rec["min_searches"], rec["ratio_pct"]
        )
        # Safety net : never surface a memorised niche even if it slipped
        # through (e.g. reached validation via a different source).
        exclude = rec.get("_exclude") or set()
        if exclude:
            winners = [w for w in winners if w.get("term") not in exclude]
            near = [n for n in near if n.get("term") not in exclude]
            rec["winners_count"] = len(winners)
        rec["phase"] = "Monétisation des meilleures niches"
        _enrich(winners, rec, deadline)
        rec["winners"] = winners
        rec["near_miss"] = near[:20]
        # Per-vertical winner tally for the UI summary.
        tally: dict[str, int] = {}
        for w in winners:
            tally[w.get("vertical") or "?"] = tally.get(w.get("vertical") or "?", 0) + 1
        rec["winners_by_vertical"] = tally
        rec["status"] = "cancelled" if rec.get("cancel") else "done"
        rec["phase"] = "Terminé"
    except Exception as e:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = str(e)
        rec["phase"] = "Erreur"
    finally:
        rec["elapsed"] = round(time.time() - rec["started_at"], 1)


def start_scan(*, max_seconds: int = DEFAULT_MAX_SECONDS,
               min_searches: int = MIN_SEARCHES,
               ratio_pct: int = RATIO_PCT,
               verticals: list[str] | None = None) -> str:
    scan_id = uuid.uuid4().hex[:12]
    verts = _norm_verticals(verticals)
    rec = {
        "id": scan_id,
        "status": "running",
        "phase": "Démarrage",
        "started_at": time.time(),
        "max_seconds": max(30, min(int(max_seconds), 600)),
        "min_searches": int(min_searches),
        "ratio_pct": int(ratio_pct),
        "verticals": verts,
        "verticals_labels": {k: VERTICALS[k]["label"] for k in verts},
        "candidates_total": 0,
        "validated": 0,
        "winners_count": 0,
        "excluded_saved": 0,
        "sources": {},
        "verticals_count": {},
        "winners": [],
        "near_miss": [],
        "cancel": False,
        # Snapshot of the memorised niches to exclude (captured at scan start so
        # the run is deterministic even if the user saves more mid-scan).
        "_exclude": saved_terms(),
    }
    with _LOCK:
        _SCANS[scan_id] = rec
        # Keep only the last ~10 scans in memory.
        if len(_SCANS) > 10:
            for old in sorted(_SCANS, key=lambda k: _SCANS[k]["started_at"])[:-10]:
                _SCANS.pop(old, None)
    threading.Thread(target=_worker, args=(scan_id,), daemon=True).start()
    return scan_id


def get_scan(scan_id: str) -> dict | None:
    rec = _SCANS.get(scan_id)
    if not rec:
        return None
    out = dict(rec)
    out["elapsed"] = round(time.time() - rec["started_at"], 1)
    out.pop("cancel", None)
    out.pop("_exclude", None)  # internal set — not JSON-serialisable / private
    return out


def cancel_scan(scan_id: str) -> bool:
    rec = _SCANS.get(scan_id)
    if not rec or rec["status"] != "running":
        return False
    rec["cancel"] = True
    return True
