"""Background job runner for the "drop a photo -> draft listing" pipeline.

A job runs entirely in a daemon thread so the FastAPI request returns
immediately with a job id. The frontend then polls GET /api/jobs/{id}.

One image flows through these steps:
    images  -> `node run.js <file>` drives Google Flow (Chrome :9222) and
               writes flow-automation/output/<name>/picture/promptN.png
    listing -> analyse + eRank tags + Claude copy + taxonomy
    draft   -> (optional) create the Etsy draft and upload its images

Only ONE job may run at a time: every job drives the single shared Chrome
window, so concurrent runs would step on each other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid
from glob import glob
from pathlib import Path
from typing import Callable

import requests

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_ACTIVE = threading.Lock()  # held for the whole duration of a running job
_PROCS: dict[str, subprocess.Popen] = {}   # job_id -> running node subprocess

_MAX_LOG_LINES = 600
FLOW_DEBUG = "http://localhost:9222"        # Chrome remote-debugging endpoint

# Fallback per-image estimate (seconds) used for the ETA until we have measured
# at least one real Flow generation in the current job.
DEFAULT_GEN_SECONDS = 120


class _Cancelled(Exception):
    """Raised inside the worker when the user asks to stop the job."""


# --------------------------------------------------------------------------- #
# Node resolution (uvicorn may not inherit nvm's PATH)
# --------------------------------------------------------------------------- #
def resolve_node() -> str:
    found = shutil.which("node")
    if found:
        return found
    candidates = sorted(glob(str(Path.home() / ".nvm/versions/node/*/bin/node")))
    if candidates:
        return candidates[-1]
    return "node"


# --------------------------------------------------------------------------- #
# Google Flow / Chrome (remote debugging on :9222)
# --------------------------------------------------------------------------- #
def flow_is_up() -> bool:
    try:
        return requests.get(f"{FLOW_DEBUG}/json/version", timeout=3).ok
    except Exception:
        return False


def flow_browser() -> str:
    try:
        r = requests.get(f"{FLOW_DEBUG}/json/version", timeout=3)
        return r.json().get("Browser", "") if r.ok else ""
    except Exception:
        return ""


def flow_has_labs_tab() -> bool:
    try:
        r = requests.get(f"{FLOW_DEBUG}/json", timeout=3)
        return r.ok and any("labs.google" in (t.get("url", "") or "") for t in r.json())
    except Exception:
        return False


def _spawn_chrome(script: Path, flow_dir: Path) -> None:
    """Run launch-chrome.sh detached.

    Cold start  : launches Chrome (:9222) and opens the Flow tab.
    Chrome warm : Chrome forwards the Flow URL to the already-running instance
                  (same --user-data-dir), i.e. it opens the Flow tab in the
                  existing window instead of starting a second Chrome.
    """
    subprocess.Popen(
        ["bash", str(script)],
        cwd=str(flow_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_flow(flow_dir: Path, log=lambda m: None, timeout: int = 60) -> bool:
    """Ensure Chrome (:9222) AND a Google Flow tab are up, launching what's missing.

    Three cases are handled:
      * already ready          -> return immediately
      * Chrome down            -> start Chrome (which opens the Flow tab itself)
      * Chrome up, no Flow tab -> re-run the launcher so Chrome opens the Flow
                                  tab inside the already-running window
    """
    if flow_is_up() and flow_has_labs_tab():
        log("Chrome / Google Flow déjà prêt.")
        return True

    script = flow_dir / "launch-chrome.sh"
    if not script.is_file():
        raise RuntimeError(f"launch-chrome.sh introuvable dans {flow_dir}.")

    if not flow_is_up():
        log("Chrome n'est pas lancé. Démarrage de Chrome (Google Flow)…")
        _spawn_chrome(script, flow_dir)
        start = time.time()
        while time.time() - start < timeout:
            if flow_is_up():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Chrome ne répond pas sur le port 9222 après lancement.")
    elif not flow_has_labs_tab():
        log("Chrome est ouvert mais sans onglet Flow. Ouverture de l'onglet Google Flow…")
        _spawn_chrome(script, flow_dir)

    log("Attente de l'onglet Google Flow…")
    start = time.time()
    while time.time() - start < timeout:
        if flow_has_labs_tab():
            time.sleep(6)  # laisse la page se charger / restaurer la session
            log("Chrome / Google Flow prêt.")
            return True
        time.sleep(1)
    raise RuntimeError(
        "Onglet Google Flow introuvable. Connecte-toi à Flow dans la fenêtre Chrome "
        "ouverte, puis relance la génération."
    )


# --------------------------------------------------------------------------- #
# Job registry
# --------------------------------------------------------------------------- #
def _new_job() -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "pending",   # pending | running | done | error | cancelled
        "step": "",            # chrome | images | listing | draft | done
        "current": "",         # current input filename
        "cancel": False,       # set True to request a stop
        "logs": [],
        "items": [],           # one result dict per processed image
        "error": None,
        "created_at": time.time(),
        "started_at": None,    # when image generation actually began
        "progress_total": 0,   # total image generations expected
        "progress_done": 0,    # generations completed so far
        "eta_seconds": None,   # estimated seconds left (None until known)
        "_real_done": 0,       # internal: real (non-skipped) generations timed
    }


def create_job() -> str:
    job = _new_job()
    with _LOCK:
        _JOBS[job["id"]] = job
    return job["id"]


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        # shallow copy + copy mutable lists so the caller sees a snapshot
        snap = dict(job)
        snap["logs"] = list(job["logs"])
        snap["items"] = list(job["items"])
        return snap


def is_busy() -> bool:
    return _ACTIVE.locked()


def current_job() -> dict | None:
    """Return a snapshot of the job currently running/pending, or None.

    Only one job runs at a time, so this lets the UI re-attach to a running
    generation (after a refresh, or when /api/generate returns 409 "busy").
    """
    with _LOCK:
        active = [
            j for j in _JOBS.values() if j["status"] in ("pending", "running")
        ]
        job_id = (
            max(active, key=lambda j: j.get("created_at", 0))["id"]
            if active else None
        )
    return get_job(job_id) if job_id else None


def _count_prompts(flow_dir: Path) -> int:
    """Number of non-empty image prompts (one generation per prompt per image)."""
    f = flow_dir / "prompts_info.txt"
    if not f.is_file():
        return 0
    return sum(
        1 for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()
    )


def _is_cancelled(job_id: str) -> bool:
    with _LOCK:
        job = _JOBS.get(job_id)
        return bool(job and job.get("cancel"))


def request_cancel(job_id: str) -> bool:
    """Flag the job for cancellation and terminate its node subprocess."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job or job["status"] not in ("pending", "running"):
            return False
        job["cancel"] = True
        proc = _PROCS.get(job_id)
    if proc and proc.poll() is None:
        proc.terminate()
        threading.Timer(5.0, lambda: proc.poll() is None and proc.kill()).start()
    return True


