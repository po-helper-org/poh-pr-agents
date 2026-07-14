"""Логика минтинга/кэша installation-токена (без крипто и сети)."""
import json
import unittest

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

    def test_installation_lookup_error_raises(self):
        def bad(method, url, headers, data):
            return (500, b"err") if url.endswith("/installation") else (201, b"{}")
        p = InstallationTokenProvider("a", "P", bad, signer, clock=Clock())
        with self.assertRaises(RuntimeError):
            p.get("o/r")


if __name__ == "__main__":
    unittest.main()
