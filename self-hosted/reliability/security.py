"""HMAC-проверка webhook (СТ-1).

Проверяет подпись GitHub `X-Hub-Signature-256` = `sha256=<hex>` над сырым телом.
Несовпадение → False → ingress обязан ответить 401 и не принимать событие.
"""
from __future__ import annotations

import hashlib
import hmac


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """True только если подпись валидна для (secret, body).

    Сравнение constant-time (`hmac.compare_digest`), чтобы не течь по таймингу.
    Пустой/некорректный заголовок → False (не принимаем неподписанное).
    """
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
