"""Real PostgreSQL tests. Set LINJIAN_PG_INTEGRATION_URL to a local disposable DB.

The URL must name a database beginning with proactive_test and use loopback.
No mock connection is used in this module.
"""
import os
import json
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from postgres_migrate import migrate
from postgres_proactive_store import PostgresProactiveStore
from proactive_store import ProactiveStore
from trigger_candidate import dispatch_candidates
from linjian_server import Handler, State


def _local_test_url():
    url = os.environ.get('LINJIAN_PG_INTEGRATION_URL', '')
    parsed = urlparse(url)
    if not url:
        raise unittest.SkipTest('LINJIAN_PG_INTEGRATION_URL is not set')
    if parsed.scheme not in ('postgres', 'postgresql') or parsed.hostname not in ('localhost', '127.0.0.1') or not parsed.path.lstrip('/').startswith('proactive_test'):
        raise RuntimeError('integration_url_must_be_loopback_proactive_test_database')
    return url


class RealPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = _local_test_url()
        try:
            import psycopg
            from psycopg import sql
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise unittest.SkipTest('psycopg is not installed') from exc
        cls.psycopg, cls.sql, cls.dict_row = psycopg, sql, dict_row

    def setUp(self):
        self.schema = 'test_' + uuid.uuid4().hex
        with self.psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(self.sql.SQL('CREATE SCHEMA {}').format(self.sql.Identifier(self.schema)))

        def connect(url):
            return self.psycopg.connect(url, row_factory=type(self).dict_row,
                                        options=f'-c search_path={self.schema}', connect_timeout=5)

        self.connect = connect
        self.store = PostgresProactiveStore(self.url, connection_factory=connect)
        self.now = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.stamp = self.now.isoformat().replace('+00:00', 'Z')

    def tearDown(self):
        with self.psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(self.sql.SQL('DROP SCHEMA {} CASCADE').format(self.sql.Identifier(self.schema)))

    def _candidate(self, key):
        return {'device_id': 'phone', 'event_id': key, 'topic': key, 'title': '提醒',
                'message': '测试内容', 'expires_at': (self.now + timedelta(days=2)).isoformat()}

    def _event(self):
        return {'device_id': 'phone', 'title': '明确事项', 'message': '测试内容',
                'due_at': (self.now - timedelta(minutes=1)).isoformat(),
                'expires_at': (self.now + timedelta(days=1)).isoformat()}

    def test_migration_empty_repeat_and_failed_version_rollback(self):
        self.assertEqual(migrate(self.url, self.connect), [1])
        self.assertEqual(migrate(self.url, self.connect), [])
        with self.connect(self.url) as conn:
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations').fetchall(), [{'version': 1}])
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            Path(temp, '001_first.sql').write_text('CREATE TABLE IF NOT EXISTS harmless(id integer);')
            Path(temp, '002_fail.sql').write_text('CREATE TABLE rollback_probe(id integer); SELECT 1/0;')
            with self.assertRaises(Exception):
                migrate(self.url, self.connect, temp)
        with self.connect(self.url) as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('rollback_probe') AS name").fetchone()['name'])
            self.assertEqual(conn.execute('SELECT version FROM schema_migrations').fetchall(), [{'version': 1}])

    def test_manual_candidates_policy_and_revision(self):
        migrate(self.url, self.connect)
        first = self.store.set_policy('phone', {'enabled': True, 'daily_max': 2}, self.stamp)
        self.assertEqual(first['version'], 1)
        self.assertEqual(self.store.set_policy('phone', {'daily_max': 1}, self.stamp)['version'], 2)
        event = self.store.create_manual_event(self._event(), self.stamp)
        due = self.store.list_due_manual_events(self.stamp)
        self.assertEqual(len(due), 1)
        self.assertEqual(self.store.materialize_due_candidates(due, self.stamp), 1)
        self.assertEqual(self.store.materialize_due_candidates(due, self.stamp), 0)
        self.assertEqual(len(self.store.list_ready_candidates(self.stamp)), 1)
        result = dispatch_candidates(self.store, self.now, {'enabled': True, 'daily_max': 2, 'cooldown_minutes': 0}, simulate=False)
        self.assertEqual(result[0]['decision'], 'send')
        self.assertEqual(self.store.list_ready_candidates(self.stamp), [])
        self.assertEqual(self.store.get('phone', f"manual:{event['id']}:1", f"manual:{event['id']}")['status'], 'queued')
        changed = self.store.update_manual_event(event['id'], dict(self._event(), message='修改后内容'), self.stamp)
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(self.store.get('phone', f"manual:{event['id']}:1", f"manual:{event['id']}")['status'], 'expired')
        self.assertEqual(self.store.list_ready_candidates(self.stamp), [])
        self.assertEqual(self.store.materialize_due_candidates(self.store.list_due_manual_events(self.stamp), self.stamp), 1)
        self.assertEqual(self.store.cancel_manual_event(event['id'], self.stamp)['status'], 'cancelled')
        self.assertEqual(self.store.list_ready_candidates(self.stamp), [])

    def test_sqlite_postgres_message_and_attempt_parity(self):
        migrate(self.url, self.connect)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            sqlite = ProactiveStore(Path(temp) / 'proactive.sqlite3')
            for store in (sqlite, self.store):
                candidate = self._candidate('parity')
                first = store.schedule(candidate, self.stamp, self.stamp, 2, lambda _: ('send', 'eligible'))
                self.assertEqual((first['decision'], first['status']), ('send', 'queued'))
                self.assertEqual(store.schedule(candidate, self.stamp, self.stamp, 2, lambda _: ('send', 'eligible'))['reason'], 'idempotent_duplicate')
                self.assertIsNotNone(store.claim('phone', 'cmd-parity', self.stamp))
                self.assertEqual(store.report('cmd-parity', True, self.stamp), 1)
                self.assertEqual(store.report('cmd-parity', True, self.stamp), 0)
                self.assertEqual(store.get('phone', 'parity', 'parity')['status'], 'completed')
                self.assertEqual(store.list_attempts(first['message_id'])[0]['status'], 'completed')

    def test_concurrent_quota_and_claim(self):
        migrate(self.url, self.connect)
        def schedule(index):
            return self.store.schedule(self._candidate(str(index)), self.stamp, self.stamp, 2,
                                       lambda recent: ('defer', 'daily_limit') if recent['today_count'] >= 2 else ('send', 'eligible'))
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(schedule, range(8)))
        self.assertEqual(sum(result['decision'] == 'send' for result in decisions), 2)
        self.assertEqual(sum(result['decision'] == 'defer' for result in decisions), 6)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(lambda index: self.store.claim('phone', f'cmd-{index}', self.stamp), range(8)))
        self.assertEqual(sum(row is not None for row in claimed), 2)
        self.assertEqual(len({row['id'] for row in claimed if row}), 2)

    def test_same_event_materialized_once_under_concurrency(self):
        migrate(self.url, self.connect)
        event = self.store.create_manual_event(self._event(), self.stamp)
        with ThreadPoolExecutor(max_workers=6) as pool:
            counts = list(pool.map(lambda _: self.store.materialize_due_candidates([event], self.stamp), range(6)))
        self.assertEqual(sum(counts), 1)
        self.assertEqual(len(self.store.list_candidates()), 1)

    def test_restart_timeout_unknown_and_late_report(self):
        migrate(self.url, self.connect)
        self.store.schedule(self._candidate('restart'), self.stamp, self.stamp, 2, lambda _: ('send', 'eligible'))
        self.store.claim('phone', 'cmd-restart', self.stamp)
        reopened = PostgresProactiveStore(self.url, connection_factory=self.connect)
        later = (self.now + timedelta(minutes=6)).isoformat()
        self.assertEqual(reopened.recover_timeouts(later), 1)
        self.assertEqual(reopened.get('phone', 'restart', 'restart')['last_error_code'], 'dispatch_outcome_unknown')
        self.assertIsNone(reopened.claim('phone', 'cmd-again', later))
        self.assertEqual(reopened.report('cmd-restart', True, later), 1)
        self.assertEqual(reopened.get('phone', 'restart', 'restart')['status'], 'completed')

    def test_readiness_requires_migration_and_complete_schema(self):
        self.assertEqual(self.store.readiness()['error'], 'migration_not_applied')
        migrate(self.url, self.connect)
        self.assertEqual(self.store.readiness(), {'ready': True, 'status': 'ready'})
        with self.connect(self.url) as conn:
            conn.execute('INSERT INTO schema_migrations(version) VALUES(2)')
        self.assertEqual(self.store.readiness()['error'], 'schema_version_mismatch')
        with self.connect(self.url) as conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=2')
            conn.execute('DROP INDEX manual_events_due')
        self.assertEqual(self.store.readiness()['error'], 'schema_incomplete')

    def test_unreachable_postgres_preserves_ordinary_command(self):
        parsed = urlparse(self.url)
        unavailable_url = urlunparse(parsed._replace(netloc=f'{parsed.username}:{parsed.password}@127.0.0.1:1'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch.dict(os.environ, {
            'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres',
            'DATABASE_URL': unavailable_url, 'LINJIAN_TOKEN': 'device-test-token',
            'LINJIAN_PROACTIVE_SEND_ENABLED': 'true'}):
            Handler.state = State()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                def post(path, body):
                    request = Request(base + path, json.dumps(body).encode(),
                                      {'X-Auth-Token': 'device-test-token', 'Content-Type': 'application/json'})
                    return json.load(urlopen(request))
                command = post('/api/command', {'device_id': 'phone', 'action': 'noop'})['command']
                request = Request(base + '/api/poll?device_id=phone', headers={'X-Auth-Token': 'device-test-token'})
                self.assertEqual(json.load(urlopen(request))['command']['id'], command['id'])
                self.assertTrue(post('/api/device/report', {'command_id': command['id'], 'ok': True})['ok'])
                self.assertIsNone(json.load(urlopen(request))['command'])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
