"""Atomic JSON snapshot persistence for the watchdog."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def save_state(path: str, data: dict[str, Any]) -> None:
    """Atomically write a JSON snapshot (tmp file + os.replace)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str) -> dict[str, Any] | None:
    """Load a snapshot; return None if missing or corrupt."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
