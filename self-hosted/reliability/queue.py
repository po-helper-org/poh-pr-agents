"""Durable queue (СТ-6..9): SQLite-backed, at-least-once, visibility-timeout, DLQ.

Переживает рестарт — достаточно для одного узла Dokploy. Для нескольких узлов —
заменить на Redis/RabbitMQ за тем же интерфейсом (lease/ack/nack). Партиции
(repo/installation) дают честность (СТ-7): один «тяжёлый» репо не голодит остальные.

Семантика: `lease` атомарно забирает доступное сообщение из партиции, обслуженной
дольше всех, и ставит visibility-timeout; не подтверждённое (`ack`) сообщение
становится доступным снова после истечения таймаута (redelivery при падении
воркера, СТ-6/17). `nack` возвращает в очередь с backoff или, при исчерпании
попыток, в dead-letter (СТ-9).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Lease:
    id: int
    partition: str
    payload: dict
    attempts: int


class DurableQueue:
    def __init__(self, path: str = ":memory:", clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._clock = clock
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                partition    TEXT NOT NULL,
                payload      TEXT NOT NULL,
                attempts     INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL,
                leased_until REAL,
                enqueued_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_avail ON messages(available_at, leased_until);
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY, partition TEXT, payload TEXT,
                attempts INTEGER, reason TEXT, dead_at REAL
            );
            CREATE TABLE IF NOT EXISTS partition_service (
                partition TEXT PRIMARY KEY, last_served REAL NOT NULL
            );
            """
        )
        self._db.commit()

    def enqueue(self, payload: dict, partition: str, *, delay: float = 0) -> int:
        now = self._clock()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO messages(partition,payload,attempts,available_at,leased_until,enqueued_at)"
                " VALUES(?,?,0,?,NULL,?)",
                (partition, json.dumps(payload), now + delay, now),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def lease(self, *, visibility_timeout: float) -> Optional[Lease]:
        now = self._clock()
        with self._lock:
            row = self._db.execute(
                """
                SELECT m.partition AS p, MIN(m.id) AS mid
                FROM messages m
                LEFT JOIN partition_service ps ON ps.partition = m.partition
                WHERE (m.leased_until IS NULL OR m.leased_until <= ?) AND m.available_at <= ?
                GROUP BY m.partition
                ORDER BY COALESCE(ps.last_served, 0) ASC, mid ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            mid = int(row["mid"])
            self._db.execute(
                "UPDATE messages SET attempts=attempts+1, leased_until=? WHERE id=?",
                (now + visibility_timeout, mid),
            )
            self._db.execute(
                "INSERT INTO partition_service(partition,last_served) VALUES(?,?) "
                "ON CONFLICT(partition) DO UPDATE SET last_served=?",
                (row["p"], now, now),
            )
            self._db.commit()
            m = self._db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
            return Lease(mid, m["partition"], json.loads(m["payload"]), int(m["attempts"]))

    def ack(self, message_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE id=?", (message_id,))
            self._db.commit()

    def nack(self, message_id: int, *, max_attempts: int, backoff: float = 0,
             reason: str = "nack") -> str:
        """Вернуть для повтора ('requeued') или, при исчерпании попыток, в
        dead-letter ('dead_letter'); 'missing' если сообщения уже нет."""
        now = self._clock()
        with self._lock:
            m = self._db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if m is None:
                return "missing"
            if int(m["attempts"]) >= max_attempts:
                self._db.execute(
                    "INSERT INTO dead_letters(id,partition,payload,attempts,reason,dead_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (m["id"], m["partition"], m["payload"], m["attempts"], reason, now),
                )
                self._db.execute("DELETE FROM messages WHERE id=?", (message_id,))
                self._db.commit()
                return "dead_letter"
            self._db.execute(
                "UPDATE messages SET leased_until=NULL, available_at=? WHERE id=?",
                (now + backoff, message_id),
            )
            self._db.commit()
            return "requeued"

    def depth(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def dead_letters(self) -> list:
        with self._lock:
            return self._db.execute("SELECT * FROM dead_letters ORDER BY id").fetchall()
