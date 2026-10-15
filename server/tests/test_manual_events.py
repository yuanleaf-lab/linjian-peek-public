import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proactive_store import ProactiveStore
from manual_event_source import ManualEventSource
from trigger_candidate import scan_manual_events, dispatch_candidates
from linjian_server import Handler, State


class ManualEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProactiveStore(Path(self.tmp.name) / 'events.sqlite3')
        self.source = ManualEventSource(self.store)
        self.now = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
        self.due = (self.now - timedelta(minutes=1)).isoformat()
        self.expiry = (self.now + timedelta(days=2)).isoformat()
        self.policy = {'enabled': True, 'daily_max': 2, 'cooldown_minutes': 0}

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **extra):
        return self.source.create({'device_id': 'phone', 'title': '考试', 'message': '记得考试', 'due_at': self.due, 'expires_at': self.expiry, **extra}, self.now)

    def test_duplicate_scan_and_dispatch(self):
        event = self.create()
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: scan_manual_events(self.source, self.store, self.now), range(5)))
        self.assertEqual(len(self.store.list_candidates()), 1)
        first = dispatch_candidates(self.store, self.now, self.policy, simulate=False)
        second = dispatch_candidates(self.store, self.now, self.policy, simulate=False)
        self.assertEqual(first[0]['decision'], 'send')
        self.assertEqual(second, [])
        self.assertEqual(self.store.get('phone', f'manual:{event["id"]}:1', f'manual:{event["id"]}')['status'], 'queued')

    def test_update_and_cancel_invalidate_queued(self):
        event = self.create()
        scan_manual_events(self.source, self.store, self.now)
        dispatch_candidates(self.store, self.now, self.policy, simulate=False)
        self.source.update(event['id'], {'message': '新内容'}, self.now)
        self.assertIsNone(self.store.claim('phone', 'old-command', self.now.isoformat()))
        scan_manual_events(self.source, self.store, self.now)
        self.assertEqual(len(self.store.list_candidates()), 2)
        self.source.cancel(event['id'], self.now)
        self.assertEqual(dispatch_candidates(self.store, self.now, self.policy, simulate=False), [])
        self.assertIsNone(self.store.claim('phone', 'new-command', self.now.isoformat()))

    def test_daily_limit_is_defer_then_next_day_send(self):
        for index in range(2):
            self.create(title=f'事项 {index}')
        scan_manual_events(self.source, self.store, self.now)
        self.policy['daily_max'] = 1
        decisions = dispatch_candidates(self.store, self.now, self.policy, simulate=False)
        self.assertEqual([x['decision'] for x in decisions], ['send', 'defer'])
        later = self.now + timedelta(days=1)
        self.assertEqual(dispatch_candidates(self.store, later, self.policy, simulate=False)[0]['decision'], 'send')

    def test_expired_event_never_generates_or_dispatches(self):
        event = self.create(expires_at=(self.now + timedelta(minutes=1)).isoformat())
        scan_manual_events(self.source, self.store, self.now)
        self.assertEqual(len(self.store.list_candidates()), 1)
        self.assertEqual(dispatch_candidates(self.store, self.now + timedelta(minutes=2), self.policy, simulate=False), [])
        scan_manual_events(self.source, self.store, self.now + timedelta(minutes=2))
        self.assertEqual(self.store.list_candidates()[0]['status'], 'expired')
        self.assertIsNone(self.store.get('phone', f'manual:{event["id"]}:1', f'manual:{event["id"]}'))

    def test_http_auth_and_normal_command(self):
        old = {k: os.environ.get(k) for k in ('LINJIAN_DATA_DIR', 'LINJIAN_TOKEN', 'LINJIAN_SCHEDULER_TOKEN', 'LINJIAN_PROACTIVE_ADMIN_TOKEN', 'LINJIAN_PROACTIVE_SEND_ENABLED')}
        os.environ.update({'LINJIAN_DATA_DIR': self.tmp.name, 'LINJIAN_TOKEN': 'device', 'LINJIAN_SCHEDULER_TOKEN': 'schedule', 'LINJIAN_PROACTIVE_ADMIN_TOKEN': 'admin', 'LINJIAN_PROACTIVE_SEND_ENABLED': 'false'})
        server = None
        try:
            Handler.state = State()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            def post(path, payload, headers):
                request = Request(base + path, json.dumps(payload).encode(), {'Content-Type': 'application/json', **headers})
                return json.load(urlopen(request))
            payload = {'device_id': 'phone', 'title': '考试', 'message': '提醒', 'due_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
            for header in ({}, {'X-Auth-Token': 'device'}, {'X-Scheduler-Token': 'schedule'}):
                with self.assertRaises(HTTPError) as error:
                    post('/api/proactive/events', payload, header)
                self.assertEqual(error.exception.code, 403)
            created = post('/api/proactive/events', payload, {'X-Proactive-Admin-Token': 'admin'})
            self.assertEqual(created['event']['status'], 'active')
            tick = post('/api/proactive/tick', {'candidates': []}, {'X-Scheduler-Token': 'schedule'})
            self.assertEqual(tick['generated_candidates'], 1)
            self.assertTrue(tick['simulated'])
            self.assertEqual(len(tick['source_results']), 1)
            event_id = created['event']['id']
            updated = post(f'/api/proactive/events/{event_id}', {'action': 'update', 'changes': {'message': '新提醒'}}, {'X-Proactive-Admin-Token': 'admin'})
            self.assertEqual(updated['event']['revision'], 2)
            cancelled = post(f'/api/proactive/events/{event_id}', {'action': 'cancel'}, {'X-Proactive-Admin-Token': 'admin'})
            self.assertEqual(cancelled['event']['status'], 'cancelled')
            self.assertEqual(post('/api/proactive/tick', {'candidates': []}, {'X-Scheduler-Token': 'schedule'})['source_results'], [])
            self.assertEqual(post('/api/command', {'device_id': 'phone', 'action': 'noop'}, {'X-Auth-Token': 'device'})['command']['action'], 'noop')
        finally:
            if server:
                server.shutdown()
                server.server_close()
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == '__main__':
    unittest.main()
