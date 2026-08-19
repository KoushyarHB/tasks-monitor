"""Change dataclass shared by the diff engine and message rendering."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Change:
    issue_id: str
    sequence_id: int | None
    name: str
    kind: str  # new | state | priority | assignees | name | deleted
    old: str = ""
    new: str = ""
    is_mine: bool = False
    created_by: str = ""
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
