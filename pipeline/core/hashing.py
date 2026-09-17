"""
Job: Compute stable content hashes so collectors/analysts can skip unchanged writes.
Reads: nothing
Writes: nothing
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any


def hash_row(values: Mapping[str, Any]) -> str:
    """Stable hash of a row's values, independent of key order."""
    canonical = "|".join(f"{k}={values[k]!r}" for k in sorted(values))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
