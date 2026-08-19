"""Sentry-обвязка: скраббер секретов и необязательность DSN.

Сеть не трогаем: скраббер — чистая функция, а capture_* без configure() = no-op.
"""
import unittest

from reliability import sentry_setup


class TestScrubber(unittest.TestCase):
    def test_filters_secrets_in_stack_frame_vars(self):
        event = {"exception": {"values": [{"stacktrace": {"frames": [{"vars": {
            "token": "ghs_liveInstallationToken",
            "GITHUB_PRIVATE_KEY_B64": "LS0tLS1CRUdJTi...",
            "openai_key": "sk-live",
            "repo": "po-helper-org/app",
            "attempts": 5,
        }}]}}]}}
        sentry_setup._scrub_event(event)
        v = event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        self.assertEqual(v["token"], "[Filtered]")
        self.assertEqual(v["GITHUB_PRIVATE_KEY_B64"], "[Filtered]")
        self.assertEqual(v["openai_key"], "[Filtered]")
        self.assertEqual(v["repo"], "po-helper-org/app")  # диагностика сохраняется
        self.assertEqual(v["attempts"], 5)

    def test_filters_diff_and_patch(self):
        event = {"exception": {"values": [{"stacktrace": {"frames": [
            {"vars": {"diff": "--- a/secret.py", "patch": "@@ -1 +1 @@"}}]}}]}}
        sentry_setup._scrub_event(event)
        v = event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        self.assertEqual(v["diff"], "[Filtered]")
        self.assertEqual(v["patch"], "[Filtered]")

    def test_filters_request_headers_and_drops_body(self):
        event = {"request": {
            "headers": {"X-Hub-Signature-256": "sha256=deadbeef", "User-Agent": "GitHub"},
            "data": "весь payload webhook'а",
        }}
        sentry_setup._scrub_event(event)
        self.assertEqual(event["request"]["headers"]["X-Hub-Signature-256"], "[Filtered]")
        self.assertEqual(event["request"]["headers"]["User-Agent"], "GitHub")
        self.assertNotIn("data", event["request"])

    def test_truncates_long_values(self):
        event = {"extra": {"body": "x" * 5000}}
        sentry_setup._scrub_event(event)
        self.assertLess(len(event["extra"]["body"]), 5000)
        self.assertTrue(event["extra"]["body"].endswith("[truncated]"))

    def test_scrubs_nested_dicts(self):
        event = {"extra": {"ctx": {"api_token": "t", "n": 1}}}
        sentry_setup._scrub_event(event)
        self.assertEqual(event["extra"]["ctx"]["api_token"], "[Filtered]")
        self.assertEqual(event["extra"]["ctx"]["n"], 1)

    def test_handles_event_without_exception_or_request(self):
        event = {"message": "hello"}
        self.assertEqual(sentry_setup._scrub_event(event), {"message": "hello"})


class TestOptional(unittest.TestCase):
    """Без DSN стек ведёт себя как до интеграции — это и процедура отката."""

    def setUp(self):
        self._saved = sentry_setup._configured
        sentry_setup._configured = False

    def tearDown(self):
        sentry_setup._configured = self._saved

    def test_configure_without_dsn_is_noop(self):
        import os
        saved = os.environ.pop("SENTRY_DSN", None)
        try:
            self.assertFalse(sentry_setup.configure("worker"))
        finally:
            if saved is not None:
                os.environ["SENTRY_DSN"] = saved

    def test_capture_helpers_are_noop_when_disabled(self):
        from reliability.state import Event
        ev = Event(delivery_id="d1", repo="o/r", number=1, head_sha="abc", command="/review")
        # не должно бросать и не должно требовать sentry_sdk
        sentry_setup.capture_dead_letter(ev, "timeout", 5)
        sentry_setup.capture_gateway_unavailable(ev, [("zai", "TaskTimeout")])


if __name__ == "__main__":
    unittest.main()


class TestAiohttpLeakNoise(unittest.TestCase):
    """PR-AGENT-9/T/X: 200 событий «Unclosed …» от чужой библиотеки за месяц.

    Их пишет aiohttp через логгер asyncio, когда `pr_agent` бросает свою
    ClientSession незакрытой в `asyncio.run`. Ни стека, ни нашего кадра — в
    списке они лишь топят настоящие сбои.
    """

    def test_unclosed_session_is_dropped(self):
        event = {"logger": "asyncio",
                 "logentry": {"message": "Unclosed client session\nclient_session: <...>"},
                 "level": "error"}
        self.assertIsNone(sentry_setup._scrub_event(event))

    def test_unclosed_connector_is_dropped(self):
        event = {"logger": "asyncio",
                 "logentry": {"message": "Unclosed connector\nconnections: [...]"},
                 "level": "error"}
        self.assertIsNone(sentry_setup._scrub_event(event))

    def test_other_asyncio_errors_survive(self):
        event = {"logger": "asyncio",
                 "logentry": {"message": "Task exception was never retrieved"},
                 "level": "error"}
        self.assertIsNotNone(sentry_setup._scrub_event(event))

    def test_our_own_unclosed_wording_from_another_logger_survives(self):
        """Отсев привязан к логгеру asyncio: свои сообщения не глушим."""
        event = {"logger": "reliability.worker",
                 "logentry": {"message": "Unclosed client session"},
                 "level": "error"}
        self.assertIsNotNone(sentry_setup._scrub_event(event))


class TestExternalFailureClassification(unittest.TestCase):
    """Отказ провайдера не должен выглядеть как баг контура."""

    @staticmethod
    def _event(exc_type):
        return {"exception": {"values": [{"type": exc_type, "value": "..."}]},
                "level": "error"}

    def test_gateway_and_provider_failures_are_downgraded(self):
        for exc_type in ("GatewayUnavailable", "InternalServerError",
                         "RateLimitError", "APITimeoutError"):
            with self.subTest(exc_type=exc_type):
                event = sentry_setup._scrub_event(self._event(exc_type))
                self.assertEqual(event["level"], "warning")
                self.assertEqual(event["fingerprint"], ["external_failure", exc_type])
                self.assertEqual(event["tags"]["failure_side"], "external")

    def test_github_answers_keep_their_level(self):
        """403/422/502 от GitHub — обычно наша ошибка: прав нет, запрос кривой."""
        event = sentry_setup._scrub_event(self._event("HTTPError"))
        self.assertEqual(event["level"], "error")
        self.assertNotIn("fingerprint", event)

    def test_our_own_bug_keeps_its_level(self):
        event = sentry_setup._scrub_event(self._event("KeyError"))
        self.assertEqual(event["level"], "error")
