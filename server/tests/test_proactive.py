import json
import os
import tempfile
import threading
import unittest
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proactive_rules import decide
from proactive_scheduler import run_candidate
from proactive_store import ProactiveStore
from linjian_server import Handler, State


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.store = ProactiveStore(Path(self.tmp.name) / "proactive.sqlite3")
        self.now = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
        self.candidate = {"device_id": "phone", "event_id": "exam-1", "topic": "exam", "title": "提醒", "message": "明天考试", "expires_at": (self.now + timedelta(hours=2)).isoformat()}
        self.policy = {"enabled": True, "timezone": "Asia/Shanghai", "quiet_start": "23:30", "quiet_end": "08:00", "daily_max": 2, "cooldown_minutes": 240}

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_closed_and_simulation_does_not_write(self):
        self.assertEqual(run_candidate(self.store, self.candidate, now=self.now)["reason"], "disabled")
        self.assertIsNone(self.store.get("phone", "exam-1", "exam"))
        self.assertEqual(run_candidate(self.store, self.candidate, self.policy, self.now)["decision"], "send")
        self.assertIsNone(self.store.get("phone", "exam-1", "exam"))

    def test_quiet_defer_then_send_and_idempotency(self):
        quiet = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)
        item = dict(self.candidate, expires_at=(quiet + timedelta(hours=12)).isoformat())
        self.assertEqual(run_candidate(self.store, item, self.policy, quiet, False)["decision"], "defer")
        self.assertEqual(run_candidate(self.store, item, self.policy, quiet + timedelta(hours=8), False)["decision"], "send")
        self.assertEqual(run_candidate(self.store, item, self.policy, quiet + timedelta(hours=8), False)["reason"], "idempotent_duplicate")
        reopened = ProactiveStore(Path(self.tmp.name) / "proactive.sqlite3")
        self.assertEqual(reopened.get("phone", "exam-1", "exam")["status"], "queued")

    def test_claim_report_and_expiry(self):
        run_candidate(self.store, self.candidate, self.policy, self.now, False)
        stamp = self.now.isoformat()
        self.assertIsNotNone(self.store.claim("phone", "cmd-1", stamp))
        self.assertIsNone(self.store.claim("phone", "cmd-2", stamp))
        self.assertEqual(self.store.report("cmd-1", True, stamp), 1)
        self.assertEqual(self.store.get("phone", "exam-1", "exam")["status"], "completed")
        later = dict(self.candidate, device_id="other-phone", event_id="exam-2", topic="other")
        run_candidate(self.store, later, self.policy, self.now, False)
        self.assertEqual(self.store.expire((self.now + timedelta(hours=3)).isoformat()), 1)
        self.assertIsNone(self.store.claim("phone", "cmd-3", (self.now + timedelta(hours=3)).isoformat()))

    def test_rule_limits_and_duplicate_topic(self):
        self.assertEqual(decide(self.candidate, self.policy, {"today_count": 2}, self.now), ("defer", "daily_limit"))
        self.assertEqual(decide(self.candidate, self.policy, {"today_count": 0, "topic_repeated": True}, self.now), ("skip", "duplicate_topic"))
        self.assertEqual(decide(self.candidate, self.policy, {"today_count": 0, "last_sent_at": self.now.isoformat()}, self.now), ("defer", "cooldown"))

    def test_schema_migration_and_restart_timeout_no_resend(self):
        old_path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as db:
            with db:
                db.execute("CREATE TABLE proactive_messages(id INTEGER PRIMARY KEY,device_id TEXT NOT NULL,event_id TEXT NOT NULL,topic TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,command_id TEXT UNIQUE,completed_at TEXT,UNIQUE(device_id,event_id,topic))")
                db.execute("INSERT INTO proactive_messages(device_id,event_id,topic,title,message,expires_at,created_at,status,reason,command_id) VALUES(?,?,?,?,?,?,?,?,?,?)", ("legacy", "old-event", "old-topic", "old", "old", "2030-01-01T12:00:00+00:00", self.now.isoformat(), "dispatched", "old", "legacy-command"))
                db.execute("PRAGMA user_version=1")
        migrated = ProactiveStore(old_path)
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(migrated.get("legacy", "old-event", "old-topic")["attempt_count"], 1)
        self.assertEqual(migrated.recover_timeouts("2030-01-01T00:00:00Z"), 1)
        run_candidate(migrated, self.candidate, self.policy, self.now, False)
        migrated.claim("phone", "cmd-old", self.now.isoformat())
        restarted = ProactiveStore(old_path)
        later = (self.now + timedelta(minutes=6)).isoformat()
        self.assertEqual(restarted.recover_timeouts(later), 1)
        row = restarted.get("phone", "exam-1", "exam")
        self.assertEqual((row["status"], row["last_error_code"], row["attempt_count"]), ("failed", "dispatch_outcome_unknown", 1))
        self.assertIsNone(restarted.claim("phone", "cmd-new", later))
        self.assertEqual(run_candidate(restarted, self.candidate, self.policy, self.now + timedelta(minutes=7), False)["reason"], "idempotent_duplicate")
        self.assertEqual(restarted.report("cmd-old", True, later), 1)
        self.assertEqual(restarted.get("phone", "exam-1", "exam")["status"], "completed")

    def test_explicit_failure_retry_limit(self):
        run_candidate(self.store, self.candidate, self.policy, self.now, False)
        self.store.claim("phone", "attempt-1", self.now.isoformat())
        self.store.report("attempt-1", False, self.now.isoformat(), "temporary_unavailable")
        self.assertEqual(run_candidate(self.store, self.candidate, self.policy, self.now + timedelta(minutes=1), False)["reason"], "retry_backoff")
        self.assertEqual(run_candidate(self.store, self.candidate, self.policy, self.now + timedelta(minutes=6), False)["reason"], "retry")
        self.store.claim("phone", "attempt-2", (self.now + timedelta(minutes=6)).isoformat())
        self.store.report("attempt-2", False, (self.now + timedelta(minutes=6)).isoformat(), "temporary_unavailable")
        self.assertEqual(run_candidate(self.store, self.candidate, self.policy, self.now + timedelta(minutes=12), False)["reason"], "idempotent_duplicate")
        self.assertEqual(self.store.get("phone", "exam-1", "exam")["attempt_count"], 2)

    def test_retry_never_revives_expired_message(self):
        run_candidate(self.store, self.candidate, self.policy, self.now, False)
        self.store.claim("phone", "attempt-expiring", self.now.isoformat())
        self.store.report("attempt-expiring", False, self.now.isoformat(), "temporary_unavailable")
        result = run_candidate(self.store, self.candidate, self.policy, self.now + timedelta(hours=3), False)
        self.assertEqual((result["status"], result["reason"]), ("expired", "expired"))

    def test_concurrent_daily_limit_and_duplicate_tick(self):
        policy = dict(self.policy, cooldown_minutes=0)
        candidates = [dict(self.candidate, event_id=f"event-{i}", topic=f"topic-{i}") for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda c: run_candidate(self.store, c, policy, self.now, False), candidates))
        self.assertEqual(sum(r["status"] == "queued" for r in results), 2)
        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(lambda _: run_candidate(self.store, candidates[0], policy, self.now, False), range(8)))
        self.assertTrue(all(r["reason"] in ("idempotent_duplicate", "daily_limit") for r in duplicates))

    def test_existing_normal_command_and_proactive_http(self):
        old = {k: os.environ.get(k) for k in ("LINJIAN_DATA_DIR", "LINJIAN_TOKEN", "LINJIAN_SCHEDULER_TOKEN", "LINJIAN_PROACTIVE_SEND_ENABLED")}
        os.environ.update({"LINJIAN_DATA_DIR": self.tmp.name, "LINJIAN_TOKEN": "test-secret", "LINJIAN_SCHEDULER_TOKEN": "scheduler-secret", "LINJIAN_PROACTIVE_SEND_ENABLED": "false"})
        try:
            Handler.state = State()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            def post(path, data):
                token_header = {"X-Scheduler-Token": "scheduler-secret"} if path == "/api/proactive/tick" else {"X-Auth-Token": "test-secret"}
                req = Request(base + path, json.dumps(data).encode(), {**token_header, "Content-Type": "application/json"})
                return json.load(urlopen(req))
            fresh = dict(self.candidate, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            simulated = post("/api/proactive/tick", {"candidates": [fresh]})
            self.assertIn(simulated["results"][0]["decision"], ("send", "defer"))
            command = post("/api/command", {"device_id": "phone", "action": "noop"})["command"]
            req = Request(base + "/api/poll?device_id=phone", headers={"X-Auth-Token": "test-secret"})
            self.assertEqual(json.load(urlopen(req))["command"]["id"], command["id"])
            self.assertIsNone(json.load(urlopen(req))["command"])
        finally:
            server.shutdown()
            server.server_close()
            for k, v in old.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v

    def test_enabled_http_dispatch_and_android_report(self):
        old = {k: os.environ.get(k) for k in ("LINJIAN_DATA_DIR", "LINJIAN_TOKEN", "LINJIAN_SCHEDULER_TOKEN", "LINJIAN_PROACTIVE_SEND_ENABLED")}
        os.environ.update({"LINJIAN_DATA_DIR": self.tmp.name, "LINJIAN_TOKEN": "test-secret", "LINJIAN_SCHEDULER_TOKEN": "scheduler-secret", "LINJIAN_PROACTIVE_SEND_ENABLED": "true"})
        server = None
        try:
            Handler.state = State()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            def post(path, data):
                token_header = {"X-Scheduler-Token": "scheduler-secret"} if path == "/api/proactive/tick" else {"X-Auth-Token": "test-secret"}
                req = Request(base + path, json.dumps(data).encode(), {**token_header, "Content-Type": "application/json"})
                return json.load(urlopen(req))
            candidate = dict(self.candidate, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            result = post("/api/proactive/tick", {"simulate": False, "candidates": [candidate]})
            self.assertEqual(result["results"][0]["status"], "queued")
            req = Request(base + "/api/poll?device_id=phone", headers={"X-Auth-Token": "test-secret"})
            command = json.load(urlopen(req))["command"]
            self.assertEqual(command["action"], "send_notification")
            post("/api/device/report", {"command_id": command["id"], "ok": True, "result": "heads_up_notification_sent"})
            self.assertEqual(Handler.state.proactive_store.get("phone", "exam-1", "exam")["status"], "completed")
        finally:
            if server:
                server.shutdown()
                server.server_close()
            for k, v in old.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v

    def test_scheduler_auth_and_url_token_rejected(self):
        old = {k: os.environ.get(k) for k in ("LINJIAN_DATA_DIR", "LINJIAN_TOKEN", "LINJIAN_SCHEDULER_TOKEN")}
        os.environ.update({"LINJIAN_DATA_DIR": self.tmp.name, "LINJIAN_TOKEN": "device-secret", "LINJIAN_SCHEDULER_TOKEN": "scheduler-secret"})
        server = None
        try:
            Handler.state = State()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            for headers in ({"X-Auth-Token": "device-secret"}, {"X-Scheduler-Token": "wrong"}):
                req = Request(base + "/api/proactive/tick", b'{"candidates":[]}', headers)
                with self.assertRaises(HTTPError) as error:
                    urlopen(req)
                self.assertEqual(error.exception.code, 403)
            req = Request(base + "/api/poll?device_id=phone&token=device-secret")
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 403)
        finally:
            if server:
                server.shutdown()
                server.server_close()
            for k, v in old.items():
                if v is None: os.environ.pop(k, None)
                else: os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
