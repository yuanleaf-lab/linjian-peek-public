"""SQLite adapter for the proactive persistence contract."""
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from proactive_store_interface import ProactiveStoreProtocol

SCHEMA_VERSION = 4
MAX_ATTEMPTS = 2
RETRYABLE_ERRORS = {"temporary_unavailable", "notification_service_busy"}
DISPATCH_TIMEOUT_SECONDS = 300
RETRY_DELAY_SECONDS = 300


def iso(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ProactiveStore:
    """A PostgreSQL implementation can satisfy the same protocol."""
    def __init__(self, path):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("proactive_schema_newer_than_code")
            if version == 0:
                db.execute("""CREATE TABLE IF NOT EXISTS proactive_messages (
                    id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    topic TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL,
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, command_id TEXT UNIQUE,
                    completed_at TEXT, UNIQUE(device_id,event_id,topic))""")
                version = 1
            if version == 1:
                columns = {row[1] for row in db.execute("PRAGMA table_info(proactive_messages)")}
                additions = {"attempt_count": "INTEGER NOT NULL DEFAULT 0", "dispatch_deadline": "TEXT", "last_error_code": "TEXT", "next_attempt_at": "TEXT", "version": "INTEGER NOT NULL DEFAULT 1"}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE proactive_messages ADD COLUMN {name} {definition}")
                # Old dispatched rows cannot prove delivery after a restart.
                db.execute("UPDATE proactive_messages SET attempt_count=1,dispatch_deadline=? WHERE status='dispatched' AND dispatch_deadline IS NULL", (iso(datetime.now(timezone.utc)),))
                for row in db.execute("SELECT id,expires_at FROM proactive_messages").fetchall():
                    db.execute("UPDATE proactive_messages SET expires_at=? WHERE id=?", (iso(row["expires_at"]), row["id"]))
                db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                db.execute("""CREATE TABLE IF NOT EXISTS manual_events (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, title TEXT NOT NULL,
                    message TEXT NOT NULL, due_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS trigger_candidates (
                    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
                    device_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, title TEXT NOT NULL,
                    message TEXT NOT NULL, eligible_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL, message_id INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(device_id,dedupe_key))""")
                db.execute("CREATE INDEX IF NOT EXISTS manual_due ON manual_events(status,due_at,expires_at)")
                db.execute("CREATE INDEX IF NOT EXISTS candidate_ready ON trigger_candidates(status,eligible_at,expires_at)")
                db.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                db.execute("""CREATE TABLE IF NOT EXISTS proactive_policy (
                    device_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', quiet_start TEXT NOT NULL DEFAULT '23:30',
                    quiet_end TEXT NOT NULL DEFAULT '08:00', daily_max INTEGER NOT NULL DEFAULT 2,
                    cooldown_minutes INTEGER NOT NULL DEFAULT 240, version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS dispatch_attempts (
                    command_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES proactive_messages(id),
                    attempt_number INTEGER NOT NULL, status TEXT NOT NULL, dispatched_at TEXT NOT NULL,
                    dispatch_deadline TEXT NOT NULL, reported_at TEXT, error_code TEXT,
                    UNIQUE(message_id, attempt_number))""")
                # Preserve the command identity of in-flight legacy messages.
                db.execute("""INSERT OR IGNORE INTO dispatch_attempts
                    (command_id,message_id,attempt_number,status,dispatched_at,dispatch_deadline,reported_at,error_code)
                    SELECT command_id,id,CASE WHEN attempt_count>0 THEN attempt_count ELSE 1 END,
                    CASE WHEN status='dispatched' THEN 'dispatched' WHEN status='completed' THEN 'completed' ELSE 'failed' END,
                    created_at,COALESCE(dispatch_deadline,created_at),completed_at,last_error_code
                    FROM proactive_messages WHERE command_id IS NOT NULL""")
                db.execute("CREATE INDEX IF NOT EXISTS attempts_message ON dispatch_attempts(message_id,attempt_number)")
                db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(4,?)", (iso(datetime.now(timezone.utc)),))
                db.execute("PRAGMA user_version=4")
            db.execute("CREATE INDEX IF NOT EXISTS proactive_pending ON proactive_messages(device_id,status,expires_at)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, device_id, event_id, topic):
        with self._db() as db:
            row = db.execute("SELECT * FROM proactive_messages WHERE device_id=? AND event_id=? AND topic=?", (device_id, event_id, topic)).fetchone()
            return dict(row) if row else None

    def get_policy(self, device_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM proactive_policy WHERE device_id=?', (device_id,)).fetchone()
            return dict(row) | {'enabled': bool(row['enabled'])} if row else None

    def set_policy(self, device_id, changes, now):
        allowed = {'enabled', 'timezone', 'quiet_start', 'quiet_end', 'daily_max', 'cooldown_minutes'}
        if not isinstance(device_id, str) or not 0 < len(device_id) <= 100 or not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError('invalid_policy')
        if 'enabled' in changes and not isinstance(changes['enabled'], bool):
            raise ValueError('invalid_policy')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO proactive_policy(device_id,version,updated_at) VALUES(?,0,?)", (device_id, now))
            row = dict(db.execute('SELECT * FROM proactive_policy WHERE device_id=?', (device_id,)).fetchone())
            row.update(changes)
            if row['timezone'] != 'Asia/Shanghai' or type(row['daily_max']) is not int or not 0 <= row['daily_max'] <= 20 or type(row['cooldown_minutes']) is not int or not 0 <= row['cooldown_minutes'] <= 1440:
                raise ValueError('invalid_policy')
            for key in ('quiet_start', 'quiet_end'):
                try:
                    datetime.strptime(row[key], '%H:%M')
                except (TypeError, ValueError) as exc:
                    raise ValueError('invalid_policy') from exc
            db.execute("""UPDATE proactive_policy SET enabled=?,timezone=?,quiet_start=?,quiet_end=?,daily_max=?,cooldown_minutes=?,version=version+1,updated_at=? WHERE device_id=?""",
                       (int(bool(row['enabled'])), row['timezone'], row['quiet_start'], row['quiet_end'], row['daily_max'], row['cooldown_minutes'], now, device_id))
        return self.get_policy(device_id)

    def list_attempts(self, message_id):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM dispatch_attempts WHERE message_id=? ORDER BY attempt_number', (message_id,))]

    def create_manual_event(self, data, now):
        event_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute("INSERT INTO manual_events VALUES(?,?,?,?,?,?,?,?,?,?)", (event_id, data['device_id'], data['title'], data['message'], data['due_at'], data['expires_at'], 'active', 1, now, now))
        return self.get_manual_event(event_id)

    def get_manual_event(self, event_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM manual_events WHERE id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    def list_due_manual_events(self, now, limit=100):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM manual_events WHERE status='active' AND due_at<=? AND expires_at>? ORDER BY due_at,id LIMIT ?", (now, now, limit))]

    @staticmethod
    def _invalidate_event(db, event_id, now):
        db.execute("UPDATE trigger_candidates SET status='cancelled',reason='source_changed',updated_at=? WHERE event_id=? AND status IN ('pending','deferred')", (now, event_id))
        # A queued notification must not outlive its source. Already dispatched is uncertain.
        db.execute("""UPDATE proactive_messages SET status='expired',reason='source_changed',version=version+1
                      WHERE status='queued' AND event_id IN
                      (SELECT dedupe_key FROM trigger_candidates WHERE event_id=?)""", (event_id,))

    def update_manual_event(self, event_id, data, now):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM manual_events WHERE id=? AND status='active'", (event_id,)).fetchone()
            if not row:
                raise ValueError('event_not_found')
            self._invalidate_event(db, event_id, now)
            db.execute("UPDATE manual_events SET title=?,message=?,due_at=?,expires_at=?,revision=revision+1,updated_at=? WHERE id=?", (data['title'], data['message'], data['due_at'], data['expires_at'], now, event_id))
        return self.get_manual_event(event_id)

    def cancel_manual_event(self, event_id, now):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM manual_events WHERE id=? AND status='active'", (event_id,)).fetchone()
            if not row:
                return None
            self._invalidate_event(db, event_id, now)
            db.execute("UPDATE manual_events SET status='cancelled',updated_at=? WHERE id=?", (now, event_id))
        return self.get_manual_event(event_id)

    def materialize_due_candidates(self, events, now):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE trigger_candidates SET status='expired',reason='expired',updated_at=? WHERE status IN ('pending','deferred') AND expires_at<=?", (now, now))
            created = 0
            for event in events:
                row = db.execute("SELECT * FROM manual_events WHERE id=? AND revision=? AND status='active' AND due_at<=? AND expires_at>?", (event['id'], event['revision'], now, now)).fetchone()
                if not row:
                    continue
                key = f'manual:{row["id"]}:{row["revision"]}'
                result = db.execute("""INSERT OR IGNORE INTO trigger_candidates
                    (event_id,event_revision,device_id,dedupe_key,title,message,eligible_at,expires_at,status,reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (row['id'], row['revision'], row['device_id'], key, row['title'], row['message'], row['due_at'], row['expires_at'], 'pending', 'due', now, now))
                created += result.rowcount
            return created

    def list_candidates(self):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM trigger_candidates ORDER BY id')]

    def list_ready_candidates(self, now, limit=100):
        with self._db() as db:
            return [dict(row) for row in db.execute("""SELECT c.* FROM trigger_candidates c JOIN manual_events e ON e.id=c.event_id
                WHERE c.status IN ('pending','deferred') AND c.eligible_at<=? AND c.expires_at>?
                AND e.status='active' AND e.revision=c.event_revision ORDER BY c.eligible_at,c.id LIMIT ?""", (now, now, limit))]

    @staticmethod
    def _recent(db, device_id, topic, day_start, now):
        active = "('queued','dispatched','completed','failed')"
        count = db.execute(f"SELECT COUNT(*) FROM proactive_messages WHERE device_id=? AND status IN {active} AND created_at>=? AND created_at<=?", (device_id, day_start, now)).fetchone()[0]
        last = db.execute(f"SELECT created_at FROM proactive_messages WHERE device_id=? AND status IN {active} ORDER BY created_at DESC LIMIT 1", (device_id,)).fetchone()
        repeat = db.execute(f"SELECT 1 FROM proactive_messages WHERE device_id=? AND topic=? AND status IN {active} AND created_at>=? LIMIT 1", (device_id, topic, day_start)).fetchone()
        return {"today_count": count, "last_sent_at": last[0] if last else None, "topic_repeated": bool(repeat)}

    def recent(self, device_id, topic, day_start, now):
        with self._db() as db:
            return self._recent(db, device_id, topic, day_start, now)

    def schedule(self, candidate, day_start, now, daily_max, evaluate):
        """Quota, cooldown and idempotency share one SQLite write transaction."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if candidate.get('candidate_id') is not None:
                valid = db.execute("""SELECT 1 FROM trigger_candidates c JOIN manual_events e ON e.id=c.event_id
                    WHERE c.id=? AND c.status IN ('pending','deferred') AND e.status='active'
                    AND e.revision=c.event_revision AND c.dedupe_key=? AND c.expires_at>?""",
                    (candidate['candidate_id'], candidate['event_id'], now)).fetchone()
                if not valid:
                    return {'decision': 'skip', 'reason': 'source_changed', 'status': 'cancelled'}
            key = (candidate["device_id"], candidate["event_id"], candidate["topic"])
            existing = db.execute("SELECT * FROM proactive_messages WHERE device_id=? AND event_id=? AND topic=?", key).fetchone()
            recent = self._recent(db, key[0], key[2], day_start, now)
            if existing and existing["status"] == "failed" and existing["last_error_code"] in RETRYABLE_ERRORS and existing["attempt_count"] < MAX_ATTEMPTS:
                if existing["expires_at"] <= now:
                    db.execute("UPDATE proactive_messages SET status='expired',reason='expired',version=version+1 WHERE id=?", (existing["id"],))
                    return {"decision": "skip", "reason": "expired", "message_id": existing["id"], "status": "expired"}
                if existing["next_attempt_at"] and existing["next_attempt_at"] > now:
                    return {"decision": "defer", "reason": "retry_backoff", "message_id": existing["id"], "status": "failed"}
                if recent["today_count"] >= daily_max:
                    return {"decision": "skip", "reason": "daily_limit", "message_id": existing["id"], "status": "failed"}
                db.execute("UPDATE proactive_messages SET status='queued',reason='retry',command_id=NULL,dispatch_deadline=NULL,next_attempt_at=NULL,version=version+1 WHERE id=?", (existing["id"],))
                db.execute("UPDATE trigger_candidates SET status='consumed',reason='retry',message_id=?,updated_at=? WHERE id=?", (existing['id'], now, candidate.get('candidate_id')))
                return {"decision": "send", "reason": "retry", "message_id": existing["id"], "status": "queued"}
            if existing and existing["status"] != "deferred":
                return {"decision": "skip", "reason": "idempotent_duplicate", "message_id": existing["id"], "status": existing["status"]}
            decision, reason = evaluate(recent)
            status = {"send": "queued", "defer": "deferred", "skip": "skipped"}[decision]
            if existing:
                db.execute("UPDATE proactive_messages SET status=?,reason=?,version=version+1 WHERE id=?", (status, reason, existing["id"]))
                message_id = existing["id"]
            else:
                cursor = db.execute("INSERT INTO proactive_messages(device_id,event_id,topic,title,message,expires_at,created_at,status,reason) VALUES(?,?,?,?,?,?,?,?,?)", (*key, candidate.get("title", "掌心窗"), candidate["message"], iso(candidate["expires_at"]), now, status, reason))
                message_id = cursor.lastrowid
            if candidate.get('candidate_id') is not None:
                candidate_status = 'consumed' if decision == 'send' else 'deferred' if decision == 'defer' else 'cancelled'
                db.execute("UPDATE trigger_candidates SET status=?,reason=?,message_id=?,updated_at=? WHERE id=?", (candidate_status, reason, message_id, now, candidate['candidate_id']))
            return {"decision": decision, "reason": reason, "message_id": message_id, "status": status}

    def expire(self, now):
        with self._db() as db:
            return db.execute("UPDATE proactive_messages SET status='expired',reason='expired',version=version+1 WHERE status IN ('queued','deferred') AND expires_at<=?", (now,)).rowcount

    def recover_timeouts(self, now):
        """Unknown delivery is terminal until a matching late device report arrives."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT command_id FROM proactive_messages WHERE status='dispatched' AND dispatch_deadline<=?", (now,)).fetchall()
            db.execute("UPDATE dispatch_attempts SET status='unknown',error_code='dispatch_outcome_unknown',reported_at=? WHERE command_id IN (SELECT command_id FROM proactive_messages WHERE status='dispatched' AND dispatch_deadline<=?)", (now, now))
            db.execute("UPDATE proactive_messages SET status='failed',reason='dispatch_outcome_unknown',last_error_code='dispatch_outcome_unknown',completed_at=?,version=version+1 WHERE status='dispatched' AND dispatch_deadline<=?", (now, now))
            return len(rows)

    def claim(self, device_id, command_id, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proactive_messages WHERE device_id=? AND status='queued' AND expires_at>? ORDER BY created_at LIMIT 1", (device_id, now)).fetchone()
            if not row:
                return None
            deadline = iso(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=DISPATCH_TIMEOUT_SECONDS))
            db.execute("UPDATE proactive_messages SET status='dispatched',command_id=?,attempt_count=attempt_count+1,dispatch_deadline=?,version=version+1 WHERE id=?", (command_id, deadline, row["id"]))
            db.execute("INSERT INTO dispatch_attempts(command_id,message_id,attempt_number,status,dispatched_at,dispatch_deadline) VALUES(?,?,?,?,?,?)", (command_id, row['id'], row['attempt_count'] + 1, 'dispatched', now, deadline))
            return dict(row)

    def report(self, command_id, ok, now, error_code=""):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proactive_messages WHERE command_id=?", (command_id,)).fetchone()
            if not row or row["status"] not in ("dispatched", "failed"):
                return 0
            if row["status"] == "failed" and row["last_error_code"] != "dispatch_outcome_unknown":
                return 0
            code = "" if ok else (error_code if error_code in RETRYABLE_ERRORS else "device_reported_failure")
            retry_at = iso(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=RETRY_DELAY_SECONDS)) if code in RETRYABLE_ERRORS and row["attempt_count"] < MAX_ATTEMPTS else None
            updated = db.execute("UPDATE proactive_messages SET status=?,reason=?,last_error_code=?,next_attempt_at=?,dispatch_deadline=NULL,completed_at=?,version=version+1 WHERE id=?", ("completed" if ok else "failed", "device_report" if ok else code, code or None, retry_at, now, row["id"])).rowcount
            db.execute("UPDATE dispatch_attempts SET status=?,reported_at=?,error_code=? WHERE command_id=?", ('completed' if ok else 'failed', now, code or None, command_id))
            return updated
