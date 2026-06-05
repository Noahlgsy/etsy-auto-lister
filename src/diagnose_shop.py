"""Diagnostic: dump what the Etsy shop exposes for shipping + readiness.

Run this when listing creation fails with a shipping/readiness error:

    python -m src.diagnose_shop

It prints the raw API responses (status + JSON) for shipping profiles and the
candidate readiness/processing endpoints, plus whichever readiness_state_id the
client picks. Paste the output back so we can wire the correct field.
"""

from __future__ import annotations

import json
import sys

import requests

from .auth import get_api_headers
from .etsy_client import (
    ETSY_API_BASE,
    get_first_readiness_state_id,
    get_shop_id,
)

CANDIDATE_PATHS = (
    "shops/{shop_id}/shipping-profiles",
    "shops/{shop_id}/readiness-states",
    "shops/{shop_id}/processing-profiles",
)


def _dump(url: str, headers: dict) -> None:
    print(f"\nGET {url}")
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"  request failed: {exc}")
        return
    print(f"  status: {resp.status_code}")
    body = resp.text
    try:
        parsed = resp.json()
        body = json.dumps(parsed, ensure_ascii=False, indent=2)
    except ValueError:
        pass
    print("  body:")
    print("    " + body[:2000].replace("\n", "\n    "))


def main() -> int:
    shop_id = get_shop_id()
    headers = get_api_headers()
    print(f"shop_id: {shop_id}")

    for template in CANDIDATE_PATHS:
        _dump(f"{ETSY_API_BASE}/{template.format(shop_id=shop_id)}", headers)

    chosen = get_first_readiness_state_id(shop_id)
    print(f"\nget_first_readiness_state_id() -> {chosen!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
