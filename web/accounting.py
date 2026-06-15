"""Comptabilité PCG : écritures en partie double depuis les ventes/achats Etsy.

Génère, en plan comptable général français, le **journal des ventes** (VE) et
le **journal des achats** (AC) à partir des commandes Etsy synchronisées, des
coûts de revient saisis et des dépenses pub. Chaque écriture est équilibrée
(Σ débit = Σ crédit) ; le total général l'est aussi.

⚠️ Cet outil PRÉPARE des écritures — il ne remplace pas un expert-comptable.
Le plan de comptes et surtout le traitement de la TVA (transfrontalière / OSS
pour les ventes hors France) doivent être validés par un professionnel.

Export au format **FEC** (Fichier des Écritures Comptables, arrêté du
29/07/2013) et en CSV lisible.

Schéma d'une écriture de vente (TVA désactivée — franchise en base) :
    411  Clients Etsy            D  revenue
        707  Ventes marchandises     C  sous-total
        7085 Ports facturés          C  port facturé

Avec TVA activée (taux r) :
    411                          D  revenue (TTC)
        707                          C  sous-total / (1+r)
        7085                         C  port / (1+r)
        44571 TVA collectée          C  TVA
"""

from __future__ import annotations

import io
from datetime import datetime

from . import finance

# Comptes PCG : clé interne -> (code par défaut, libellé). Seul le CODE est
# configurable dans l'UI ; le libellé reste la dénomination PCG standard.
ACCOUNTS: dict[str, tuple[str, str]] = {
    "client": ("411", "Clients Etsy"),
    "ventes": ("707", "Ventes de marchandises"),
    "ports": ("7085", "Ports et frais accessoires facturés"),
    "tva_col": ("44571", "TVA collectée"),
    "four_ali": ("401ALI", "Fournisseur AliExpress"),
    "four_etsy": ("401ETSY", "Fournisseur Etsy"),
    "achats": ("607", "Achats de marchandises"),
    "commission": ("6222", "Commissions et courtages sur ventes"),
    "paiement": ("627", "Services bancaires (frais de paiement)"),
    "pub": ("6231", "Annonces et insertions"),
    "transport": ("6242", "Transports sur ventes"),
    "banque": ("512", "Banque"),
}

DEFAULT_VAT_RATE = 20.0
EXCLUDED = finance.EXCLUDED_STATUSES


class AccountingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------------------- #
# Configuration (plan de comptes + TVA)
# --------------------------------------------------------------------------- #
def _raw_config() -> dict[str, str]:
    with finance._db() as conn:
        rows = conn.execute("SELECT key, value FROM acct_config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_config() -> dict:
    """Plan de comptes effectif + état TVA."""
    raw = _raw_config()
    accounts = {
        key: {"code": raw.get(f"acc_{key}", default), "label": label}
        for key, (default, label) in ACCOUNTS.items()
    }
    try:
        vat_rate = float(raw.get("vat_rate", DEFAULT_VAT_RATE))
    except (TypeError, ValueError):
        vat_rate = DEFAULT_VAT_RATE
    return {
        "vat_enabled": raw.get("vat_enabled", "0") == "1",
        "vat_rate": vat_rate,
        "accounts": accounts,
    }


def save_config(updates: dict) -> dict:
    """Met à jour les codes de comptes et/ou les réglages TVA."""
    sets: dict[str, str] = {}
    if "vat_enabled" in updates and updates["vat_enabled"] is not None:
        sets["vat_enabled"] = "1" if updates["vat_enabled"] else "0"
    if updates.get("vat_rate") is not None:
        rate = float(updates["vat_rate"])
        if not 0 <= rate <= 100:
            raise AccountingError(400, "Taux de TVA invalide (0–100).")
        sets["vat_rate"] = f"{rate:g}"
    for key, val in (updates.get("accounts") or {}).items():
        if key in ACCOUNTS and val:
            code = str(val).strip()
            if not code:
                raise AccountingError(400, f"Code de compte vide pour {key}.")
            sets[f"acc_{key}"] = code
    if not sets:
        raise AccountingError(400, "Rien à mettre à jour.")
    with finance._write_lock, finance._db() as conn:
        for key, val in sets.items():
            conn.execute(
                "INSERT INTO acct_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, val),
            )
    return get_config()


# --------------------------------------------------------------------------- #
# Génération des écritures
# --------------------------------------------------------------------------- #
def _r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def _line(code: str, label: str, debit: float = 0.0, credit: float = 0.0) -> dict:
    return {"account": code, "account_lib": label, "debit": _r2(debit), "credit": _r2(credit)}


def generate(days: int = 365, shop: str | None = None) -> dict:
    """Construit les journaux VE + AC sur la fenêtre. Tout en mémoire (lecture)."""
    cfg = get_config()
    acc = {k: v["code"] for k, v in cfg["accounts"].items()}
    lib = {k: v["label"] for k, v in cfg["accounts"].items()}
    vat = cfg["vat_enabled"]
    r = cfg["vat_rate"] / 100 if vat else 0.0
    fs = finance.settings_snapshot()

    orders = [o for o in finance.computed_orders(days, shop) if not o["excluded"]]
    orders.sort(key=lambda o: o["created_ts"])
    # Numéro de commande affiché dans l'app (#1 = la plus ancienne) : il sert de
    # référence de pièce dans le FEC, le même que dans l'onglet Commandes.
    seq = finance._seq_map(shop)

    ventes: list[dict] = []
    achats: list[dict] = []
    banque: list[dict] = []
    ve_num = ac_num = bq_num = 0

    for o in orders:
        date = datetime.fromtimestamp(o["created_ts"], finance.TZ).strftime("%Y-%m-%d")
        receipt = str(o["receipt_id"])
        order_no = seq.get(o["receipt_id"])
        # PieceRef = n° de commande (référence pour l'utilisateur) ; le réf Etsy
        # reste dans le libellé pour le rapprochement.
        piece = str(order_no) if order_no else receipt
        no = f"n°{order_no}" if order_no else receipt
        who = (o.get("buyer_name") or "").strip()
        subtotal = o["subtotal"]
        shipping = o["shipping_charged"]
        revenue = o["revenue"]

        # ---- Journal des ventes (VE) ----
        ve_num += 1
        lines = [_line(acc["client"], lib["client"], debit=revenue)]
        if vat:
            ht_sub = _r2(subtotal / (1 + r))
            ht_ship = _r2(shipping / (1 + r))
            tva = _r2(revenue - ht_sub - ht_ship)
            lines.append(_line(acc["ventes"], lib["ventes"], credit=ht_sub))
            if ht_ship:
                lines.append(_line(acc["ports"], lib["ports"], credit=ht_ship))
            lines.append(_line(acc["tva_col"], lib["tva_col"], credit=tva))
        else:
            lines.append(_line(acc["ventes"], lib["ventes"], credit=subtotal))
            if shipping:
                lines.append(_line(acc["ports"], lib["ports"], credit=shipping))
        ventes.append({
            "journal": "VE", "journal_lib": "Journal des ventes",
            "num": ve_num, "date": date, "piece": piece,
            "order_no": order_no, "receipt": receipt,
            "label": f"Vente Etsy {no} – {who} (réf {receipt})" if who
                     else f"Vente Etsy {no} (réf {receipt})",
            "lines": lines,
        })

        # ---- Journal des achats (AC) : frais Etsy (commission + paiement) ----
        commission = _r2(
            fs["transaction_fee_pct"] / 100 * revenue
            + fs["listing_fee"] * max(1, o["item_count"])
        )
        payment = _r2(
            fs["payment_fee_pct"] / 100 * (revenue + o["tax"]) + fs["payment_fee_fixed"]
        )
        if commission or payment:
            ac_num += 1
            achats.append({
                "journal": "AC", "journal_lib": "Journal des achats",
                "num": ac_num, "date": date, "piece": piece,
                "order_no": order_no, "receipt": receipt,
                "label": f"Frais Etsy cde {no} (réf {receipt})",
                "lines": [
                    _line(acc["commission"], lib["commission"], debit=commission),
                    _line(acc["paiement"], lib["paiement"], debit=payment),
                    _line(acc["four_etsy"], lib["four_etsy"], credit=_r2(commission + payment)),
                ],
            })

        # ---- Journal des achats (AC) : marchandises (COGS) + port payé ----
        cogs = o["cogs"]
        ship_paid = o["ship_cost"]
        if cogs or ship_paid:
            ac_num += 1
            lines = []
            if cogs:
                lines.append(_line(acc["achats"], lib["achats"], debit=cogs))
            if ship_paid:
                lines.append(_line(acc["transport"], lib["transport"], debit=ship_paid))
            lines.append(_line(acc["four_ali"], lib["four_ali"], credit=_r2(cogs + ship_paid)))
            achats.append({
                "journal": "AC", "journal_lib": "Journal des achats",
                "num": ac_num, "date": date, "piece": piece,
                "order_no": order_no, "receipt": receipt,
                "label": f"Marchandises + port cde {no} (réf {receipt})",
                "lines": lines,
            })

        # ---- Journal de banque (BQ) : paiement du fournisseur AliExpress ----
        # Une commande EXPÉDIÉE signifie que l'achat AliExpress a été payé : on
        # solde le fournisseur (401) par la banque (512). Daté à la date d'achat
        # saisie, sinon à la date d'expédition, sinon à la vente.
        paid = _r2(cogs + ship_paid)
        if o.get("shipped") and paid > 0:
            bq_num += 1
            pay_date = o.get("purchase_date")
            if not pay_date and o.get("shipped_at"):
                pay_date = datetime.fromtimestamp(
                    o["shipped_at"], finance.TZ
                ).date().isoformat()
            if not pay_date:
                pay_date = date
            banque.append({
                "journal": "BQ", "journal_lib": "Journal de banque",
                "num": bq_num, "date": pay_date, "piece": piece,
                "order_no": order_no, "receipt": receipt,
                "label": f"Paiement AliExpress cde {no} (réf {receipt})",
                "lines": [
                    _line(acc["four_ali"], lib["four_ali"], debit=paid),
                    _line(acc["banque"], lib["banque"], credit=paid),
                ],
            })

    # ---- Journal des achats (AC) : publicité Etsy Ads (par jour) ----
    until = datetime.now(finance.TZ).date()
    for e in finance.ads_entries(days=days):
        try:
            d = datetime.strptime(e["day"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (until - d).days >= days or e["amount"] <= 0:
            continue
        ac_num += 1
        amount = _r2(e["amount"])
        achats.append({
            "journal": "AC", "journal_lib": "Journal des achats",
            "num": ac_num, "date": e["day"], "piece": f"ADS{e['day']}",
            "label": "Publicité Etsy Ads",
            "lines": [
                _line(acc["pub"], lib["pub"], debit=amount),
                _line(acc["four_etsy"], lib["four_etsy"], credit=amount),
            ],
        })
    achats.sort(key=lambda e: (e["date"], e["num"]))
    banque.sort(key=lambda e: (e["date"], e["num"]))

    entries = ventes + achats + banque
    return {
        "vat_enabled": vat,
        "vat_rate": cfg["vat_rate"],
        "ventes": ventes,
        "achats": achats,
        "banque": banque,
        "totals": _ledger(entries),
        "summary": _journal_summary(ventes, achats, banque),
    }


def _ledger(entries: list[dict]) -> list[dict]:
    """Grand livre : cumul débit/crédit + solde par compte, trié par code."""
    acc: dict[str, dict] = {}
    for e in entries:
        for ln in e["lines"]:
            slot = acc.setdefault(
                ln["account"],
                {"account": ln["account"], "label": ln["account_lib"], "debit": 0.0, "credit": 0.0},
            )
            slot["debit"] += ln["debit"]
            slot["credit"] += ln["credit"]
    out = []
    for slot in acc.values():
        slot["debit"] = _r2(slot["debit"])
        slot["credit"] = _r2(slot["credit"])
        slot["balance"] = _r2(slot["debit"] - slot["credit"])
        out.append(slot)
    return sorted(out, key=lambda s: s["account"])


def _journal_summary(ventes: list[dict], achats: list[dict], banque: list[dict]) -> dict:
    def tot(entries: list[dict]) -> dict:
        d = _r2(sum(ln["debit"] for e in entries for ln in e["lines"]))
        c = _r2(sum(ln["credit"] for e in entries for ln in e["lines"]))
        return {"entries": len(entries), "debit": d, "credit": c}

    ve, ac, bq = tot(ventes), tot(achats), tot(banque)
    grand_d = _r2(ve["debit"] + ac["debit"] + bq["debit"])
    grand_c = _r2(ve["credit"] + ac["credit"] + bq["credit"])
    return {
        "ventes": ve,
        "achats": ac,
        "banque": bq,
        "total": {"debit": grand_d, "credit": grand_c, "balanced": abs(grand_d - grand_c) < 0.01},
    }


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #
def _fec_amount(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def _fec_date(ymd: str) -> str:
    return ymd.replace("-", "")


_FEC_HEADER = [
    "JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum",
    "CompteLib", "CompAuxNum", "CompAuxLib", "PieceRef", "PieceDate",
    "EcritureLib", "Debit", "Credit", "EcritureLet", "DateLet", "ValidDate",
    "Montantdevise", "Idevise",
]


def to_fec(data: dict) -> str:
    """Fichier des Écritures Comptables : 18 colonnes, séparateur tabulation."""
    rows = ["\t".join(_FEC_HEADER)]
    for e in data["ventes"] + data["achats"] + data["banque"]:
        ecr_num = f"{e['journal']}{e['num']:05d}"
        d = _fec_date(e["date"])
        for ln in e["lines"]:
            rows.append("\t".join([
                e["journal"], e["journal_lib"], ecr_num, d,
                ln["account"], ln["account_lib"], "", "",
                e["piece"], d, e["label"].replace("\t", " "),
                _fec_amount(ln["debit"]), _fec_amount(ln["credit"]),
                "", "", d, "", "EUR",
            ]))
    return "\r\n".join(rows) + "\r\n"


def to_csv(data: dict) -> str:
    """Export CSV lisible (Excel) : une ligne par mouvement, point-virgule."""
    import csv

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Date", "Journal", "Pièce", "Compte", "Libellé compte",
                "Libellé écriture", "Débit", "Crédit"])
    for e in data["ventes"] + data["achats"] + data["banque"]:
        for ln in e["lines"]:
            w.writerow([
                e["date"], e["journal"], e["piece"], ln["account"], ln["account_lib"],
                e["label"],
                _fec_amount(ln["debit"]) if ln["debit"] else "",
                _fec_amount(ln["credit"]) if ln["credit"] else "",
            ])
    return buf.getvalue()
