"""Логика минтинга/кэша installation-токена (без крипто и сети)."""
import json
import unittest
from datetime import datetime, timezone

from reliability.token import InstallationTokenProvider


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTransport:
    """installation → {id:42}; access_tokens → {token, expires_at:null}."""
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url))
        if url.endswith("/installation"):
            return 200, json.dumps({"id": 42}).encode()
        if url.endswith("/access_tokens"):
            return 201, json.dumps({"token": "ghs_abc", "expires_at": None}).encode()
        return 404, b"{}"


def signer(app_id, pem, iat, exp):
    return f"JWT[{app_id}]"


class TestInstallationTokenProvider(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.t = FakeTransport()
        self.p = InstallationTokenProvider("appid", "PEM", self.t, signer, clock=self.clock)

    def test_exchange_returns_token(self):
        self.assertEqual(self.p.get("o/r"), "ghs_abc")
        self.assertEqual(len(self.t.calls), 2)  # installation + access_tokens

    def test_caches_until_expiry(self):
        self.p.get("o/r")
        n = len(self.t.calls)
        self.p.get("o/r")  # из кэша (expires_at=None → clock()+3000)
        self.assertEqual(len(self.t.calls), n)

    def test_refetch_after_expiry(self):
        self.p.get("o/r")
        n = len(self.t.calls)
        self.clock.t += 5000  # за пределами exp
        self.p.get("o/r")
        self.assertEqual(len(self.t.calls), n + 2)

    def test_malformed_expiry_falls_back_and_caches(self):
        calls = {"n": 0}

        def tr(method, url, headers, data):
            calls["n"] += 1
            if url.endswith("/installation"):
                return 200, json.dumps({"id": 1}).encode()
            return 201, json.dumps({"token": "ghs_x", "expires_at": "garbage"}).encode()

        p = InstallationTokenProvider("a", "P", tr, signer, clock=self.clock)
        self.assertEqual(p.get("o/r"), "ghs_x")   # битая дата не роняет
        before = calls["n"]
        self.assertEqual(p.get("o/r"), "ghs_x")   # из кэша (фолбэк exp=clock+3000)
        self.assertEqual(calls["n"], before)      # без новых обменов

    def test_installation_lookup_error_raises(self):
        def bad(method, url, headers, data):
            return (500, b"err") if url.endswith("/installation") else (201, b"{}")
        p = InstallationTokenProvider("a", "P", bad, signer, clock=Clock())
        with self.assertRaises(RuntimeError):
            p.get("o/r")

    def test_list_installations(self):
        def tr(method, url, headers, data):
            self.assertIn("/app/installations", url)
            return 200, json.dumps([{"id": 11}, {"id": 22}]).encode()
        p = InstallationTokenProvider("a", "P", tr, signer, clock=Clock())
        self.assertEqual([i["id"] for i in p.list_installations()], [11, 22])

    def test_token_for_installation(self):
        def tr(method, url, headers, data):
            self.assertEqual(method, "POST")
            self.assertIn("/app/installations/22/access_tokens", url)
            return 201, json.dumps({"token": "ghs_inst22"}).encode()
        p = InstallationTokenProvider("a", "P", tr, signer, clock=Clock())
        self.assertEqual(p.token_for(22), "ghs_inst22")


if __name__ == "__main__":
    unittest.main()


class TestRefreshMarginCoversTheWholeRun(unittest.TestCase):
    """PR-AGENT-B/C/D: ревью сгенерировано, публикация упала на 401.

    Токен берётся один раз в начале прогона и отдаётся pr-agent в настройки, а
    публикует он ревью в самом конце — спустя минуты. Запас в 60 секунд считал
    годным токен, который к моменту публикации уже мёртв.
    """

    class _ExpiringTransport(FakeTransport):
        def __call__(self, method, url, headers, data):
            if url.endswith("/installation"):
                return 200, json.dumps({"id": 42}).encode()
            if url.endswith("/access_tokens"):
                self.calls.append((method, url))
                n = len([c for c in self.calls if c[1].endswith("/access_tokens")])
                return 201, json.dumps({
                    "token": f"ghs_{n}",
                    "expires_at": "2026-08-19T10:00:00Z",
                }).encode()
            return 404, b"{}"

    def test_token_with_minutes_left_is_refetched(self):
        transport = self._ExpiringTransport()
        expiry = datetime(2026, 8, 19, 10, 0, 0, tzinfo=timezone.utc).timestamp()

        clock = Clock()
        clock.t = expiry - 3600            # час до истечения — токен свежий
        provider = InstallationTokenProvider("a", "P", transport, signer, clock=lambda: clock.t)
        first = provider.get("o/r")

        clock.t = expiry - 300             # осталось 5 минут: прогон столько идёт
        second = provider.get("o/r")

        self.assertNotEqual(first, second,
                            "отдан токен, который умрёт до конца прогона")

    def test_cache_still_works_for_the_bulk_of_the_hour(self):
        transport = self._ExpiringTransport()
        expiry = datetime(2026, 8, 19, 10, 0, 0, tzinfo=timezone.utc).timestamp()

        clock = Clock()
        clock.t = expiry - 3600
        provider = InstallationTokenProvider("a", "P", transport, signer, clock=lambda: clock.t)
        first = provider.get("o/r")

        clock.t = expiry - 1200            # 20 минут в запасе — обмен не нужен
        self.assertEqual(provider.get("o/r"), first)
