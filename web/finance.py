"""Suivi financier : ventes Etsy, coûts, résultat net, expéditions.

Source des ventes : l'API Etsy (getShopReceipts, scope ``transactions_r``),
synchronisée dans une base SQLite locale (``data/finance.db``). Tout le suivi
d'expédition (statut « expédié », n° de suivi, transporteur) est PUREMENT
LOCAL : rien n'est jamais écrit sur Etsy depuis ce module.

Modèle de coûts du résultat net, par commande :
    net = (sous-total + port facturé) − frais Etsy − coût de revient − port payé
puis, sur une période : net_total = Σ net − dépenses pub (Etsy Ads).

Les frais Etsy sont estimés à partir de taux configurables (réglages) :
commission transaction, frais de paiement (% + fixe), frais de mise en ligne,
TVA éventuelle sur les frais. Le coût de revient est saisi une fois par
listing ; le port payé par commande (ou une valeur par défaut).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from src import shops
from src.auth import get_api_headers
from src.etsy_client import ETSY_API_BASE

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "finance.db"

# Les heures/jours des tendances sont exprimés dans le fuseau du vendeur.
TZ = ZoneInfo("Europe/Paris")

# Statuts Etsy exclus du chiffre d'affaires (commande annulée/remboursée).
EXCLUDED_STATUSES = {"canceled", "cancelled", "fully refunded"}

_write_lock = threading.Lock()


class FinanceError(Exception):
    """Erreur métier exposée à l'UI avec un statut HTTP propre."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- #
# Base SQLite
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    receipt_id        INTEGER NOT NULL,
    shop_id           TEXT NOT NULL,
    created_ts        INTEGER NOT NULL,
    updated_ts        INTEGER,
    status            TEXT,
    is_shipped_etsy   INTEGER DEFAULT 0,
    buyer_country     TEXT,
    buyer_name        TEXT,
    currency          TEXT,
    subtotal          REAL DEFAULT 0,
    shipping_charged  REAL DEFAULT 0,
    tax               REAL DEFAULT 0,
    discount          REAL DEFAULT 0,
    grandtotal        REAL DEFAULT 0,
    item_count        INTEGER DEFAULT 0,
    items_json        TEXT,
    raw_json          TEXT,
    -- Suivi d'expédition LOCAL (jamais envoyé à Etsy)
    shipped           INTEGER DEFAULT 0,
    shipped_at        INTEGER,
    tracking_number   TEXT,
    carrier           TEXT,
    ship_note         TEXT,
    shipping_cost     REAL,
    PRIMARY KEY (receipt_id, shop_id)
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders (created_ts);

CREATE TABLE IF NOT EXISTS product_costs (
    listing_id  INTEGER PRIMARY KEY,
    title       TEXT,
    unit_cost   REAL NOT NULL DEFAULT 0,
    updated_ts  INTEGER
);

