"""
Script CLI : analyse les photos d'un dossier produit et affiche la fiche generee.

Usage :
    python -m src.analyze products/<nom-du-dossier>

Exemple :
    python -m src.analyze products/exemple-produit
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .vision import analyze_product_folder


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage : python -m src.analyze <chemin/vers/dossier/produit>")
        return 1

    folder = Path(argv[1])
    if not folder.is_dir():
        print(f"ERREUR : {folder} n'est pas un dossier valide.")
        return 1

    print(f"Analyse des photos de {folder}...\n")

    try:
        result = analyze_product_folder(folder)
    except RuntimeError as exc:
        print(f"ERREUR : {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERREUR inattendue : {type(exc).__name__}: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
