"""PostgreSQL adapter. Each operation commits or rolls back as one transaction."""
import uuid
from datetime import datetime, time, timedelta, timezone

from proactive_store import DISPATCH_TIMEOUT_SECONDS, MAX_ATTEMPTS, RETRYABLE_ERRORS, RETRY_DELAY_SECONDS, iso
from proactive_store_interface import ProactiveStoreProtocol


class PostgresStoreNotReady(RuntimeError):
    pass


def _default_connection_factory(url):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresStoreNotReady('postgres_driver_unavailable') from exc
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5,
                           options='-c statement_timeout=3000 -c lock_timeout=3000')


def _value(value):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, time):
        return value.strftime('%H:%M')
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _one(cur):
    row = cur.fetchone()
    return {key: _value(value) for key, value in row.items()} if row else None


def _all(cur):
    return [{key: _value(value) for key, value in row.items()} for row in cur.fetchall()]


class PostgresProactiveStore:
    REQUIRED_TABLES = {'schema_migrations', 'manual_events', 'trigger_candidates',
                       'proactive_messages', 'proactive_policy', 'dispatch_attempts'}
    REQUIRED_INDEXES = {'manual_events_due', 'proactive_messages_claim',
                        'proactive_messages_quota', 'proactive_messages_timeout',
                        'trigger_candidates_ready', 'dispatch_attempts_message'}
    REQUIRED_CONSTRAINTS = {
        'manual_events': {'manual_events_pkey', 'manual_events_check', 'manual_events_status_check'},
        'trigger_candidates': {'trigger_candidates_pkey', 'trigger_candidates_event_id_fkey',
                               'trigger_candidates_message_id_fkey', 'trigger_candidates_device_id_dedupe_key_key',
                               'trigger_candidates_status_check', 'trigger_candidates_check'},
        'proactive_messages': {'proactive_messages_pkey', 'proactive_messages_device_id_event_id_topic_key',
                               'proactive_messages_command_id_key', 'proactive_messages_status_check'},
        'proactive_policy': {'proactive_policy_pkey', 'proactive_policy_daily_max_check'},
        'dispatch_attempts': {'dispatch_attempts_pkey', 'dispatch_attempts_message_id_fkey',
                              'dispatch_attempts_message_id_attempt_number_key', 'dispatch_attempts_status_check'},
    }
    def __init__(self, database_url, connection_factory=None):
        if not database_url or not database_url.startswith(('postgresql://', 'postgres://')):
            raise ValueError('postgres_database_url_required')
        self.database_url = database_url
        self.connection_factory = connection_factory or _default_connection_factory

    def _connect(self):
        return self.connection_factory(self.database_url)

    def readiness(self):
        """Read-only schema preflight; returns only safe, fixed error categories."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute('SELECT 1')
                for table in self.REQUIRED_TABLES:
                    cur.execute('SELECT to_regclass(%s) AS name', (table,))
                    if cur.fetchone()['name'] is None:
                        return {'ready': False, 'status': 'unavailable', 'error': 'migration_not_applied'}
                cur.execute('SELECT version FROM schema_migrations ORDER BY version')
                if [row['version'] for row in cur.fetchall()] != [1]:
                    return {'ready': False, 'status': 'unavailable', 'error': 'schema_version_mismatch'}
                cur.execute('SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()')
                if not self.REQUIRED_INDEXES <= {row['indexname'] for row in cur.fetchall()}:
                    return {'ready': False, 'status': 'unavailable', 'error': 'schema_incomplete'}
                for table, expected in self.REQUIRED_CONSTRAINTS.items():
                    cur.execute('SELECT conname FROM pg_constraint WHERE conrelid=%s::regclass', (table,))
                    if not expected <= {row['conname'] for row in cur.fetchall()}:
                        return {'ready': False, 'status': 'unavailable', 'error': 'schema_incomplete'}
        except Exception:
            return {'ready': False, 'status': 'unavailable', 'error': 'database_unavailable'}
        return {'ready': True, 'status': 'ready'}

    def get(self, device_id, event_id, topic):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM proactive_messages WHERE device_id=%s AND event_id=%s AND topic=%s', (device_id, event_id, topic))
            return _one(cur)

    def get_policy(self, device_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM proactive_policy WHERE device_id=%s', (device_id,))
            return _one(cur)

    def set_policy(self, device_id, changes, now):
        allowed = {'enabled', 'timezone', 'quiet_start', 'quiet_end', 'daily_max', 'cooldown_minutes'}
        if not isinstance(device_id, str) or not 0 < len(device_id) <= 100 or not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError('invalid_policy')
        if 'enabled' in changes and not isinstance(changes['enabled'], bool):
            raise ValueError('invalid_policy')
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('INSERT INTO proactive_policy(device_id,updated_at) VALUES(%s,%s) ON CONFLICT(device_id) DO NOTHING RETURNING device_id', (device_id, now))
            created = bool(cur.fetchone())
            cur.execute('SELECT * FROM proactive_policy WHERE device_id=%s FOR UPDATE', (device_id,))
            row = _one(cur)
            row.update(changes)
            if row['timezone'] != 'Asia/Shanghai' or type(row['daily_max']) is not int or not 0 <= row['daily_max'] <= 20 or type(row['cooldown_minutes']) is not int or not 0 <= row['cooldown_minutes'] <= 1440:
                raise ValueError('invalid_policy')
            for key in ('quiet_start', 'quiet_end'):
                try:
                    datetime.strptime(row[key], '%H:%M')
                except (TypeError, ValueError) as exc:
                    raise ValueError('invalid_policy') from exc
            cur.execute('UPDATE proactive_policy SET enabled=%s,timezone=%s,quiet_start=%s,quiet_end=%s,daily_max=%s,cooldown_minutes=%s,version=version+%s,updated_at=%s WHERE device_id=%s',
                        (row['enabled'], row['timezone'], row['quiet_start'], row['quiet_end'], row['daily_max'], row['cooldown_minutes'], 0 if created else 1, now, device_id))
        return self.get_policy(device_id)

    def list_attempts(self, message_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM dispatch_attempts WHERE message_id=%s ORDER BY attempt_number', (message_id,))
            return _all(cur)

    def create_manual_event(self, data, now):
        event_id = str(uuid.uuid4())
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO manual_events(id,device_id,title,message,due_at,expires_at,status,revision,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,'active',1,%s,%s)",
                        (event_id, data['device_id'], data['title'], data['message'], data['due_at'], data['expires_at'], now, now))
        return self.get_manual_event(event_id)

    def get_manual_event(self, event_id):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM manual_events WHERE id=%s', (event_id,))
            return _one(cur)

    def list_due_manual_events(self, now, limit=100):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM manual_events WHERE status='active' AND due_at<=%s AND expires_at>%s ORDER BY due_at,id LIMIT %s", (now, now, limit))
            return _all(cur)

    @staticmethod
    def _invalidate_event(cur, event_id, now):
        cur.execute("UPDATE trigger_candidates SET status='cancelled',reason='source_changed',updated_at=%s WHERE event_id=%s AND status IN ('pending','deferred')", (now, event_id))
        cur.execute("UPDATE proactive_messages SET status='expired',reason='source_changed',version=version+1 WHERE status='queued' AND event_id IN (SELECT dedupe_key FROM trigger_candidates WHERE event_id=%s)", (event_id,))

    def update_manual_event(self, event_id, data, now):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM manual_events WHERE id=%s AND status='active' FOR UPDATE", (event_id,))
            if not cur.fetchone():
                raise ValueError('event_not_found')
            self._invalidate_event(cur, event_id, now)
            cur.execute('UPDATE manual_events SET title=%s,message=%s,due_at=%s,expires_at=%s,revision=revision+1,updated_at=%s WHERE id=%s',
                        (data['title'], data['message'], data['due_at'], data['expires_at'], now, event_id))
        return self.get_manual_event(event_id)

    def cancel_manual_event(self, event_id, now):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM manual_events WHERE id=%s AND status='active' FOR UPDATE", (event_id,))
            if not cur.fetchone():
                return None
            self._invalidate_event(cur, event_id, now)
            cur.execute("UPDATE manual_events SET status='cancelled',updated_at=%s WHERE id=%s", (now, event_id))
        return self.get_manual_event(event_id)

    def materialize_due_candidates(self, events, now):
        created = 0
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE trigger_candidates SET status='expired',reason='expired',updated_at=%s WHERE status IN ('pending','deferred') AND expires_at<=%s", (now, now))
            for event in events:
                cur.execute("SELECT * FROM manual_events WHERE id=%s AND revision=%s AND status='active' AND due_at<=%s AND expires_at>%s FOR UPDATE", (event['id'], event['revision'], now, now))
                row = _one(cur)
                if row:
                    key = f"manual:{row['id']}:{row['revision']}"
                    cur.execute("INSERT INTO trigger_candidates(event_id,event_revision,device_id,dedupe_key,title,message,eligible_at,expires_at,status,reason,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending','due',%s,%s) ON CONFLICT(device_id,dedupe_key) DO NOTHING",
                                (row['id'], row['revision'], row['device_id'], key, row['title'], row['message'], row['due_at'], row['expires_at'], now, now))
                    created += cur.rowcount
        return created

    def list_candidates(self):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM trigger_candidates ORDER BY id')
            return _all(cur)

    def list_ready_candidates(self, now, limit=100):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT c.* FROM trigger_candidates c JOIN manual_events e ON e.id=c.event_id WHERE c.status IN ('pending','deferred') AND c.eligible_at<=%s AND c.expires_at>%s AND e.status='active' AND e.revision=c.event_revision ORDER BY c.eligible_at,c.id LIMIT %s", (now, now, limit))
            return _all(cur)

    @staticmethod
    def _recent(cur, device_id, topic, day_start, now):
        active = "('queued','dispatched','completed','failed')"
        cur.execute(f'SELECT COUNT(*) AS count FROM proactive_messages WHERE device_id=%s AND status IN {active} AND created_at>=%s AND created_at<=%s', (device_id, day_start, now))
        count = _one(cur)['count']
        cur.execute(f'SELECT created_at FROM proactive_messages WHERE device_id=%s AND status IN {active} ORDER BY created_at DESC LIMIT 1', (device_id,))
        last = _one(cur)
        cur.execute(f'SELECT 1 AS repeated FROM proactive_messages WHERE device_id=%s AND topic=%s AND status IN {active} AND created_at>=%s LIMIT 1', (device_id, topic, day_start))
        return {'today_count': count, 'last_sent_at': last['created_at'] if last else None, 'topic_repeated': bool(_one(cur))}

    def recent(self, device_id, topic, day_start, now):
        with self._connect() as conn, conn.cursor() as cur:
            return self._recent(cur, device_id, topic, day_start, now)

    def schedule(self, candidate, day_start, now, daily_max, evaluate):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (candidate['device_id'],))
            if candidate.get('candidate_id') is not None:
                cur.execute("SELECT c.id FROM trigger_candidates c JOIN manual_events e ON e.id=c.event_id WHERE c.id=%s AND c.status IN ('pending','deferred') AND e.status='active' AND e.revision=c.event_revision AND c.dedupe_key=%s AND c.expires_at>%s FOR UPDATE OF c,e", (candidate['candidate_id'], candidate['event_id'], now))
                if not cur.fetchone():
                    return {'decision': 'skip', 'reason': 'source_changed', 'status': 'cancelled'}
            key = (candidate['device_id'], candidate['event_id'], candidate['topic'])
            cur.execute('SELECT * FROM proactive_messages WHERE device_id=%s AND event_id=%s AND topic=%s FOR UPDATE', key)
            existing = _one(cur)
            recent = self._recent(cur, key[0], key[2], day_start, now)
            if existing and existing['status'] == 'failed' and existing['last_error_code'] in RETRYABLE_ERRORS and existing['attempt_count'] < MAX_ATTEMPTS:
                if existing['expires_at'] <= now:
                    cur.execute("UPDATE proactive_messages SET status='expired',reason='expired',version=version+1 WHERE id=%s", (existing['id'],))
                    return {'decision': 'skip', 'reason': 'expired', 'message_id': existing['id'], 'status': 'expired'}
                if existing['next_attempt_at'] and existing['next_attempt_at'] > now:
                    return {'decision': 'defer', 'reason': 'retry_backoff', 'message_id': existing['id'], 'status': 'failed'}
                if recent['today_count'] >= daily_max:
                    return {'decision': 'skip', 'reason': 'daily_limit', 'message_id': existing['id'], 'status': 'failed'}
                cur.execute("UPDATE proactive_messages SET status='queued',reason='retry',command_id=NULL,dispatch_deadline=NULL,next_attempt_at=NULL,version=version+1 WHERE id=%s", (existing['id'],))
                if candidate.get('candidate_id') is not None:
                    cur.execute("UPDATE trigger_candidates SET status='consumed',reason='retry',message_id=%s,updated_at=%s WHERE id=%s", (existing['id'], now, candidate['candidate_id']))
                return {'decision': 'send', 'reason': 'retry', 'message_id': existing['id'], 'status': 'queued'}
            if existing and existing['status'] != 'deferred':
                return {'decision': 'skip', 'reason': 'idempotent_duplicate', 'message_id': existing['id'], 'status': existing['status']}
            decision, reason = evaluate(recent)
            status = {'send': 'queued', 'defer': 'deferred', 'skip': 'skipped'}[decision]
            if existing:
                cur.execute('UPDATE proactive_messages SET status=%s,reason=%s,version=version+1 WHERE id=%s', (status, reason, existing['id']))
                message_id = existing['id']
            else:
                cur.execute('INSERT INTO proactive_messages(device_id,event_id,topic,title,message,expires_at,created_at,status,reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id', (*key, candidate.get('title', '掌心窗'), candidate['message'], iso(candidate['expires_at']), now, status, reason))
                message_id = _one(cur)['id']
            if candidate.get('candidate_id') is not None:
                candidate_status = 'consumed' if decision == 'send' else 'deferred' if decision == 'defer' else 'cancelled'
                cur.execute('UPDATE trigger_candidates SET status=%s,reason=%s,message_id=%s,updated_at=%s WHERE id=%s', (candidate_status, reason, message_id, now, candidate['candidate_id']))
            return {'decision': decision, 'reason': reason, 'message_id': message_id, 'status': status}

    def expire(self, now):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE proactive_messages SET status='expired',reason='expired',version=version+1 WHERE status IN ('queued','deferred') AND expires_at<=%s", (now,))
            return cur.rowcount

    def recover_timeouts(self, now):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE proactive_messages SET status='failed',reason='dispatch_outcome_unknown',last_error_code='dispatch_outcome_unknown',completed_at=%s,version=version+1 WHERE status='dispatched' AND dispatch_deadline<=%s RETURNING command_id", (now, now))
            ids = [row['command_id'] for row in cur.fetchall()]
            for command_id in ids:
                cur.execute("UPDATE dispatch_attempts SET status='unknown',error_code='dispatch_outcome_unknown',reported_at=%s WHERE command_id=%s", (now, command_id))
            return len(ids)

    def claim(self, device_id, command_id, now):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM proactive_messages WHERE device_id=%s AND status='queued' AND expires_at>%s ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED", (device_id, now))
            row = _one(cur)
            if not row:
                return None
            deadline = iso(datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(seconds=DISPATCH_TIMEOUT_SECONDS))
            cur.execute("UPDATE proactive_messages SET status='dispatched',command_id=%s,attempt_count=attempt_count+1,dispatch_deadline=%s,version=version+1 WHERE id=%s", (command_id, deadline, row['id']))
            cur.execute("INSERT INTO dispatch_attempts(command_id,message_id,attempt_number,status,dispatched_at,dispatch_deadline) VALUES(%s,%s,%s,'dispatched',%s,%s)", (command_id, row['id'], row['attempt_count'] + 1, now, deadline))
            return row

    def report(self, command_id, ok, now, error_code=''):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM proactive_messages WHERE command_id=%s FOR UPDATE', (command_id,))
            row = _one(cur)
            if not row or row['status'] not in ('dispatched', 'failed') or row['status'] == 'failed' and row['last_error_code'] != 'dispatch_outcome_unknown':
                return 0
            code = '' if ok else (error_code if error_code in RETRYABLE_ERRORS else 'device_reported_failure')
            retry_at = iso(datetime.fromisoformat(now.replace('Z', '+00:00')) + timedelta(seconds=RETRY_DELAY_SECONDS)) if code in RETRYABLE_ERRORS and row['attempt_count'] < MAX_ATTEMPTS else None
            cur.execute('UPDATE proactive_messages SET status=%s,reason=%s,last_error_code=%s,next_attempt_at=%s,dispatch_deadline=NULL,completed_at=%s,version=version+1 WHERE id=%s',
                        ('completed' if ok else 'failed', 'device_report' if ok else code, code or None, retry_at, now, row['id']))
            count = cur.rowcount
            cur.execute('UPDATE dispatch_attempts SET status=%s,reported_at=%s,error_code=%s WHERE command_id=%s', ('completed' if ok else 'failed', now, code or None, command_id))
            return count
