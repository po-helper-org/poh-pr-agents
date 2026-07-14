"""App JWT → installation token с кэшем по репозиторию.

RS256-подпись JWT требует crypto-библиотеки (PyJWT/cryptography) — она есть в
контейнере pr-agent. Подписант и HTTP-транспорт инъектируются → логика кэша и
обмена тестируется без крипто и без сети.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Callable, Optional

# (method, url, headers, data) -> (status_code, body_bytes)
Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]
# (app_id, pem, iat, exp) -> jwt
JwtSigner = Callable[[str, str, int, int], str]


class InstallationTokenProvider:
    def __init__(self, app_id: str, private_key_pem: str, transport: Transport,
                 jwt_signer: JwtSigner, api_base: str = "https://api.github.com",
                 clock: Callable[[], float] = time.time):
        self._app_id = app_id
        self._pem = private_key_pem
        self._transport = transport
        self._sign = jwt_signer
        self._api = api_base.rstrip("/")
        self._clock = clock
        self._cache: dict[str, "tuple[str, float]"] = {}
        # сериализует получение токена (редкое — кэш ~1 ч), чтобы конкурентный
        # промах не породил дублирующие обмены и не бил по rate-limit GitHub.
        self._lock = threading.Lock()

    def get(self, repo: str) -> str:
        with self._lock:
            cached = self._cache.get(repo)
            if cached and cached[1] - 60 > self._clock():  # запас 60 c до истечения
                return cached[0]
            token, exp = self._exchange(repo)
            self._cache[repo] = (token, exp)
            return token

    def _headers(self, jwt: str) -> dict:
        return {"Authorization": f"Bearer {jwt}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "pr-agent-reliability"}

    def _app_jwt(self) -> str:
        now = int(self._clock())
        return self._sign(self._app_id, self._pem, now - 60, now + 540)  # ≤10 мин

    def _exchange(self, repo: str) -> "tuple[str, float]":
        jwt = self._app_jwt()
        s, b = self._transport("GET", f"{self._api}/repos/{repo}/installation",
                               self._headers(jwt), None)
        if s >= 300:
            raise RuntimeError(f"installation lookup failed: {s}")
        inst_id = json.loads(b)["id"]
        s, b = self._transport("POST",
                               f"{self._api}/app/installations/{inst_id}/access_tokens",
                               self._headers(jwt), b"")
        if s >= 300:
            raise RuntimeError(f"token exchange failed: {s}")
        data = json.loads(b)
        return data["token"], self._parse_expiry(data.get("expires_at"))

    def _parse_expiry(self, s: Optional[str]) -> float:
        if not s:
            return self._clock() + 3000
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return self._clock() + 3000
