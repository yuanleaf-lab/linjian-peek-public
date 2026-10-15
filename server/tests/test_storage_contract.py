import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proactive_store import ProactiveStore, ProactiveStoreProtocol
from postgres_proactive_store import PostgresProactiveStore, PostgresStoreNotReady
from proactive_scheduler import run_candidate


class StorageContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.tmp.name) / 'store.sqlite3'
        self.store = ProactiveStore(self.path)
        self.now = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.candidate = {'device_id': 'phone', 'event_id': 'event', 'topic': 'topic',
                          'title': '提醒', 'message': '内容',
                          'expires_at': (self.now + timedelta(days=2)).isoformat()}
        self.policy = {'enabled': True, 'daily_max': 2, 'cooldown_minutes': 0}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sqlite_policy_and_schema_v4_migration(self):
        self.assertIsInstance(self.store, ProactiveStoreProtocol)
        self.assertIsNone(self.store.get_policy('phone'))
        policy = self.store.set_policy('phone', self.policy, self.now.isoformat())
        self.assertEqual(policy['version'], 1)
        self.assertEqual(self.store.set_policy('phone', {'daily_max': 1}, self.now.isoformat())['version'], 2)
        reopened = ProactiveStore(self.path)
        self.assertEqual(reopened.get_policy('phone')['daily_max'], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT version FROM schema_migrations').fetchone()[0], 4)

    def test_stored_policy_can_disable_candidate(self):
        self.store.set_policy('phone', {'enabled': False}, self.now.isoformat())
        result = run_candidate(self.store, self.candidate, self.policy, self.now, simulate=True)
        self.assertEqual((result['decision'], result['reason']), ('skip', 'disabled'))

    def test_attempts_are_durable_and_late_old_report_does_not_complete_new_attempt(self):
        run_candidate(self.store, self.candidate, self.policy, self.now, False)
        self.store.claim('phone', 'first', self.now.isoformat())
        self.assertEqual(self.store.report('first', False, self.now.isoformat(), 'temporary_unavailable'), 1)
        run_candidate(self.store, self.candidate, self.policy, self.now + timedelta(minutes=6), False)
        self.store.claim('phone', 'second', (self.now + timedelta(minutes=6)).isoformat())
        self.assertEqual(self.store.report('first', True, (self.now + timedelta(minutes=7)).isoformat()), 0)
        self.assertEqual(self.store.get('phone', 'event', 'topic')['status'], 'dispatched')
        self.assertEqual(self.store.report('second', True, (self.now + timedelta(minutes=7)).isoformat()), 1)
        attempts = self.store.list_attempts(self.store.get('phone', 'event', 'topic')['id'])
        self.assertEqual([a['command_id'] for a in attempts], ['first', 'second'])
        self.assertEqual([a['status'] for a in attempts], ['failed', 'completed'])

    def test_concurrent_quota_stays_atomic(self):
        policy = dict(self.policy, daily_max=1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: run_candidate(self.store, dict(self.candidate, event_id=str(i), topic=str(i)), policy, self.now, False), range(6)))
        self.assertEqual(sum(r['decision'] == 'send' for r in results), 1)
        self.assertEqual(sum(r['decision'] == 'defer' for r in results), 5)

    def test_postgres_requires_configuration_and_never_connects_on_init(self):
        with self.assertRaises(ValueError):
            PostgresProactiveStore('', connection_factory=Mock())
        factory = Mock()
        store = PostgresProactiveStore('postgresql://example.invalid/db', connection_factory=factory)
        self.assertIsInstance(store, ProactiveStoreProtocol)
        factory.assert_not_called()
        self.assertTrue(hasattr(store, 'create_manual_event'))
        factory.assert_not_called()

    def test_postgres_parameterizes_queries_with_mock_connection(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = {'device_id': 'phone', 'daily_max': 2}
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.__enter__.return_value = connection
        factory = Mock(return_value=connection)
        store = PostgresProactiveStore('postgresql://example.invalid/db', connection_factory=factory)
        self.assertEqual(store.get_policy('phone')['daily_max'], 2)
        sql, params = cursor.execute.call_args.args
        self.assertIn('%s', sql)
        self.assertEqual(params, ('phone',))
        self.assertNotIn('phone', sql)

    def test_postgres_migration_has_constraints_and_indexes(self):
        sql = (Path(__file__).resolve().parents[1] / 'migrations' / 'postgres' / '001_proactive.sql').read_text(encoding='utf-8')
        for table in ('schema_migrations', 'manual_events', 'trigger_candidates', 'proactive_messages', 'proactive_policy', 'dispatch_attempts'):
            self.assertIn('CREATE TABLE', sql)
            self.assertIn(table, sql)
        self.assertIn('UNIQUE (device_id, dedupe_key)', sql)
        self.assertNotIn('postgres_mutations_not_implemented', (Path(__file__).resolve().parents[1] / 'postgres_proactive_store.py').read_text(encoding='utf-8'))
        self.assertIn('CREATE INDEX', sql)
        self.assertIn('CHECK (', sql)


if __name__ == '__main__':
    unittest.main()
