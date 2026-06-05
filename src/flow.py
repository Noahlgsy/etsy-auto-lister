"""Regenerate product photos via the Google Flow automation script.

If a product output folder has fewer than MIN_IMAGES photos, we shell out to the
flow-automation `run.js` (Playwright) script targeting that product's source
image to (re)generate its photos before publishing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Un seul visuel suffit pour un aperçu / un brouillon : on ne (re)génère via Flow
# que lorsqu'un dossier produit est totalement vide (0 image). Les chemins web
# (aperçu + publication) n'imposent de toute façon aucun minimum.
MIN_IMAGES = 1
INPUT_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")

PICTURE_SUBDIR = "picture"
VIDEO_SUBDIR = "video"


def images_dir(folder: Path) -> Path:
    """Where a product's generated photos live: the `picture/` subfolder.

    Falls back to the product folder itself for legacy layouts that predate the
    picture/ + video/ split.
    """
    sub = folder / PICTURE_SUBDIR
    return sub if sub.is_dir() else folder


def videos_dir(folder: Path) -> Path:
    """Where a product's generated video lives: the `video/` subfolder.

    Falls back to the product folder itself for legacy layouts.
    """
    sub = folder / VIDEO_SUBDIR
    return sub if sub.is_dir() else folder


def count_images(folder: Path) -> int:
    """Number of image files in the product's `picture/` subfolder."""
    pics = images_dir(folder)
    if not pics.is_dir():
        return 0
    return sum(
        1
        for f in pics.iterdir()
        if f.is_file() and f.suffix.lower() in INPUT_EXTS
    )


def _flow_dir_for(folder: Path) -> Path:
    """Locate the flow-automation dir from a product output folder.

    Product folders live at `<flow-automation>/output/<name>`, so the flow dir
    is two levels up. Validated by the presence of `run.js`.
    """
    candidate = folder.parent.parent
    if (candidate / "run.js").is_file():
        return candidate
    raise RuntimeError(
        f"Could not locate the Flow script (run.js) two levels above {folder}."
    )


def _find_input_image(flow_dir: Path, folder_name: str) -> Path | None:
    """The source image in `<flow-automation>/input` matching this product."""
    input_dir = flow_dir / "input"
    for ext in INPUT_EXTS:
        candidate = input_dir / f"{folder_name}{ext}"
        if candidate.is_file():
            return candidate
    return None


def ensure_min_images(folder: Path, *, min_images: int = MIN_IMAGES) -> int:
    """Generate photos with Flow if `folder` has fewer than `min_images`.

    Returns the image count after the (possible) generation run. If the source
    image is missing we leave the folder as-is (can't regenerate).
    """
    count = count_images(folder)
    if count >= min_images:
        return count

    flow_dir = _flow_dir_for(folder)
    source = _find_input_image(flow_dir, folder.name)
    if source is None:
        print(
            f"  [flow] {folder.name}: {count} image(s) < {min_images}, but no "
            f"source in {flow_dir / 'input'} — skipping regeneration."
        )
        return count

    print(
        f"  [flow] {folder.name}: {count} image(s) < {min_images} → generating "
        f"with Flow (source: {source.name}). This drives a browser; make sure "
        f"Chrome is running (npm run chrome) and you're logged into Google Flow."
    )
    result = subprocess.run(["node", "run.js", source.name], cwd=str(flow_dir))
    if result.returncode != 0:
        raise RuntimeError(
            "Flow generation failed. Start Chrome with "
            "`cd ~/Desktop/Etsy/flow-automation && npm run chrome`, log into "
            "Google Flow, then retry."
        )

    new_count = count_images(folder)
    print(f"  [flow] {folder.name}: now {new_count} image(s).")
    return new_count


def find_video(folder: Path) -> Path | None:
    """Return the product video from `folder/video/` (prefers video.mp4)."""
    vids = videos_dir(folder)
    if not vids.is_dir():
        return None
    canonical = vids / "video.mp4"
    if canonical.is_file():
        return canonical
    for f in sorted(vids.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            return f
    return None


def ensure_video(folder: Path) -> Path | None:
    """Generate a product video with Veo if none exists yet.

    Best-effort: if the source image is missing, video.js isn't present, or the
    generation run fails (e.g. Chrome not running), we warn and return None so
    publishing can still proceed with images only.
    """
    existing = find_video(folder)
    if existing is not None:
        return existing

    flow_dir = _flow_dir_for(folder)
    source = _find_input_image(flow_dir, folder.name)
    if source is None:
        print(
            f"  [flow] {folder.name}: no source image — skipping video generation."
        )
        return None
    if not (flow_dir / "video.js").is_file():
        print(
            f"  [flow] {folder.name}: video.js not found in {flow_dir} — "
            f"skipping video generation."
        )
        return None

    print(
        f"  [flow] {folder.name}: no video → generating with Veo "
        f"(source: {source.name}). Chrome must be running and logged into Flow."
    )
    result = subprocess.run(["node", "video.js", source.name], cwd=str(flow_dir))
    if result.returncode != 0:
        print(
            f"  [flow] {folder.name}: Veo video generation failed — continuing "
            f"without a video. (Is Chrome running on port 9222 and logged into "
            f"Google Flow?)"
        )
        return None

    return find_video(folder)
