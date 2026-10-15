"""Deterministic gates for opt-in proactive notifications."""
from datetime import datetime, time, timedelta, timezone


def policy_timezone(policy):
    # v1 supports the project's fixed China time zone without an OS tzdata dependency.
    if policy.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("unsupported_timezone")
    return timezone(timedelta(hours=8))


def decide(candidate, policy, recent, now=None):
    now = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(candidate["expires_at"].replace("Z", "+00:00"))
    if expiry <= now:
        return "skip", "expired"
    if not candidate.get("event_id") or not candidate.get("topic") or not candidate.get("message"):
        return "skip", "invalid_candidate"
    if not policy.get("enabled", False):
        return "skip", "disabled"
    local = now.astimezone(policy_timezone(policy))
    start = time.fromisoformat(policy.get("quiet_start", "23:30"))
    end = time.fromisoformat(policy.get("quiet_end", "08:00"))
    quiet = start <= local.time() < end if start < end else local.time() >= start or local.time() < end
    if quiet:
        return "defer", "quiet_hours"
    if recent["today_count"] >= int(policy.get("daily_max", 2)):
        return "defer", "daily_limit"
    last = recent.get("last_sent_at")
    if last and (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < int(policy.get("cooldown_minutes", 240)) * 60:
        return "defer", "cooldown"
    if recent.get("topic_repeated"):
        return "skip", "duplicate_topic"
    return "send", "eligible"
