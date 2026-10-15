import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from linjian_server import Handler, State
from postgres_proactive_store import PostgresProactiveStore


class ReadinessTests(unittest.TestCase):
    def test_postgres_missing_url_still_fails_without_fallback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch('linjian_server.load_dotenv'), patch.dict(os.environ, {
            'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres', 'DATABASE_URL': ''}, clear=True):
            with self.assertRaisesRegex(ValueError, 'postgres_database_url_required'):
                State()

    def test_connection_failure_is_safe_and_does_not_block_ordinary_report(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch('linjian_server.load_dotenv'), patch.dict(os.environ, {
            'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres',
            'DATABASE_URL': 'postgresql://user:secret@127.0.0.1:1/test',
            'LINJIAN_TOKEN': 'device', 'LINJIAN_PROACTIVE_ADMIN_TOKEN': 'admin',
            'LINJIAN_SCHEDULER_TOKEN': 'scheduler'}, clear=True):
            state = State()
            Handler.state = state
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + '/api/proactive/readiness', headers={'X-Auth-Token': 'device'}))
                self.assertEqual(denied.exception.code, 403)
                with self.assertRaises(HTTPError) as unavailable:
                    urlopen(Request(base + '/api/proactive/readiness', headers={'X-Proactive-Admin-Token': 'admin'}))
                self.assertEqual(unavailable.exception.code, 503)
                payload = json.load(unavailable.exception)
                self.assertEqual(payload['status'], 'unavailable')
                self.assertNotIn('secret', json.dumps(payload))
                started = time.monotonic()
                request = Request(base + '/api/device/report', json.dumps({'command_id': 'old-normal-command', 'ok': True}).encode(),
                                  {'X-Auth-Token': 'device', 'Content-Type': 'application/json'})
                self.assertTrue(json.load(urlopen(request))['ok'])
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(json.load(urlopen(base + '/health'))['ok'])
            finally:
                server.shutdown()
                server.server_close()

    def test_schema_missing_and_version_mismatch_block_proactive(self):
        for category in ('migration_not_applied', 'schema_version_mismatch'):
            with self.subTest(category=category):
                store = Mock()
                store.readiness.return_value = {'ready': False, 'status': 'unavailable', 'error': category}
                with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch('linjian_server.load_dotenv'), patch.dict(os.environ, {
                    'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'sqlite',
                    'LINJIAN_TOKEN': 'device', 'LINJIAN_PROACTIVE_ADMIN_TOKEN': 'admin',
                    'LINJIAN_SCHEDULER_TOKEN': 'scheduler'}, clear=True):
                    state = State()
                    state.proactive_store = store
                    state.proactive_store_mode = 'postgres'
                    state.proactive_readiness = store.readiness()
                    Handler.state = state
                    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    try:
                        base = f'http://127.0.0.1:{server.server_port}'
                        request = Request(base + '/api/proactive/tick', b'{}', {'X-Scheduler-Token': 'scheduler'})
                        with self.assertRaises(HTTPError) as blocked:
                            urlopen(request)
                        self.assertEqual(blocked.exception.code, 503)
                        store.expire.assert_not_called()
                    finally:
                        server.shutdown()
                        server.server_close()

    def test_unknown_report_after_restart_does_not_wait_for_slow_store(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp, patch('linjian_server.load_dotenv'), patch.dict(os.environ, {
            'LINJIAN_DATA_DIR': temp, 'LINJIAN_PROACTIVE_STORE': 'postgres',
            'DATABASE_URL': 'postgresql://local/test', 'LINJIAN_TOKEN': 'device'}, clear=True):
            state = State()
            called = threading.Event()

            def slow_report(*_):
                time.sleep(1)
                called.set()

            state.proactive_store.report = slow_report
            with ThreadPoolExecutor(max_workers=1) as pool, patch('linjian_server._late_report_pool', pool):
                Handler.state = state
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    started = time.monotonic()
                    request = Request(f'http://127.0.0.1:{server.server_port}/api/device/report',
                                      json.dumps({'command_id': 'old-normal-command', 'ok': True}).encode(),
                                      {'X-Auth-Token': 'device', 'Content-Type': 'application/json'})
                    self.assertTrue(json.load(urlopen(request))['ok'])
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertTrue(called.wait(2))
                finally:
                    server.shutdown()
                    server.server_close()


if __name__ == '__main__':
    unittest.main()
