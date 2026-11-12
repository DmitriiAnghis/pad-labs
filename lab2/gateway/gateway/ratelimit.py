"""Ограничение частоты запросов алгоритмом «ведро с жетонами» (token bucket).

У каждого клиента ведро на N жетонов; каждый запрос забирает жетон; жетоны восполняются
равномерно (N в минуту). Пустое ведро = ответ 429 Too Many Requests.
Счётчики в памяти процесса: у каждой копии gateway свои. Для нескольких копий их надо
перенести в Redis (записано в DECISIONS.md как следующий шаг).
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """True, если запрос можно пропустить; иначе (False, через сколько секунд повторить)."""
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1:
                self.buckets[key] = (tokens - 1, now)
                return True, 0.0
            self.buckets[key] = (tokens, now)
            return False, (1 - tokens) / self.rate
