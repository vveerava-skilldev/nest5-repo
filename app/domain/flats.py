"""Authoritative flat roster for Nest5."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FlatProfile:
    number: str
    owner_name: str
    owner_notes: str | None = None
    username: str | None = None  # login name; derived from the owner name when omitted


FLAT_ROSTER: tuple[FlatProfile, ...] = (
    FlatProfile("101", "Manikyamba", "Tenant"),
    FlatProfile("201", "A. Siva Rama Krishna Sastry", username="asrk.sastry"),
    FlatProfile("301", "Venkata Giridhar"),
    FlatProfile("401", "Manikyamba"),
    FlatProfile("501", "Pavan Kumar"),
)

# Numbers an older database may still use for a roster flat: {old: current}. Renumbered once, in place.
LEGACY_FLAT_NUMBERS: dict[str, str] = {"102": "201"}
# Owner names those older databases carried (replaced only if the admin never edited them).
LEGACY_OWNER_NAMES: dict[str, str] = {"201": "Sastry"}

FLAT_NUMBERS: list[str] = [p.number for p in FLAT_ROSTER]
