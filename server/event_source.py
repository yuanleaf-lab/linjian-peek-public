"""Contract for sources that produce facts, never send messages."""
from datetime import datetime, timezone
from typing import Protocol


def utc_iso(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


class EventSource(Protocol):
    def collect_due(self, now: str, limit: int = 100) -> list[dict]: ...
