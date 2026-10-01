from enum import Enum


class Outcome(str, Enum):
    PROCEED = "proceed"
    HUMAN_QUEUE = "human_queue"
    SUSPENDED = "suspended"