"""Load batch configuration from a simple key:value text file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULTS: dict[str, str] = {
    "currency": "EUR",
    "language": "en",
    "quantity": "1",
    "who_made": "i_did",
    "when_made": "made_to_order",
    "min_processing_time": "1",
    "max_processing_time": "3",
    "processing_time_unit": "weeks",
}

REQUIRED = ["price", "base_tag"]

VALID_WHO_MADE = {"i_did", "collective", "someone_else"}
VALID_LANGUAGES = {"en", "fr"}
VALID_TIME_UNITS = {"days", "weeks"}


@dataclass(frozen=True)
class BatchConfig:
    price: float
    base_tag: str
    currency: str
    language: str
    quantity: int
    who_made: str
    when_made: str
    min_processing_time: int
    max_processing_time: int
    processing_time_unit: str

    @property
    def readiness_state(self) -> str:
        """Etsy readiness_state value derived from when_made."""
        return "made_to_order" if self.when_made == "made_to_order" else "ready_to_ship"


def load_batch_config(path: Path) -> BatchConfig:
    if not path.is_file():
        raise FileNotFoundError(
            f"Batch config not found: {path}. "
            "Copy `batch.txt.example` to `batch.txt` and fill it in."
        )

    data = dict(DEFAULTS)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip().lower()] = value.strip()

    for k in REQUIRED:
        if k not in data or not data[k]:
            raise ValueError(f"Missing required field `{k}` in {path}")

    if data["who_made"] not in VALID_WHO_MADE:
        raise ValueError(
            f"`who_made` must be one of {sorted(VALID_WHO_MADE)}, got {data['who_made']!r}"
        )
    if data["language"] not in VALID_LANGUAGES:
        raise ValueError(
            f"`language` must be one of {sorted(VALID_LANGUAGES)}, got {data['language']!r}"
        )
    if data["processing_time_unit"] not in VALID_TIME_UNITS:
        raise ValueError(
            f"`processing_time_unit` must be one of {sorted(VALID_TIME_UNITS)}, "
            f"got {data['processing_time_unit']!r}"
        )

    return BatchConfig(
        price=float(data["price"]),
        base_tag=data["base_tag"],
        currency=data["currency"].upper(),
        language=data["language"].lower(),
        quantity=int(data["quantity"]),
        who_made=data["who_made"].lower(),
        when_made=data["when_made"].lower(),
        min_processing_time=int(data["min_processing_time"]),
        max_processing_time=int(data["max_processing_time"]),
        processing_time_unit=data["processing_time_unit"].lower(),
    )
