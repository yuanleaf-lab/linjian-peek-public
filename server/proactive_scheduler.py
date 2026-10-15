"""One-shot scheduler: a caller supplies event candidates; no timer or model."""
from datetime import datetime, timezone
from proactive_rules import decide, policy_timezone
from proactive_store_interface import ProactiveStoreProtocol


DEFAULT_POLICY = {"enabled": False, "timezone": "Asia/Shanghai", "quiet_start": "23:30", "quiet_end": "08:00", "daily_max": 2, "cooldown_minutes": 240}


def run_candidate(store: ProactiveStoreProtocol, candidate, policy=None, now=None, simulate=True):
    now = now or datetime.now(timezone.utc)
    stored_policy = store.get_policy(candidate['device_id'])
    policy = {**DEFAULT_POLICY, **(policy or {}), **(stored_policy or {})}
    stamp = now.isoformat().replace("+00:00", "Z")
    local = now.astimezone(policy_timezone(policy))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if simulate:
        recent = store.recent(candidate["device_id"], candidate["topic"], day_start, stamp)
        decision, reason = decide(candidate, policy, recent, now)
        return {"decision": decision, "reason": reason, "simulated": True}
    return store.schedule(candidate, day_start, stamp, int(policy["daily_max"]), lambda recent: decide(candidate, policy, recent, now))