def _update(job_id: str, **kw) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(kw)


def _log(job_id: str, line: str) -> None:
    line = line.rstrip("\n")
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["logs"].append(line)
        if len(job["logs"]) > _MAX_LOG_LINES:
            del job["logs"][: len(job["logs"]) - _MAX_LOG_LINES]


def _add_item(job_id: str, item: dict) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job["items"].append(item)


def _note_progress(job_id: str, line: str) -> None:
    """Advance the progress bar + ETA from a run.js log line.

    run.js prints "download fini" after each successful generation and
    "[skip] …" for prompts already done. Either one advances the bar; only real
    downloads feed the measured average used for the ETA.
    """
    low = line.lower()
    is_skip = "[skip]" in low
    is_real = "download fini" in low
    if not (is_skip or is_real):
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        total = job.get("progress_total") or 0
        job["progress_done"] = job.get("progress_done", 0) + 1
        if total:
            job["progress_done"] = min(job["progress_done"], total)
        if is_real:
            job["_real_done"] = job.get("_real_done", 0) + 1
        done = job["progress_done"]
        remaining = max(0, total - done) if total else 0
        real = job.get("_real_done", 0)
        started = job.get("started_at") or job.get("created_at") or time.time()
        if real >= 1:
            per = (time.time() - started) / real
        else:
            per = float(DEFAULT_GEN_SECONDS)
        job["eta_seconds"] = int(round(per * remaining)) if total else None


