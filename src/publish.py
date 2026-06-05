"""Publish a single product folder as an Etsy draft listing.

Usage:
    python -m src.publish <product_folder> [--config batch.txt]

End-to-end pipeline:
    folder photos -> vision analysis -> eRank tags -> Claude listing copy
                  -> Etsy taxonomy mapping -> create draft -> upload images
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_batch_config
from .flow import ensure_min_images, images_dir
from .etsy_client import (
    MAX_IMAGES_PER_LISTING,
    create_draft_listing,
    ensure_readiness_state_id,
    get_first_shipping_profile_id,
    get_shop_id,
    listing_admin_url,
    upload_listing_image,
)
from .generator import generate_listing
from .tags import get_optimized_tags
from .taxonomy import find_taxonomy_id
from .vision import IMAGE_MEDIA_TYPES, analyze_product_folder

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "batch.txt"


def publish_folder(folder: Path, config_path: Path) -> dict:
    """Run the full pipeline for one folder. Returns the created listing dict."""
    config = load_batch_config(config_path)
    shop_id = get_shop_id()

    shipping_profile_id = get_first_shipping_profile_id(shop_id)
    if shipping_profile_id is None:
        raise RuntimeError(
            "Your Etsy shop has no shipping profile configured. "
            "Create one in Etsy: Shop Manager > Settings > Shipping settings."
        )

    # Physical listings require a readiness/processing state. Reuse the shop's
    # existing one or create it from the batch config's processing times.
    readiness_state_id = ensure_readiness_state_id(
        shop_id,
        readiness_state=config.readiness_state,
        min_processing_time=config.min_processing_time,
        max_processing_time=config.max_processing_time,
        processing_time_unit=config.processing_time_unit,
    )

    print(f"\n=== {folder.name} ===")

    # 0. Make sure we have enough photos (regenerate via Flow if < 4).
    ensure_min_images(folder)

    # 1. Vision analysis
    print("  [1/5] Analyzing photos with Claude vision...")
    analysis = analyze_product_folder(folder)
    print(f"        product_type: {analysis['product_type']}")
    print(f"        top_level_category: {analysis['top_level_category']}")

    # 2. eRank tags
    print(f"  [2/5] Fetching optimized tags from eRank (base: {config.base_tag!r})...")
    partial_tags = get_optimized_tags(config.base_tag)
    print(f"        got {len(partial_tags)} tag(s) from eRank")

    # 3. Generate listing copy
    print("  [3/5] Generating title, description, 13 tags, materials...")
    listing_copy = generate_listing(
        analysis, partial_tags, language=config.language, base_tag=config.base_tag
    )
    print(f"        title: {listing_copy['title'][:80]}...")

    # 4. Taxonomy mapping
    print("  [4/5] Resolving Etsy taxonomy_id...")
    taxonomy_id = find_taxonomy_id(
        analysis["top_level_category"], analysis.get("subcategory_hint")
    )
    print(f"        taxonomy_id: {taxonomy_id}")

    # 5. Create draft listing
    print("  [5/5] Creating draft listing on Etsy...")
    listing = create_draft_listing(
        shop_id,
        title=listing_copy["title"],
        description=listing_copy["description"],
        price=config.price,
        quantity=config.quantity,
        taxonomy_id=taxonomy_id,
        tags=listing_copy["tags"],
        materials=listing_copy["materials"],
        who_made=config.who_made,
        when_made=config.when_made,
        shipping_profile_id=shipping_profile_id,
        readiness_state_id=readiness_state_id,
    )
    listing_id = listing["listing_id"]
    print(f"        listing_id: {listing_id}")

    # Image upload (photos live in the folder's picture/ subdir)
    images = sorted(
        f for f in images_dir(folder).iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_MEDIA_TYPES
    )[:MAX_IMAGES_PER_LISTING]
    print(f"  [+] Uploading {len(images)} image(s)...")
    for rank, img_path in enumerate(images, start=1):
        upload_listing_image(shop_id, listing_id, img_path, rank=rank)
        print(f"        ({rank}/{len(images)}) {img_path.name}")

    print(f"  Done. Edit at: {listing_admin_url(listing_id)}")
    return listing


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Publish a product folder as an Etsy draft.")
    parser.add_argument("folder", help="Path to a product folder (must contain images).")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to batch.txt config (default: {DEFAULT_CONFIG_PATH}).",
    )
    args = parser.parse_args(argv[1:])

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: {folder} is not a directory.")
        return 1

    try:
        publish_folder(folder, Path(args.config))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
