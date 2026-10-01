from dataclasses import dataclass
from typing import Optional

from .outcome import Outcome


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    allowed: bool
    requires_human_review: bool
    suspended: bool
    decision_id: str
    reason: Optional[str] = None
    terminal_state: Optional[str] = None