"""Control + proxy for the local niche-detector backend (eRank intelligence).

The niche-detector is a separate FastAPI app shipped under
    ../erank-tag-searcher-competition-fix/niche-detector/
It talks to the user's eRank Pro account (cookie in the eRank repo's .env) and
exposes the rich endpoints the competitive-research tabs rely on: keyword
stats, top listings by revenue/sales/age, shop/listing spy, tag suggestions and
niche discovery.

We run it as a *managed* local service on :8770 and the Etsy web app proxies to
it, so the browser only ever talks to one origin (:8000) and the eRank cookie
never leaves the server. Everything here is read-only eRank intelligence — it
never triggers an Etsy write or a Flow generation.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests

ROOT = Path(__file__).resolve().parent.parent                    # etsy-auto-lister/
ETSY_VENV_PY = ROOT / ".venv" / "bin" / "python"                 # healthy interpreter
ERANK_REPO = ROOT.parent / "erank-tag-searcher-competition-fix"  # sibling repo
NICHE_DIR = ERANK_REPO / "niche-detector"

NICHE_API_URL = os.environ.get("NICHE_API_URL", "http://127.0.0.1:8770").rstrip("/")
_split = urlsplit(NICHE_API_URL)
NICHE_HOST = _split.hostname or "127.0.0.1"
NICHE_PORT = _split.port or 8770

LOG_FILE = Path(os.environ.get("NICHE_LOG", "/tmp/niche-detector-8770.log"))

_proc: subprocess.Popen | None = None


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def is_up(timeout: float = 3.0) -> bool:
    """True if the niche-detector answers /api/health."""
    try:
        r = requests.get(f"{NICHE_API_URL}/api/health", timeout=timeout)
        return r.ok and bool(r.json().get("ok"))
    except Exception:
        return False


def health(timeout: float = 4.0) -> dict:
    """Health + eRank plan/cookie status (or {up:False})."""
    try:
        r = requests.get(f"{NICHE_API_URL}/api/health", timeout=timeout)
        data = r.json() if r.ok else {}
        return {"up": bool(r.ok and data.get("ok")), "url": NICHE_API_URL, **data}
    except Exception:
        return {"up": False, "url": NICHE_API_URL}


# --------------------------------------------------------------------------- #
# Launch (on demand)
# --------------------------------------------------------------------------- #
def _python() -> str:
    """Prefer the Etsy app's venv (known good); fall back to the current one."""
    if ETSY_VENV_PY.is_file():
        return str(ETSY_VENV_PY)
    return sys.executable


def launch(timeout: float = 45.0) -> dict:
    """Start the niche-detector on its port if it isn't already up.

    Idempotent: if something already answers on the port (e.g. started
    manually), this is a no-op. Blocks until healthy or `timeout`.
    """
    global _proc
    if is_up():
        return {"up": True, "already": True, "url": NICHE_API_URL}

    if not (NICHE_DIR / "app.py").is_file():
        raise FileNotFoundError(
            f"niche-detector introuvable: {NICHE_DIR} (app.py manquant)."
        )

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_FILE, "ab", buffering=0)
    _proc = subprocess.Popen(
        [_python(), "app.py", "--host", NICHE_HOST, "--port", str(NICHE_PORT)],
        cwd=str(NICHE_DIR),
        stdout=logf,
        stderr=logf,
        start_new_session=True,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_up():
            return {"up": True, "already": False, "url": NICHE_API_URL}
        if _proc.poll() is not None:  # process died early
            tail = ""
            try:
                tail = LOG_FILE.read_text(errors="replace")[-800:]
            except Exception:
                pass
            raise RuntimeError(
                f"niche-detector s'est arrêté au démarrage (code {_proc.returncode}).\n{tail}"
            )
        time.sleep(0.5)
    raise TimeoutError(f"niche-detector n'a pas démarré en {timeout:.0f}s.")


# --------------------------------------------------------------------------- #
# Proxy helpers
# --------------------------------------------------------------------------- #
class NicheError(RuntimeError):
    """Raised when the niche-detector returns a non-2xx response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def get_json(path: str, params: dict | None = None, *, timeout: float = 60.0):
    """Async GET against the niche-detector. `path` starts with '/api/...'.

    Raises NicheError(status, detail) on failure so callers can map it to an
    HTTP response.
    """
    url = f"{NICHE_API_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {})
    except httpx.HTTPError as e:
        raise NicheError(503, f"niche-detector injoignable: {e}") from e
    if r.status_code != 200:
        detail = ""
        try:
            detail = (r.json() or {}).get("detail") or r.text[:300]
        except Exception:
            detail = r.text[:300]
        raise NicheError(r.status_code, detail or f"HTTP {r.status_code}")
    return r.json()


def get_json_sync(path: str, params: dict | None = None, *, timeout: float = 60.0):
    """Synchronous sibling of get_json (used by the competitor importer)."""
    url = f"{NICHE_API_URL}{path}"
    try:
        r = requests.get(url, params=params or {}, timeout=timeout)
    except requests.RequestException as e:
        raise NicheError(503, f"niche-detector injoignable: {e}") from e
    if r.status_code != 200:
        try:
            detail = (r.json() or {}).get("detail") or r.text[:300]
        except Exception:
            detail = r.text[:300]
        raise NicheError(r.status_code, detail or f"HTTP {r.status_code}")
    return r.json()
