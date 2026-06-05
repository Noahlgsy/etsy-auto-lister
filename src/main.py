"""Batch publish: loop through every product folder and create an Etsy draft.

Usage:
    python -m src.main [--source <folder>] [--config batch.txt]

Defaults:
    source = ~/Desktop/Etsy/flow-automation/output/
    config = ~/Desktop/Etsy/etsy-auto-lister/batch.txt

Skips any sub-folder that contains no image files. Continues on per-folder
errors so a single bad folder doesn't abort the whole batch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .flow import images_dir
from .publish import DEFAULT_CONFIG_PATH, publish_folder
from .vision import IMAGE_MEDIA_TYPES

DEFAULT_SOURCE = Path.home() / "Desktop" / "Etsy" / "flow-automation" / "output"


def _has_images(folder: Path) -> bool:
    pics = images_dir(folder)
    if not pics.is_dir():
        return False
    return any(
        f.is_file() and f.suffix.lower() in IMAGE_MEDIA_TYPES
        for f in pics.iterdir()
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Batch-publish all product folders to Etsy as drafts.")
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help=f"Folder containing product sub-folders (default: {DEFAULT_SOURCE}).",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to batch.txt config (default: {DEFAULT_CONFIG_PATH}).",
    )
    args = parser.parse_args(argv[1:])

    source = Path(args.source)
    if not source.is_dir():
        print(f"ERROR: source folder not found: {source}")
        return 1

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"ERROR: config file not found: {config_path}")
        print("Tip: `cp batch.txt.example batch.txt` then edit it.")
        return 1

    candidates = [
        p for p in sorted(source.iterdir())
        if p.is_dir() and _has_images(p)
    ]
    if not candidates:
        print(f"No product folders with images found in {source}")
        return 1

    print(f"Found {len(candidates)} product folder(s) in {source.name}/")
    print(f"Using config: {config_path}")

    success: list[str] = []
    failed: list[tuple[str, str]] = []

    for folder in candidates:
        try:
            publish_folder(folder, config_path)
            success.append(folder.name)
        except Exception as exc:  # noqa: BLE001
            failed.append((folder.name, f"{type(exc).__name__}: {exc}"))
            print(f"  ! Failed on {folder.name}: {exc}")

    print("\n" + "=" * 60)
    print(f"Done. {len(success)} succeeded, {len(failed)} failed.")
    if failed:
        print("\nFailures:")
        for name, err in failed:
            print(f"  - {name}: {err}")
    print("\nReview your drafts at: https://www.etsy.com/your/shops/me/tools/listings")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
