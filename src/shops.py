"""Multi-shop registry for the Etsy auto-lister.

The app can drive several Etsy shops from a single dev-app (the keystring /
shared secret in ``x-api-key`` are shared; each shop only differs by its
``refresh_token`` + numeric ``shop_id`` + ``user_id``).

Env layout (in ``.env``):

    Shop "1" (the original, kept backward-compatible — unsuffixed keys):
        ETSY_REFRESH_TOKEN, ETSY_SHOP_ID, ETSY_USER_ID, ETSY_SHOP_LABEL

    Shops "2".."9" (suffixed):
        ETSY_SHOP2_REFRESH_TOKEN, ETSY_SHOP2_ID (alias ETSY_SHOP2_SHOP_ID),
        ETSY_SHOP2_USER_ID, ETSY_SHOP2_LABEL
        … and so on for 3, 4, …

A slot is *listed* only when BOTH its refresh token AND its shop_id are present,
so a half-configured shop (token but no shop_id, e.g. mid-setup) never shows up.

Active-shop selection uses a :class:`contextvars.ContextVar`, set per request via
the :func:`use_shop` context manager. ``contextvars`` are isolated per request
thread (Starlette runs sync endpoints in a per-request context), so concurrent
requests never see each other's selection. Background job threads do NOT inherit
the request's context, so the chosen shop must be passed explicitly into the job
and re-applied there with ``use_shop`` (see web/jobs.py + web/app.py).
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

ENV_PATH = Path(os.environ.get("ETSY_ENV_FILE") or (Path(__file__).resolve().parent.parent / ".env"))

# Slots scanned when listing shops. "1" maps to the legacy unsuffixed keys.
_MAX_SLOTS = 9
_SHOP_KEYS = [str(i) for i in range(1, _MAX_SLOTS + 1)]


class ShopError(Exception):
    """Base class for shop-resolution errors."""


class UnknownShopError(ShopError):
    """Raised when a requested shop key isn't configured."""


class NoShopsConfigured(ShopError):
    """Raised when no shop is configured at all (.env empty of shop creds)."""


@dataclass(frozen=True)
class Shop:
    """One configured Etsy shop.

    `key` is the registry slot ("1", "2", …) — a stable handle the frontend
    sends back; `shop_id` is the numeric Etsy id used in API paths.
    """

    key: str
    label: str
    shop_id: str
    refresh_token: str
    user_id: str | None = None


def _env_names(key: str) -> dict[str, str]:
    """Map a slot key to its env var names.

    Slot "1" uses the original unsuffixed keys (backward compatible); every
    other slot uses an ``ETSY_SHOP{key}_*`` prefix.
    """
    if key == "1":
        return {
            "refresh_token": "ETSY_REFRESH_TOKEN",
            "shop_id": "ETSY_SHOP_ID",
            "user_id": "ETSY_USER_ID",
            "label": "ETSY_SHOP_LABEL",
        }
    return {
        "refresh_token": f"ETSY_SHOP{key}_REFRESH_TOKEN",
        "shop_id": f"ETSY_SHOP{key}_ID",
        "user_id": f"ETSY_SHOP{key}_USER_ID",
        "label": f"ETSY_SHOP{key}_LABEL",
    }


def refresh_token_env_key(key: str) -> str:
    """The .env var holding the rotating refresh token for this shop slot.

    Etsy refresh tokens are single-use and rotate on every refresh, so the auth
    layer writes the new token back to exactly this key.
    """
    return _env_names(key)["refresh_token"]


def _read_shop(key: str) -> Shop | None:
    """Build a Shop from env for a slot, or None if not fully configured."""
    names = _env_names(key)
    refresh_token = (os.environ.get(names["refresh_token"]) or "").strip()
    shop_id = (os.environ.get(names["shop_id"]) or "").strip()
    if key != "1":
        # Accept ETSY_SHOP{N}_SHOP_ID as an alias for ETSY_SHOP{N}_ID.
        if not shop_id:
            shop_id = (os.environ.get(f"ETSY_SHOP{key}_SHOP_ID") or "").strip()
    if not (refresh_token and shop_id):
        return None
    label = (os.environ.get(names["label"]) or "").strip() or f"Boutique {key}"
    user_id = (os.environ.get(names["user_id"]) or "").strip() or None
    return Shop(
        key=key,
        label=label,
        shop_id=shop_id,
        refresh_token=refresh_token,
        user_id=user_id,
    )


def list_shops() -> list[Shop]:
    """Every fully-configured shop, in slot order (1, 2, …).

    Re-reads .env on each call (override=True) so a shop added via the OAuth
    setup tooling is picked up without restarting the server.
    """
    load_dotenv(ENV_PATH, override=True)
    shops: list[Shop] = []
    for key in _SHOP_KEYS:
        shop = _read_shop(key)
        if shop is not None:
            shops.append(shop)
    return shops


def get_shop(key: str) -> Shop:
    """Look up one shop by its slot key, or raise UnknownShopError."""
    key = str(key).strip()
    for shop in list_shops():
        if shop.key == key:
            return shop
    raise UnknownShopError(f"Boutique inconnue : {key!r}.")


def default_shop() -> Shop:
    """The first configured shop, or raise NoShopsConfigured."""
    shops = list_shops()
    if not shops:
        raise NoShopsConfigured(
            "Aucune boutique Etsy configurée. Lance `python -m src.auth` puis "
            "`python -m src.setup_shop`."
        )
    return shops[0]


# --------------------------------------------------------------------------- #
# Active shop (per-request / per-job selection)
# --------------------------------------------------------------------------- #
_active_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "active_shop_key", default=None
)


def active_shop() -> Shop:
    """Resolve the shop selected for the current context.

    Falls back to :func:`default_shop` when nothing was explicitly selected.
    """
    key = _active_key.get()
    if key is not None:
        return get_shop(key)
    return default_shop()


@contextmanager
def use_shop(key: str | None = None) -> Iterator[Shop]:
    """Set the active shop for the duration of the block.

    `key` None/empty selects the default shop. The shop is validated eagerly
    (raising UnknownShopError / NoShopsConfigured *before* the block runs), so
    callers can surface a clean error instead of failing mid-pipeline.
    """
    shop = get_shop(key) if (key not in (None, "")) else default_shop()
    token = _active_key.set(shop.key)
    try:
        yield shop
    finally:
        _active_key.reset(token)
