"""Persist source candidates, then hand them to the existing scheduler."""
from datetime import datetime, timezone
from proactive_scheduler import run_candidate
from proactive_store_interface import ProactiveStoreProtocol
from event_source import EventSource, utc_iso


def scan_manual_events(source: EventSource, store: ProactiveStoreProtocol, now=None):
    stamp = utc_iso(now or datetime.now(timezone.utc))
    return store.materialize_due_candidates(source.collect_due(stamp), stamp)


def dispatch_candidates(store: ProactiveStoreProtocol, now=None, policy=None, simulate=True, limit=100):
    now = now or datetime.now(timezone.utc)
    results = []
    for item in store.list_ready_candidates(utc_iso(now), limit):
        candidate = {"device_id": item["device_id"], "event_id": item["dedupe_key"],
                     "topic": f'manual:{item["event_id"]}', "title": item["title"],
                     "message": item["message"], "expires_at": item["expires_at"],
                     "candidate_id": item["id"]}
        result = run_candidate(store, candidate, policy, now, simulate)
        results.append({"candidate_id": item["id"], **result})
    return results
