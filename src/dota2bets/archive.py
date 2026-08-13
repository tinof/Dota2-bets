"""Gzipped archive of every raw odds payload.

The normalisers sit in front of an unofficial API that has already changed shape once
(moneyline selections arriving as ``designation`` in some payloads and ``participantId``
in others). Keeping the raw JSON means a normalisation bug found later can be repaired
by reprocessing, instead of silently poisoning a line history that cannot be re-recorded.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

DEFAULT_ARCHIVE_ROOT = Path("data/raw")


def archive_path(root: Path | str, source: str, captured_at: int) -> Path:
    """``<root>/<source>/<YYYY-MM-DD>/<epoch>.json.gz`` — one file per poll."""
    day = time.strftime("%Y-%m-%d", time.gmtime(captured_at))
    return Path(root) / source / day / f"{captured_at}.json.gz"


def write_payload(root: Path | str, source: str, captured_at: int, payload: Any) -> Path:
    path = archive_path(root, source, captured_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    return path


def read_payload(path: Path | str) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)
