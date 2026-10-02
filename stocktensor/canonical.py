"""Canonical JSON and hashing.

Every signed or hashed object in Stocktensor (forecasts, task results, epoch
bundles) is serialised the same way, so the Python validator and the
TypeScript verifier produce byte-identical output:

- object keys sorted, no whitespace (``separators=(",", ":")``)
- ASCII only
- no floats: numbers that are not integers travel as decimal strings

``canonical`` raises on floats instead of guessing a format, so a float can
never slip into a hash by accident.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _check(value: Any, path: str = "$") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        if isinstance(value, str) and not value.isascii():
            raise ValueError(f"non-ascii string at {path}")
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise TypeError(f"float at {path}: encode decimals as strings")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}")
            _check(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    raise TypeError(f"unsupported type {type(value).__name__} at {path}")


def canonical(value: Any) -> bytes:
    """Canonical JSON bytes for ``value``."""
    _check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """Lowercase hex sha256 of the canonical JSON of ``value``."""
    return sha256_hex(canonical(value))