CREATE TABLE IF NOT EXISTS ad_spend (
    day     TEXT NOT NULL,
    shop_id TEXT NOT NULL DEFAULT '',
    amount  REAL NOT NULL DEFAULT 0,
    note    TEXT,
    PRIMARY KEY (day, shop_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    shop_id         TEXT PRIMARY KEY,
    last_sync_ts    INTEGER,
    receipts_total  INTEGER DEFAULT 0
);
"""

# Taux par défaut (modifiables dans l'onglet Coûts & frais). Les frais Etsy
# évoluent : ce sont des estimations, pas la facture Etsy officielle.
DEFAULT_SETTINGS: dict[str, float] = {
    "transaction_fee_pct": 6.5,    # commission Etsy sur articles + port
    "payment_fee_pct": 4.0,        # Etsy Payments (France : 4 % + 0,30 €)
    "payment_fee_fixed": 0.30,
    "listing_fee": 0.19,           # 0,20 $ ≈ 0,19 € par article vendu
    "fee_vat_pct": 0.0,            # TVA appliquée aux frais Etsy (FR : 20 si non assujetti)
    "default_shipping_cost": 0.0,  # port payé par défaut quand non saisi sur la commande
}


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# Réglages (taux de frais)
# --------------------------------------------------------------------------- #
def get_settings() -> dict[str, float]:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    out: dict[str, float] = {}
    for key, default in DEFAULT_SETTINGS.items():
        try:
            out[key] = float(stored.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


def save_settings(updates: dict[str, float]) -> dict[str, float]:
    clean: dict[str, float] = {}
    for key, value in updates.items():
        if key not in DEFAULT_SETTINGS or value is None:
            continue
        v = float(value)
        if v < 0:
            raise FinanceError(400, f"Valeur négative refusée pour {key}.")
        clean[key] = v
    with _write_lock, _db() as conn:
        for key, v in clean.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(v)),
            )
    return get_settings()


# --------------------------------------------------------------------------- #
# Synchronisation Etsy → SQLite
# --------------------------------------------------------------------------- #
def _money(m) -> float:
    """Etsy Money {amount, divisor} → float (0.0 si absent)."""
    if isinstance(m, dict):
        amount = m.get("amount")
        divisor = m.get("divisor") or 100
        if isinstance(amount, (int, float)) and divisor:
            return round(amount / divisor, 2)
        return 0.0
    try:
        return float(m or 0)
    except (TypeError, ValueError):
        return 0.0


def _receipt_row(r: dict, shop_id: str) -> tuple:
    created = int(r.get("created_timestamp") or r.get("create_timestamp") or 0)
    updated = int(r.get("updated_timestamp") or r.get("update_timestamp") or created)
    grand = r.get("grandtotal") or {}
    currency = (grand.get("currency_code") if isinstance(grand, dict) else None) or "EUR"
    items = []
    item_count = 0
    for t in r.get("transactions") or []:
        qty = int(t.get("quantity") or 1)
        item_count += qty
        items.append(
            {
                "listing_id": t.get("listing_id"),
                "title": (t.get("title") or "")[:140],
                "quantity": qty,
                "price": _money(t.get("price")),
            }
        )
    is_shipped = 1 if r.get("is_shipped") else 0
    return (
        int(r["receipt_id"]),
        str(shop_id),
        created,
        updated,
        (r.get("status") or "").lower(),
        is_shipped,
        (r.get("country_iso") or "").upper(),
        r.get("name") or "",
        currency,
        _money(r.get("subtotal")),
        _money(r.get("total_shipping_cost")),
        _money(r.get("total_tax_cost")) + _money(r.get("total_vat_cost")),
        _money(r.get("discount_amt")),
        _money(grand),
        item_count,
        json.dumps(items, ensure_ascii=False),
        json.dumps(r, ensure_ascii=False),
        # Initialisation du suivi LOCAL à la PREMIÈRE insertion seulement (le
        # ON CONFLICT n'y touche jamais) : une commande déjà expédiée côté Etsy
        # n'arrive pas dans « À expédier ». Ensuite, seul l'utilisateur décide.
        is_shipped,
        updated if is_shipped else None,
    )


# L'upsert ne touche QUE les colonnes Etsy : le suivi local (shipped, n° de
# suivi, port payé…) survit à toutes les synchronisations.
_UPSERT = """
INSERT INTO orders (
    receipt_id, shop_id, created_ts, updated_ts, status, is_shipped_etsy,
    buyer_country, buyer_name, currency, subtotal, shipping_charged, tax,
    discount, grandtotal, item_count, items_json, raw_json, shipped, shipped_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(receipt_id, shop_id) DO UPDATE SET
    updated_ts = excluded.updated_ts,
    status = excluded.status,
    is_shipped_etsy = excluded.is_shipped_etsy,
    buyer_country = excluded.buyer_country,
    buyer_name = excluded.buyer_name,
    currency = excluded.currency,
    subtotal = excluded.subtotal,
    shipping_charged = excluded.shipping_charged,
    tax = excluded.tax,
    discount = excluded.discount,
    grandtotal = excluded.grandtotal,
    item_count = excluded.item_count,
    items_json = excluded.items_json,
    raw_json = excluded.raw_json
"""


def _friendly_etsy_error(resp: requests.Response) -> FinanceError:
    text = (resp.text or "")[:400]
    low = text.lower()
    if resp.status_code in (401, 403):
        if "scope" in low or "insufficient" in low:
            return FinanceError(
                403,
                "Le token Etsy n'a pas la permission de lire les commandes "
                "(scope transactions_r). Relance « python -m src.auth » pour "
                "ré-autoriser l'app, puis re-synchronise.",
            )
        return FinanceError(
            403, f"Accès refusé par Etsy ({resp.status_code}) : {text}"
        )
    if resp.status_code == 429:
        return FinanceError(429, "Limite de l'API Etsy atteinte, réessaie dans une minute.")
    return FinanceError(502, f"Erreur API Etsy {resp.status_code} : {text}")


def _sync_one(shop: shops.Shop) -> dict:
    """Synchronise les commandes d'UNE boutique. Lecture seule côté Etsy."""
    headers = get_api_headers(shop)
    with _db() as conn:
        row = conn.execute(
            "SELECT last_sync_ts FROM sync_state WHERE shop_id = ?", (shop.shop_id,)
        ).fetchone()
    last_sync = int(row["last_sync_ts"]) if row and row["last_sync_ts"] else 0

    params: dict = {"limit": 100, "offset": 0}
    if last_sync:
        # Incrémental : tout ce qui a bougé depuis la dernière synchro (avec
        # 1 h de recouvrement) — attrape nouvelles commandes ET changements
        # de statut (expédié côté Etsy, remboursement…).
        params["min_last_modified"] = max(0, last_sync - 3600)

    synced = 0
    now = int(time.time())
    for _page in range(200):  # garde-fou : 20 000 commandes max par synchro
        resp = requests.get(
            f"{ETSY_API_BASE}/shops/{shop.shop_id}/receipts",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            raise _friendly_etsy_error(resp)
        results = resp.json().get("results") or []
        if not results:
            break
        rows = [_receipt_row(r, shop.shop_id) for r in results]
        with _write_lock, _db() as conn:
            conn.executemany(_UPSERT, rows)
        synced += len(results)
        if len(results) < params["limit"]:
            break
        params["offset"] += params["limit"]

    with _write_lock, _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE shop_id = ?", (shop.shop_id,)
        ).fetchone()["n"]
        conn.execute(
            "INSERT INTO sync_state (shop_id, last_sync_ts, receipts_total) "
            "VALUES (?, ?, ?) ON CONFLICT(shop_id) DO UPDATE SET "
            "last_sync_ts = excluded.last_sync_ts, receipts_total = excluded.receipts_total",
            (shop.shop_id, now, total),
        )
    return {
        "shop_key": shop.key,
        "shop_label": shop.label,
        "synced": synced,
        "total": total,
        "incremental": bool(last_sync),
    }


def sync(shop_key: str | None = None) -> dict:
    """Synchronise une boutique (clé de slot) ou TOUTES les boutiques configurées."""
    targets = [shops.get_shop(shop_key)] if shop_key else shops.list_shops()
    if not targets:
        raise FinanceError(
            503,
            "Aucune boutique Etsy configurée : renseigne .env puis lance "
            "« python -m src.auth ».",
        )
    results = []
    for shop in targets:
        try:
            results.append(_sync_one(shop))
        except FinanceError:
            raise
        except RuntimeError as e:  # token/refresh KO, .env incomplet…
            raise FinanceError(502, str(e)) from e
    return {"shops": results, "synced": sum(r["synced"] for r in results)}


def status() -> dict:
    """État de la synchro + compteurs, pour l'en-tête de l'onglet Ventes."""
    with _db() as conn:
        sync_rows = conn.execute("SELECT * FROM sync_state").fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    by_shop = {r["shop_id"]: dict(r) for r in sync_rows}
    items = []
    for s in shops.list_shops():
        st = by_shop.get(s.shop_id) or {}
        items.append(
            {
                "key": s.key,
                "label": s.label,
                "shop_id": s.shop_id,
                "last_sync_ts": st.get("last_sync_ts"),
                "orders": st.get("receipts_total") or 0,
            }
        )
    return {"shops": items, "total_orders": total}


# --------------------------------------------------------------------------- #
# Calculs : frais, coût de revient, net
# --------------------------------------------------------------------------- #
def _product_cost_map(conn: sqlite3.Connection) -> dict[int, float]:
    rows = conn.execute("SELECT listing_id, unit_cost FROM product_costs").fetchall()
    return {int(r["listing_id"]): float(r["unit_cost"]) for r in rows}


def _shop_id_for_key(shop_key: str | None) -> str | None:
    """Clé de slot UI ("1", "2"…) → shop_id numérique ; None = toutes."""
    if not shop_key:
        return None
    return shops.get_shop(shop_key).shop_id


def _compute(order: sqlite3.Row, st: dict, costs: dict[int, float]) -> dict:
    """Une ligne orders → dict + champs calculés (frais, COGS, net)."""
    o = dict(order)
    items = json.loads(o.pop("items_json") or "[]")
    o.pop("raw_json", None)
    o["items"] = items

    revenue = o["subtotal"] + o["shipping_charged"]
    fees = st["listing_fee"] * max(1, o["item_count"])
    fees += st["transaction_fee_pct"] / 100 * revenue
    fees += st["payment_fee_pct"] / 100 * (revenue + o["tax"]) + st["payment_fee_fixed"]
    fees *= 1 + st["fee_vat_pct"] / 100

    cogs = 0.0
    cogs_missing = False
    for it in items:
        lid = it.get("listing_id")
        if lid in costs:
            cogs += costs[lid] * it.get("quantity", 1)
        else:
            cogs_missing = True

    ship_cost = o["shipping_cost"] if o["shipping_cost"] is not None else st["default_shipping_cost"]
    excluded = (o["status"] or "") in EXCLUDED_STATUSES

    o.update(
        revenue=round(revenue, 2),
        fees=round(fees, 2),
        cogs=round(cogs, 2),
        cogs_missing=cogs_missing,
        ship_cost=round(float(ship_cost), 2),
        net=round(revenue - fees - cogs - float(ship_cost), 2),
        excluded=excluded,
        shipped=bool(o["shipped"]),
        is_shipped_etsy=bool(o["is_shipped_etsy"]),
    )
    return o


def _orders_window(
    days: int,
    shop_key: str | None = None,
    country: str | None = None,
    *,
    until_ts: int | None = None,
) -> list[dict]:
    """Commandes de la fenêtre, avec champs calculés, plus récentes d'abord."""
    until = int(until_ts if until_ts is not None else time.time())
    since = until - max(1, int(days)) * 86400
    shop_id = _shop_id_for_key(shop_key)

    sql = "SELECT * FROM orders WHERE created_ts >= ? AND created_ts < ?"
    args: list = [since, until]
    if shop_id:
        sql += " AND shop_id = ?"
        args.append(shop_id)
    if country:
        sql += " AND buyer_country = ?"
        args.append(country.upper())
    sql += " ORDER BY created_ts DESC"

    with _db() as conn:
        st = get_settings()
        costs = _product_cost_map(conn)
        rows = conn.execute(sql, args).fetchall()
    return [_compute(r, st, costs) for r in rows]


def _ads_total(days: int, *, until_ts: int | None = None) -> float:
    """Dépenses pub (globales) de la fenêtre, en jours LOCAUX (Europe/Paris)."""
    until = datetime.fromtimestamp(until_ts or time.time(), TZ).date()
    since = until - timedelta(days=max(1, int(days)) - 1)
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM ad_spend WHERE day >= ? AND day <= ?",
            (since.isoformat(), until.isoformat()),
        ).fetchone()
    return round(float(row["total"]), 2)


def _to_ship_count(shop_key: str | None = None) -> int:
    """Commandes à expédier, TOUTES périodes (non expédiées, non annulées)."""
    shop_id = _shop_id_for_key(shop_key)
    sql = "SELECT COUNT(*) AS n FROM orders WHERE shipped = 0 AND status NOT IN ('canceled','cancelled','fully refunded')"
    args: list = []
    if shop_id:
        sql += " AND shop_id = ?"
        args.append(shop_id)
    with _db() as conn:
        return int(conn.execute(sql, args).fetchone()["n"])


def summary(days: int = 30, shop: str | None = None, country: str | None = None) -> dict:
    """KPIs de la période + comparaison avec la période précédente."""
    now = int(time.time())
    orders = [o for o in _orders_window(days, shop, country) if not o["excluded"]]
    prev_orders = [
        o
        for o in _orders_window(days, shop, country, until_ts=now - days * 86400)
        if not o["excluded"]
    ]

    def _tot(rows: list[dict], key: str) -> float:
        return round(sum(o[key] for o in rows), 2)

    revenue = _tot(orders, "revenue")
    fees = _tot(orders, "fees")
    cogs = _tot(orders, "cogs")
    ship = _tot(orders, "ship_cost")
    ads = _ads_total(days)
    net = round(revenue - fees - cogs - ship - ads, 2)

    prev_revenue = _tot(prev_orders, "revenue")
    prev_net = round(
        prev_revenue
        - _tot(prev_orders, "fees")
        - _tot(prev_orders, "cogs")
        - _tot(prev_orders, "ship_cost")
        - _ads_total(days, until_ts=now - days * 86400),
        2,
    )

    currencies = [o["currency"] for o in orders if o.get("currency")]
    currency = max(set(currencies), key=currencies.count) if currencies else "EUR"

    return {
        "days": days,
        "currency": currency,
        "orders": len(orders),
        "items": sum(o["item_count"] for o in orders),
        "revenue": revenue,
        "fees": fees,
        "cogs": cogs,
        "cogs_missing_count": sum(1 for o in orders if o["cogs_missing"]),
        "shipping_cost": ship,
        "ads": ads,
        "net": net,
        "margin_pct": round(net / revenue * 100, 1) if revenue else None,
        "aov": round(revenue / len(orders), 2) if orders else None,
        "to_ship": _to_ship_count(shop),
        "prev": {"orders": len(prev_orders), "revenue": prev_revenue, "net": prev_net},
    }


def trends(days: int = 30, shop: str | None = None, country: str | None = None) -> dict:
    """Tendances : par jour (série), par heure 0-23, par jour de semaine, par pays.

    Le filtre ``country`` s'applique aux séries temporelles ; ``by_country``
    reste la ventilation complète (c'est elle qui sert de filtre).
    """
    all_orders = [o for o in _orders_window(days, shop) if not o["excluded"]]
    orders = [o for o in all_orders if not country or o["buyer_country"] == country.upper()]

    by_day: dict[str, dict] = {}
    today = datetime.now(TZ).date()
    for i in range(int(days)):
        d = today - timedelta(days=int(days) - 1 - i)
        by_day[d.isoformat()] = {"day": d.isoformat(), "orders": 0, "revenue": 0.0, "net": 0.0}
    by_hour = [{"hour": h, "orders": 0, "revenue": 0.0} for h in range(24)]
    by_weekday = [{"weekday": w, "orders": 0, "revenue": 0.0} for w in range(7)]

    for o in orders:
        local = datetime.fromtimestamp(o["created_ts"], TZ)
        day = local.date().isoformat()
        if day in by_day:
            slot = by_day[day]
            slot["orders"] += 1
            slot["revenue"] = round(slot["revenue"] + o["revenue"], 2)
            slot["net"] = round(slot["net"] + o["net"], 2)
        by_hour[local.hour]["orders"] += 1
        by_hour[local.hour]["revenue"] = round(by_hour[local.hour]["revenue"] + o["revenue"], 2)
        wd = local.weekday()  # 0 = lundi
        by_weekday[wd]["orders"] += 1
        by_weekday[wd]["revenue"] = round(by_weekday[wd]["revenue"] + o["revenue"], 2)

    countries: dict[str, dict] = {}
    for o in all_orders:
        iso = o["buyer_country"] or "??"
        slot = countries.setdefault(
            iso, {"country": iso, "orders": 0, "revenue": 0.0, "net": 0.0}
        )
        slot["orders"] += 1
        slot["revenue"] = round(slot["revenue"] + o["revenue"], 2)
        slot["net"] = round(slot["net"] + o["net"], 2)

    return {
        "days": days,
        "country": (country or "").upper() or None,
        "by_day": list(by_day.values()),
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "by_country": sorted(countries.values(), key=lambda c: -c["revenue"]),
    }


def list_orders(
    days: int = 30,
    shop: str | None = None,
    country: str | None = None,
    ship: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Liste paginée des commandes de la fenêtre (avec frais/net calculés)."""
    orders = _orders_window(days, shop, country)
    if ship == "to_ship":
        orders = [o for o in orders if not o["shipped"] and not o["excluded"]]
    elif ship == "shipped":
        orders = [o for o in orders if o["shipped"]]
    total = len(orders)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return {
        "orders": orders[offset : offset + limit],
        "total": total,
        "to_ship": _to_ship_count(shop),
    }


def set_shipping(receipt_id: int, fields: dict) -> dict:
    """Met à jour le suivi d'expédition LOCAL d'une commande.

    Champs acceptés : shipped (bool), tracking_number, carrier, ship_note
    (str), shipping_cost (float, None = retomber sur le défaut). Seuls les
    champs présents dans ``fields`` sont modifiés. Aucune écriture Etsy.
    """
    allowed = {"shipped", "tracking_number", "carrier", "ship_note", "shipping_cost"}
    sets: list[str] = []
    args: list = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "shipped":
            sets += ["shipped = ?", "shipped_at = ?"]
            args += [1 if value else 0, int(time.time()) if value else None]
        elif key == "shipping_cost":
            sets.append("shipping_cost = ?")
            args.append(None if value is None else max(0.0, float(value)))
        else:
            sets.append(f"{key} = ?")
            args.append((str(value).strip() or None) if value is not None else None)
    if not sets:
        raise FinanceError(400, "Rien à mettre à jour.")

    with _write_lock, _db() as conn:
        cur = conn.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE receipt_id = ?",
            (*args, int(receipt_id)),
        )
        if cur.rowcount == 0:
            raise FinanceError(404, f"Commande {receipt_id} introuvable.")
        row = conn.execute(
            "SELECT * FROM orders WHERE receipt_id = ?", (int(receipt_id),)
        ).fetchone()
        st = get_settings()
        costs = _product_cost_map(conn)
    return _compute(row, st, costs)


# --------------------------------------------------------------------------- #
# Coût de revient par produit
# --------------------------------------------------------------------------- #
def products() -> list[dict]:
    """Produits vendus (agrégés depuis les commandes) + coût de revient saisi.

    Inclut aussi les coûts saisis pour des produits sans vente sur la période
    de vie de la base (ils restent éditables).
    """
    with _db() as conn:
        rows = conn.execute("SELECT items_json FROM orders").fetchall()
        costs = {
            int(r["listing_id"]): dict(r)
            for r in conn.execute("SELECT * FROM product_costs").fetchall()
        }
    sold: dict[int, dict] = {}
    for r in rows:
        for it in json.loads(r["items_json"] or "[]"):
            lid = it.get("listing_id")
            if not lid:
                continue
            slot = sold.setdefault(
                int(lid),
                {"listing_id": int(lid), "title": it.get("title") or "", "sold_qty": 0},
            )
            slot["sold_qty"] += it.get("quantity", 1)
            if it.get("title"):
                slot["title"] = it["title"]

    out: list[dict] = []
    for lid, info in sold.items():
        cost = costs.pop(lid, None)
        out.append(
            {
                **info,
                "unit_cost": float(cost["unit_cost"]) if cost else None,
                "has_cost": cost is not None,
            }
        )
    for lid, cost in costs.items():  # coûts saisis sans vente enregistrée
        out.append(
            {
                "listing_id": lid,
                "title": cost.get("title") or "",
                "sold_qty": 0,
                "unit_cost": float(cost["unit_cost"]),
                "has_cost": True,
            }
        )
    return sorted(out, key=lambda p: -p["sold_qty"])


def set_product_cost(listing_id: int, unit_cost: float, title: str | None = None) -> dict:
    if unit_cost is None or float(unit_cost) < 0:
        raise FinanceError(400, "Coût de revient invalide.")
    with _write_lock, _db() as conn:
        conn.execute(
            "INSERT INTO product_costs (listing_id, title, unit_cost, updated_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(listing_id) DO UPDATE SET "
            "unit_cost = excluded.unit_cost, updated_ts = excluded.updated_ts, "
            "title = COALESCE(NULLIF(excluded.title, ''), product_costs.title)",
            (int(listing_id), (title or "").strip(), float(unit_cost), int(time.time())),
        )
    return {"listing_id": int(listing_id), "unit_cost": float(unit_cost)}


# --------------------------------------------------------------------------- #
# Dépenses pub (Etsy Ads) — saisies par jour, globales (toutes boutiques)
# --------------------------------------------------------------------------- #
def ads_list(days: int = 90) -> dict:
    until = datetime.now(TZ).date()
    since = until - timedelta(days=max(1, int(days)) - 1)
    with _db() as conn:
        rows = conn.execute(
            "SELECT day, amount, note FROM ad_spend WHERE day >= ? AND day <= ? ORDER BY day DESC",
            (since.isoformat(), until.isoformat()),
        ).fetchall()
    entries = [dict(r) for r in rows]
    return {"entries": entries, "total": round(sum(e["amount"] for e in entries), 2)}


def ads_set(day: str, amount: float, note: str | None = None) -> dict:
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise FinanceError(400, "Date invalide (format AAAA-MM-JJ).")
    if amount is None or float(amount) < 0:
        raise FinanceError(400, "Montant invalide.")
    with _write_lock, _db() as conn:
        conn.execute(
            "INSERT INTO ad_spend (day, shop_id, amount, note) VALUES (?, '', ?, ?) "
            "ON CONFLICT(day, shop_id) DO UPDATE SET amount = excluded.amount, note = excluded.note",
            (day, float(amount), (note or "").strip() or None),
        )
    return {"day": day, "amount": float(amount)}


def ads_delete(day: str) -> bool:
    with _write_lock, _db() as conn:
        cur = conn.execute("DELETE FROM ad_spend WHERE day = ? AND shop_id = ''", (day,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Données de démonstration (dev / aperçu sans clés Etsy)
# --------------------------------------------------------------------------- #
DEMO_SHOP_ID = "demo"


def seed_demo(n_days: int = 60) -> dict:
    """Insère des ventes factices (shop_id='demo') pour tester l'onglet Ventes.

    Déterministe (seed fixe). À purger avec ``clear_demo()`` /
    ``python -m web.finance --clear-demo``.
    """
    import random

    rng = random.Random(42)
    products_demo = [
        (9001, "Peluche renard kawaii personnalisée", 34.90),
        (9002, "Peluche dragon brodée prénom", 42.90),
        (9003, "Plush axolotl pastel fait main", 27.90),
    ]
    countries = ["FR"] * 32 + ["US"] * 24 + ["DE"] * 12 + ["GB"] * 10 + ["CA"] * 6 \
        + ["AU"] * 4 + ["NL"] * 4 + ["IT"] * 4 + ["ES"] * 2 + ["BE"] * 2
    hours = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 18, 19, 19, 20, 20, 20, 21, 21, 21, 22, 22, 23, 8, 7]
    names = ["Emma L.", "Liam K.", "Olivia M.", "Noah B.", "Mia S.", "Lucas P.", "Lea D.", "Hugo T."]

    now = int(time.time())
    rows = []
    rid = 5_000_001
    for day in range(n_days):
        for _ in range(rng.choice([0, 0, 1, 1, 1, 2, 2, 3])):
            lid, title, price = rng.choice(products_demo)
            qty = 1 if rng.random() < 0.85 else 2
            created = now - day * 86400 - rng.choice(hours) * 3600 - rng.randint(0, 3599)
            subtotal = round(price * qty, 2)
            shipping = rng.choice([0.0, 4.90, 6.90])
            tax = round(subtotal * rng.choice([0.0, 0.0, 0.08]), 2)
            status = "completed" if rng.random() > 0.04 else "canceled"
            items = [{"listing_id": lid, "title": title, "quantity": qty, "price": price}]
            rows.append((
                rid, DEMO_SHOP_ID, created, created, status,
                1 if day > 5 and rng.random() < 0.8 else 0,
                rng.choice(countries), rng.choice(names), "EUR",
                subtotal, shipping, tax, 0.0,
                round(subtotal + shipping + tax, 2),
                qty, json.dumps(items), "{}",
            ))
            rid += 1

    with _write_lock, _db() as conn:
        conn.executemany(_UPSERT, rows)
        # Les commandes « expédiées côté Etsy » sont aussi marquées localement,
        # pour un état de démo réaliste.
        conn.execute(
            "UPDATE orders SET shipped = 1, shipped_at = created_ts + 86400, "
            "tracking_number = 'LP00' || receipt_id || 'FR', carrier = 'La Poste' "
            "WHERE shop_id = ? AND is_shipped_etsy = 1",
            (DEMO_SHOP_ID,),
        )
    set_product_cost(9001, 7.20, "Peluche renard kawaii personnalisée")
    set_product_cost(9003, 5.10, "Plush axolotl pastel fait main")
    for day_off in range(0, 30, 3):
        d = (datetime.now(TZ).date() - timedelta(days=day_off)).isoformat()
        ads_set(d, 3.0, "démo")
    return {"orders": len(rows)}


def clear_demo() -> dict:
    """Purge TOUT ce que seed_demo a créé : commandes, pub « démo », coûts."""
    with _write_lock, _db() as conn:
        cur = conn.execute("DELETE FROM orders WHERE shop_id = ?", (DEMO_SHOP_ID,))
        ads = conn.execute("DELETE FROM ad_spend WHERE note = 'démo'")
        costs = conn.execute(
            "DELETE FROM product_costs WHERE listing_id IN (9001, 9002, 9003)"
        )
    return {
        "deleted": cur.rowcount,
        "ads_deleted": ads.rowcount,
        "costs_deleted": costs.rowcount,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Outils de la base finances.")
    parser.add_argument("--seed-demo", action="store_true", help="insère des ventes de démo")
    parser.add_argument("--clear-demo", action="store_true", help="supprime les ventes de démo")
    cli_args = parser.parse_args()
    if cli_args.seed_demo:
        print(seed_demo())
    elif cli_args.clear_demo:
        print(clear_demo())
    else:
        parser.print_help()
