"""Authoritative flat roster for Nest5."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FlatProfile:
    number: str
    owner_name: str
    owner_notes: str | None = None


FLAT_ROSTER: tuple[FlatProfile, ...] = (
    FlatProfile("101", "Manikyamba", "Tenant"),
    FlatProfile("102", "Sastry"),
    FlatProfile("301", "Venkata Giridhar"),
    FlatProfile("401", "Manikyamba"),
    FlatProfile("501", "Pavan Kumar"),
)

FLAT_NUMBERS: list[str] = [p.number for p in FLAT_ROSTER]
