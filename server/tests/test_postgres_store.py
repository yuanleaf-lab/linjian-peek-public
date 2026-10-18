import sys
import json
import io
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from postgres_migrate import migrate
from postgres_proactive_store import PostgresProactiveStore, _connection_kwargs
from proactive_store import ProactiveStore
from linjian_server import Handler, State


class FakeConnection:
    def __init__(self, existing=None, fail_sql=False):
        self.existing = existing if existing is not None else set()
        self.fail_sql = fail_sql
        self.pending = None
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        if kind:
            self.rollbacks += 1
        else:
            if self.pending is not None:
                self.existing.add(self.pending)
            self.commits += 1

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith('INSERT INTO schema_migrations'):
            self.pending = params[0]
        elif self.fail_sql and 'CREATE TABLE' in sql:
            raise RuntimeError('migration_failed')

    def fetchone(self):
        return {'table_name': 'schema_migrations' if self.existing else None}

    def fetchall(self):
        return [{'version': version} for version in self.existing]


class PostgresStoreTests(unittest.TestCase):
    def test_pooled_neon_connection_does_not_send_startup_options(self):
        pooled = _connection_kwargs(
            'postgresql://user:secret@ep-example-pooler.us-east-2.aws.neon.tech/proactive'
        )
        direct = _connection_kwargs(
            'postgresql://user:secret@ep-example.us-east-2.aws.neon.tech/proactive'
        )
        self.assertNotIn('options', pooled)
        self.assertEqual(direct['options'], '-c statement_timeout=3000 -c lock_timeout=3000')

    def test_store_selection_requires_url_without_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict('os.environ', {'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'sqlite'}):
                self.assertIsInstance(State().proactive_store, ProactiveStore)
            with patch.dict('os.environ', {'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres', 'DATABASE_URL': ''}):
                with self.assertRaisesRegex(ValueError, 'postgres_database_url_required'):
                    State()
            with patch.dict('os.environ', {'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres', 'DATABASE_URL': 'postgresql://local/test'}):
                self.assertIsInstance(State().proactive_store, PostgresProactiveStore)

    def test_admin_token_must_be_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            common = {'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'sqlite',
                      'LINJIAN_TOKEN': 'device-secret', 'LINJIAN_SCHEDULER_TOKEN': 'scheduler-secret'}
            for reused in ('device-secret', 'scheduler-secret'):
                with patch.dict('os.environ', {**common, 'LINJIAN_PROACTIVE_ADMIN_TOKEN': reused}):
                    with self.assertRaisesRegex(ValueError, 'proactive_admin_token_must_be_distinct'):
                        State()

    def test_send_defaults_off_and_request_log_redacts_query(self):
        with tempfile.TemporaryDirectory() as temp, patch('linjian_server.load_dotenv'):
            with patch.dict('os.environ', {'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'sqlite'}, clear=True):
                self.assertFalse(State().proactive_enabled)
        fake = Mock()
        fake.address_string.return_value = '127.0.0.1'
        output = io.StringIO()
        with patch('sys.stderr', output):
            Handler.log_message(fake, '"%s" %s %s', 'GET /api/poll?token=sensitive-value HTTP/1.1', '403', '-')
        self.assertNotIn('sensitive-value', output.getvalue())
        self.assertIn('GET /api/poll', output.getvalue())

    def test_proactive_failure_does_not_break_normal_command(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {
            'LINJIAN_DATA_DIR': temp, 'LINJIAN_TOKEN': 'device-test-token',
            'LINJIAN_SCHEDULER_TOKEN': 'scheduler-test-token',
            'LINJIAN_PROACTIVE_ADMIN_TOKEN': 'admin-test-token',
            'LINJIAN_PROACTIVE_SEND_ENABLED': 'true', 'LINJIAN_PROACTIVE_STORE': 'sqlite'}):
            state = State()
            state.proactive_store = Mock()
            state.proactive_store.expire.side_effect = RuntimeError('database_down')
            state.proactive_store.claim.side_effect = RuntimeError('database_down')
            state.proactive_store.report.side_effect = RuntimeError('database_down')
            state.proactive_store.create_manual_event.side_effect = RuntimeError('DATABASE_URL=sensitive-value')
            state.manual_event_source.store = state.proactive_store
            Handler.state = state
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
                state.proactive_store.expire.assert_not_called()
                state.proactive_store.recover_timeouts.assert_not_called()
                state.proactive_store.report.assert_not_called()
                self.assertIsNone(json.load(urlopen(request))['command'])
                admin_request = Request(base + '/api/proactive/events', json.dumps({
                    'device_id': 'phone', 'title': '提醒', 'message': '内容',
                    'due_at': '2026-10-14T04:00:00Z', 'expires_at': '2026-10-15T04:00:00Z'}).encode(),
                    {'X-Proactive-Admin-Token': 'admin-test-token', 'Content-Type': 'application/json'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(admin_request)
                self.assertEqual(error.exception.code, 503)
                self.assertNotIn('sensitive-value', error.exception.read().decode())
            finally:
                server.shutdown()
                server.server_close()

    def test_migration_applies_once_and_rolls_back_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, '001_first.sql').write_text('CREATE TABLE example(id integer);', encoding='utf-8')
            existing = set()
            connections = []

            def factory(_):
                conn = FakeConnection(existing)
                connections.append(conn)
                return conn

            url = 'postgresql://local/test'
            self.assertEqual(migrate(url, factory, temp), [1])
            self.assertEqual(existing, {1})
            self.assertEqual(migrate(url, factory, temp), [])
            self.assertFalse(any('CREATE TABLE example' in sql for sql, _ in connections[-1].calls))
            Path(temp, '002_second.sql').write_text('CREATE TABLE failure(id integer);', encoding='utf-8')

            def failing_factory(_):
                return FakeConnection(existing, fail_sql=True)

            with self.assertRaisesRegex(RuntimeError, 'migration_failed'):
                migrate(url, failing_factory, temp)
            self.assertEqual(existing, {1})

    def test_postgres_write_is_parameterized_and_transactional(self):
        cur = MagicMock()
        cur.fetchone.return_value = {'id': 7, 'status': 'queued', 'attempt_count': 0,
                                     'title': '提醒', 'message': '内容'}
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        store = PostgresProactiveStore('postgresql://local/test', connection_factory=Mock(return_value=conn))
        row = store.claim('phone', 'command-1', '2026-10-14T04:00:00Z')
        self.assertEqual(row['id'], 7)
        self.assertEqual(cur.execute.call_count, 3)
        for call in cur.execute.call_args_list:
            sql, params = call.args
            self.assertNotIn('command-1', sql)
            self.assertTrue(params)
        conn.__exit__.assert_called_once()

    def test_postgres_schedule_uses_device_transaction_lock(self):
        cur = MagicMock()
        cur.fetchone.side_effect = [None, {'count': 0}, None, None, {'id': 3}]
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        store = PostgresProactiveStore('postgresql://local/test', connection_factory=Mock(return_value=conn))
        candidate = {'device_id': 'phone', 'event_id': 'event', 'topic': 'topic', 'message': '内容',
                     'expires_at': '2026-10-15T04:00:00Z'}
        result = store.schedule(candidate, '2026-10-14T00:00:00Z', '2026-10-14T04:00:00Z', 2,
                                lambda recent: ('send', 'eligible'))
        self.assertEqual(result['message_id'], 3)
        self.assertIn('pg_advisory_xact_lock', cur.execute.call_args_list[0].args[0])


if __name__ == '__main__':
    unittest.main()
