from dataclasses import dataclass
from typing import Any, Literal

CURRENT_RADIO_PROFILE = "medium-int8-word-timestamps-v1"
CURRENT_RADIO_POLICY = "f1-confidence-gate-v1"


@dataclass(frozen=True, slots=True)
class FinalTranscriptDecision:
    status: Literal["accepted", "rejected", "deferred"]
    text: str | None
    reason: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RadioSummary:
    profile: str
    policy: str
    total: int
    accepted: int
    rejected: int
    manual: int

    def to_manifest(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "policy": self.policy,
            "total": self.total,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "manual": self.manual,
        }
