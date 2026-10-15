"""User-authored, single-occurrence reminders."""
from datetime import datetime, timezone
from event_source import utc_iso
from proactive_store_interface import ProactiveStoreProtocol


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('invalid_event')
    for key, maximum in (("device_id", 100), ("title", 100), ("message", 1000)):
        if not isinstance(data.get(key), str) or not 0 < len(data[key].strip()) <= maximum:
            raise ValueError(f"invalid_{key}")
    try:
        due = datetime.fromisoformat(data["due_at"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError) as exc:
        raise ValueError('invalid_event_time') from exc
    if due.tzinfo is None or expiry.tzinfo is None or expiry <= due:
        raise ValueError("invalid_event_time")
    return {key: data[key].strip() for key in ("device_id", "title", "message")} | {"due_at": utc_iso(due), "expires_at": utc_iso(expiry)}


class ManualEventSource:
    def __init__(self, store: ProactiveStoreProtocol):
        self.store = store

    def create(self, data: dict, now=None):
        stamp = utc_iso(now or datetime.now(timezone.utc))
        return self.store.create_manual_event(validate(data), stamp)

    def update(self, event_id: str, changes: dict, now=None):
        if not isinstance(changes, dict):
            raise ValueError('invalid_changes')
        previous = self.store.get_manual_event(event_id)
        if not previous or previous["status"] != "active":
            raise ValueError("event_not_found")
        data = validate({key: changes.get(key, previous[key]) for key in ("device_id", "title", "message", "due_at", "expires_at")})
        if data["device_id"] != previous["device_id"]:
            raise ValueError("device_change_forbidden")
        return self.store.update_manual_event(event_id, data, utc_iso(now or datetime.now(timezone.utc)))

    def cancel(self, event_id: str, now=None):
        result = self.store.cancel_manual_event(event_id, utc_iso(now or datetime.now(timezone.utc)))
        if not result:
            raise ValueError("event_not_found")
        return result

    def collect_due(self, now: str, limit: int = 100) -> list[dict]:
        return self.store.list_due_manual_events(now, limit)
