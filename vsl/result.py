from dataclasses import dataclass
from typing import Any

from .decision import Decision


@dataclass(frozen=True)
class GovernedResult:
    decision: Decision
    performed: bool
    result: Any = None