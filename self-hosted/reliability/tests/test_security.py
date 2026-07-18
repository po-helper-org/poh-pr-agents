"""СТ-1: HMAC-проверка webhook."""
import hashlib
import hmac
import unittest

from reliability.security import verify_signature

SECRET = "topsecret"
BODY = b'{"action":"opened"}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestVerifySignature(unittest.TestCase):
    def test_valid_signature_passes(self):
        self.assertTrue(verify_signature(SECRET, BODY, _sign(SECRET, BODY)))

    def test_tampered_body_fails(self):
        self.assertFalse(verify_signature(SECRET, BODY + b"x", _sign(SECRET, BODY)))

    def test_wrong_secret_fails(self):
        self.assertFalse(verify_signature("other", BODY, _sign(SECRET, BODY)))

    def test_missing_header_fails(self):
        self.assertFalse(verify_signature(SECRET, BODY, None))

    def test_non_sha256_prefix_fails(self):
        bad = "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
        self.assertFalse(verify_signature(SECRET, BODY, bad))

    def test_empty_secret_fails(self):
        self.assertFalse(verify_signature("", BODY, _sign("", BODY)))


if __name__ == "__main__":
    unittest.main()
