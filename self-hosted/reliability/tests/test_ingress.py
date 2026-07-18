"""СТ-1, 2, 4: приём webhook (подпись, dedup, устойчивость к битому payload)."""
import hashlib
import hmac
import json
import unittest

from reliability.ingress import handle_webhook
from reliability.state import StateStore

SECRET = "s"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def pr_body() -> bytes:
    return json.dumps({
        "action": "opened",
        "repository": {"full_name": "o/r"},
        "pull_request": {"number": 7, "head": {"sha": "abc"}},
    }).encode()


class Sched:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class TestHandleWebhook(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(":memory:")
        self.sched = Sched()

    def _hdr(self, body, event="pull_request", delivery="d1", sig=None):
        return {
            "X-Hub-Signature-256": sig if sig is not None else sign(body),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        }

    def _call(self, body, **hdr_kw):
        return handle_webhook(body, self._hdr(body, **hdr_kw),
                              secret=SECRET, store=self.store, schedule=self.sched)

    def test_bad_signature_401(self):
        b = pr_body()
        self.assertEqual(self._call(b, sig="sha256=bad"), 401)
        self.assertEqual(self.sched.events, [])

    def test_valid_pr_schedules_two_commands(self):
        self.assertEqual(self._call(pr_body()), 200)
        self.assertEqual(len(self.sched.events), 2)  # describe + review

    def test_duplicate_delivery_dedups(self):
        b = pr_body()
        self._call(b)                       # delivery d1
        self.assertEqual(self._call(b), 200)  # тот же d1 повторно
        self.assertEqual(len(self.sched.events), 2)  # повторно не запланировано

    def test_malformed_json_400(self):
        b = b"{not valid json"
        self.assertEqual(self._call(b), 400)
        self.assertEqual(self.sched.events, [])

    def test_missing_fields_no_events(self):
        b = json.dumps({"action": "opened", "repository": {}}).encode()  # нет pull_request
        self.assertEqual(self._call(b), 200)
        self.assertEqual(self.sched.events, [])

    def test_logs_arrival_and_accepted(self):
        # в логах контейнера ingress: строка о приходе + результат с enqueued/deduped
        with self.assertLogs("reliability.ingress", level="INFO") as cm:
            self._call(pr_body())
        joined = "\n".join(cm.output)
        self.assertIn("webhook received: event=pull_request", joined)
        self.assertIn("webhook accepted 200: event=pull_request parsed=2 enqueued=2 deduped=0", joined)

    def test_logs_rejected_bad_signature(self):
        with self.assertLogs("reliability.ingress", level="WARNING") as cm:
            self._call(pr_body(), sig="sha256=bad")
        self.assertIn("webhook rejected 401", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
