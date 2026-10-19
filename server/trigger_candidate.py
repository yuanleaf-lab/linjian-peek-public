"""Persist source candidates, then hand them to the existing scheduler."""
from datetime import datetime, timezone
from proactive_scheduler import run_candidate
from proactive_store_interface import ProactiveStoreProtocol
from event_source import EventSource, utc_iso


def manual_event_candidate(event: dict) -> dict:
    return {
        "device_id": event["device_id"],
        "event_id": f'manual:{event["id"]}:{event["revision"]}',
        "topic": f'manual:{event["id"]}',
        "title": event["title"],
        "message": event["message"],
        "expires_at": event["expires_at"],
    }


def scan_manual_events(source: EventSource, store: ProactiveStoreProtocol, now=None):
    stamp = utc_iso(now or datetime.now(timezone.utc))
    return store.materialize_due_candidates(source.collect_due(stamp), stamp)


def preview_manual_events(source: EventSource, store: ProactiveStoreProtocol, now=None, policy=None, limit=100):
    """Read due source events and evaluate them without materializing candidates."""
    now = now or datetime.now(timezone.utc)
    results = []
    for event in source.collect_due(utc_iso(now), limit):
        result = run_candidate(store, manual_event_candidate(event), policy, now, simulate=True)
        results.append({"event_id": event["id"], **result})
    return results


def dispatch_candidates(store: ProactiveStoreProtocol, now=None, policy=None, simulate=True, limit=100):
    now = now or datetime.now(timezone.utc)
    results = []
    for item in store.list_ready_candidates(utc_iso(now), limit):
        candidate = manual_event_candidate({
            "id": item["event_id"], "revision": item["event_revision"],
            "device_id": item["device_id"], "title": item["title"],
            "message": item["message"], "expires_at": item["expires_at"],
        }) | {"event_id": item["dedupe_key"], "candidate_id": item["id"]}
        result = run_candidate(store, candidate, policy, now, simulate)
        results.append({"candidate_id": item["id"], **result})
    return results