def _bump_progress(job_id: str) -> None:
    """Advance the bar by one unit when no run.js line drives _note_progress
    (used by the « aperçu seulement » mode, one unit per product)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        total = job.get("progress_total") or 0
        job["progress_done"] = job.get("progress_done", 0) + 1
        if total:
            job["progress_done"] = min(job["progress_done"], total)
        done = job["progress_done"]
        started = job.get("started_at") or job.get("created_at") or time.time()
        if total and done >= 1:
            per = (time.time() - started) / done
            job["eta_seconds"] = int(round(per * max(0, total - done)))
        else:
            job["eta_seconds"] = None


_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def _has_generated_images(flow_dir: Path, name: str) -> bool:
    """True if output/<name>/picture/ already holds at least one image."""
    pic = flow_dir / "output" / name / "picture"
    if not pic.is_dir():
        return False
    try:
        return any(
            p.is_file() and p.suffix.lower() in _IMG_EXT
            for p in pic.iterdir()
        )
    except OSError:
        return False


def _merge_pictures(flow_dir: Path, input_files: list[str], merge_into: str) -> int:
    """Pool every output/<ref-stem>/picture/* image into one folder
    output/<merge_into>/picture/ and return how many images were collected.

    The destination is cleared first so a re-run reflects ONLY the current
    references (no stale accumulation). Sources are copied (not moved) so
    run.js's per-reference skip-cache stays valid for future runs.
    """
    dest = flow_dir / "output" / merge_into / "picture"
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.iterdir():
        if old.is_file():
            old.unlink()
    n = 0
    for fname in input_files:
        stem = Path(fname).stem
        if stem == merge_into:
            continue  # garde-fou : ne lis jamais le dossier de destination
        src = flow_dir / "output" / stem / "picture"
        if not src.is_dir():
            continue
        for img in sorted(src.iterdir()):
            if img.is_file() and img.suffix.lower() in _IMG_EXT:
                n += 1
                shutil.copyfile(img, dest / f"{stem}__{img.name}")
    return n


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def start_pipeline(
    job_id: str,
    *,
    flow_dir: Path,
    input_files: list[str],
    build_listing: Callable[[str, Callable[[str], None], bool, str | None], dict],
    auto_publish: bool,
    prompt_indices: list[int] | None = None,
    skip_images: bool = False,
    shop: str | None = None,
    merge_into: str | None = None,
) -> None:
    """Spawn the daemon thread that runs the whole pipeline.

    `prompt_indices` (1-based) restricts which prompts are sent to Flow; None
    means every prompt in prompts_info.txt (unchanged behaviour).
    `skip_images` (mode « aperçu seulement ») : on saute entièrement Google
    Flow et on construit directement le listing à partir des images déjà
    présentes dans output/<nom>/picture/.
    `shop` is the Etsy shop slot key ("1", "2", …) to auto-publish into; it is
    passed to `build_listing` because this thread does NOT inherit the request's
    active-shop contextvar and must re-apply the selection itself.
    `merge_into` (mode « 1 référence par photo → 1 seul listing ») : chaque
    fichier d'`input_files` est généré par Flow comme référence indépendante,
    puis TOUTES les images produites sont regroupées dans
    output/<merge_into>/picture/ pour bâtir UN SEUL listing (au lieu d'un
    listing par fichier). Ignoré quand `skip_images` est vrai.
    """
    thread = threading.Thread(
        target=_run,
        args=(job_id, flow_dir, input_files, build_listing, auto_publish,
              prompt_indices, skip_images, shop, merge_into),
        daemon=True,
    )
    thread.start()


def _run(
    job_id: str,
    flow_dir: Path,
    input_files: list[str],
    build_listing: Callable[[str, Callable[[str], None], bool, str | None], dict],
    auto_publish: bool,
    prompt_indices: list[int] | None = None,
    skip_images: bool = False,
    shop: str | None = None,
    merge_into: str | None = None,
) -> None:
    if not _ACTIVE.acquire(blocking=False):
        _update(job_id, status="error", error="Une génération est déjà en cours.")
        _log(job_id, "ERREUR : une autre génération tourne déjà.")
        return

    node_bin = resolve_node()
    log = lambda m: _log(job_id, m)

    try:
        if skip_images:
            # Mode « aperçu seulement » : les images existent déjà, on ne touche
            # NI à Chrome NI à Google Flow. Une unité de progression = un produit.
            _update(job_id, status="running", step="listing")
            log("Mode aperçu : images déjà générées, Google Flow est ignoré.")
            total = max(1, len(input_files))
            _update(
                job_id,
                progress_total=total,
                progress_done=0,
                started_at=time.time(),
                eta_seconds=None,
            )
        else:
            _update(job_id, status="running", step="chrome")
            log("Vérification de Chrome / Google Flow…")
            launch_flow(flow_dir, log)

            num_prompts = len(prompt_indices) if prompt_indices else _count_prompts(flow_dir)
            total = max(1, len(input_files)) * max(1, num_prompts)
            _update(
                job_id,
                progress_total=total,
                progress_done=0,
                started_at=time.time(),
                eta_seconds=int(round(total * DEFAULT_GEN_SECONDS)),
            )

        # ------------------------------------------------------------------ #
        # Mode « 1 référence par photo → 1 seul listing » (merge_into).
        # Chaque fichier d'input_files est une référence Flow indépendante : on
        # génère pour chacune (un échec n'arrête pas les suivantes), PUIS on
        # regroupe toutes les images produites dans output/<merge_into>/picture/
        # et on bâtit UN SEUL listing. (skip_images n'utilise jamais ce mode.)
        # ------------------------------------------------------------------ #
        if merge_into and not skip_images:
            total_refs = len(input_files)
            for idx, fname in enumerate(input_files, start=1):
                if _is_cancelled(job_id):
                    raise _Cancelled()
                _update(job_id, current=fname, step="images")
                log(f"━━━ Référence {idx}/{total_refs} : {fname} ━━━")
                log("Génération des images via Google Flow (Chrome)…")
                try:
                    _run_node(job_id, flow_dir, node_bin, fname, prompt_indices)
                except _Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001 — isole l'échec d'une réf.
                    log(f"✗ Échec sur la référence {idx}/{total_refs} ({fname}) : {e}")
                    if idx < total_refs:
                        log("→ On passe à la référence suivante.")
            if _is_cancelled(job_id):
                raise _Cancelled()
            pooled = _merge_pictures(flow_dir, input_files, merge_into)
            log(f"{pooled} image(s) regroupée(s) dans un seul listing.")
            if pooled == 0:
                raise RuntimeError(
                    "Aucune image générée par Flow — listing non créé."
                )
            _update(job_id, current=merge_into, step="listing")
            log("Images prêtes. Construction du listing…")
            item = build_listing(merge_into, log, auto_publish, shop)
            item["input"] = f"{total_refs} référence(s)"
            item["references"] = total_refs
            item["image_count"] = pooled
            _add_item(job_id, item)
            if item.get("published"):
                _update(job_id, step="draft")
                log(f"✓ Brouillon Etsy créé : listing {item.get('listing_id')}")
            else:
                log("✓ Aperçu prêt (brouillon non créé).")
            _update(job_id, status="done", step="done", current="")
            log("=== TERMINÉ ===")
            return

        # Each input image is processed to completion — Flow images, then the
        # listing, then (optionally) its Etsy draft — BEFORE the next image is
        # started. A failure on one image is recorded and the batch moves on to
        # the next image (so "et ainsi de suite" holds even if one fails).
        total_files = len(input_files)
        failures = 0
        for idx, fname in enumerate(input_files, start=1):
            if _is_cancelled(job_id):
                raise _Cancelled()
            name = Path(fname).stem
            _update(job_id, current=fname, step="listing" if skip_images else "images")
            log(f"━━━ Produit {idx}/{total_files} : {fname} ━━━")
            if not skip_images:
                log("Génération des images via Google Flow (Chrome)…")
            try:
                if skip_images:
                    # On exige des images déjà présentes ; sinon échec clair.
                    if not _has_generated_images(flow_dir, name):
                        raise RuntimeError(
                            "Aucune image générée pour ce produit — "
                            "lance d'abord une génération."
                        )
                    log("Images existantes détectées, on saute la génération.")
                else:
                    _run_node(job_id, flow_dir, node_bin, fname, prompt_indices)

                if _is_cancelled(job_id):
                    raise _Cancelled()
                _update(job_id, step="listing")
                log("Images prêtes. Construction du listing…")
                item = build_listing(name, log, auto_publish, shop)
                item["input"] = fname
                _add_item(job_id, item)
                # En mode aperçu, aucune ligne run.js ne nourrit la barre :
                # on avance d'une unité par produit traité.
                if skip_images:
                    _bump_progress(job_id)

                if item.get("published"):
                    _update(job_id, step="draft")
                    log(f"✓ Brouillon Etsy créé : listing {item.get('listing_id')}")
                else:
                    log("✓ Aperçu prêt (brouillon non créé).")
            except _Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 — isolate one product's failure
                failures += 1
                _add_item(job_id, {
                    "input": fname, "folder": name,
                    "published": False, "error": str(e),
                })
                if skip_images:
                    _bump_progress(job_id)
                log(f"✗ Échec sur le produit {idx}/{total_files} ({fname}) : {e}")
                if idx < total_files:
                    log("→ On passe au produit suivant.")

        if failures >= total_files:
            _update(job_id, status="error", step="done", current="",
                    error=f"Les {total_files} produit(s) ont échoué.")
            log("=== TERMINÉ AVEC ERREURS ===")
        else:
            _update(job_id, status="done", step="done", current="")
            if failures:
                log(f"=== TERMINÉ : {total_files - failures}/{total_files} réussi(s), "
                    f"{failures} en échec ===")
            else:
                log("=== TERMINÉ ===")
    except _Cancelled:
        _update(job_id, status="cancelled", current="")
        _log(job_id, "■ Génération annulée.")
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        _update(job_id, status="error", error=str(e))
        _log(job_id, f"ERREUR : {e}")
    finally:
        with _LOCK:
            _PROCS.pop(job_id, None)
        _ACTIVE.release()


def _run_node(
    job_id: str,
    flow_dir: Path,
    node_bin: str,
    fname: str,
    prompt_indices: list[int] | None = None,
) -> None:
    """Run `node run.js <fname>` streaming its stdout into the job log.

    When `prompt_indices` is set, PROMPT_INDICES (comma-separated, 1-based) is
    exported so run.js only generates those prompts.
    """
    env = os.environ.copy()
    if prompt_indices:
        env["PROMPT_INDICES"] = ",".join(str(i) for i in prompt_indices)
    try:
        proc = subprocess.Popen(
            [node_bin, "run.js", fname],
            cwd=str(flow_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"node introuvable ({node_bin}). Node.js est-il installé ?") from e

    with _LOCK:
        _PROCS[job_id] = proc
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            _log(job_id, line)
            _note_progress(job_id, line)
        code = proc.wait()
    finally:
        with _LOCK:
            _PROCS.pop(job_id, None)

    if _is_cancelled(job_id):
        raise _Cancelled()
    if code != 0:
        raise RuntimeError(
            f"run.js a échoué (code {code}). "
            "Chrome est-il lancé sur le port 9222 (npm run chrome) et connecté à Google Flow ?"
        )
