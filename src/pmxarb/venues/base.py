"""Shared HTTP plumbing: token-bucket rate limiting, bounded retries, one place for timeouts."""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} {url}: {body[:200]}")
        self.status = status


class RateLimiter:
    def __init__(self, rps: float, burst: int | None = None):
        self.rate = max(rps, 0.1)
        self.capacity = burst or max(1, int(self.rate))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class BaseClient:
    def __init__(self, base_url: str, rps: float, timeout: float = 15.0, name: str = "venue"):
        self.base_url = base_url.rstrip("/")
        self.limiter = RateLimiter(rps)
        self.name = name
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "pm-xarb/0.1 (paper desk)"})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None,
                      base: str | None = None, retries: int = 4) -> Any:
        url = f"{(base or self.base_url).rstrip('/')}/{path.lstrip('/')}"
        attempt = 0
        while True:
            await self.limiter.acquire()
            try:
                r = await self._client.request(method, url, params=params, json=json)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= retries:
                    raise HttpError(0, url, str(exc)) from exc
                await self._backoff(attempt, f"{self.name} transport error {exc!r}")
                attempt += 1
                continue
            if r.status_code in RETRY_STATUS and attempt < retries:
                await self._backoff(attempt, f"{self.name} HTTP {r.status_code} on {path}")
                attempt += 1
                continue
            if r.status_code >= 400:
                raise HttpError(r.status_code, url, r.text)
            if not r.content:
                return None
            return r.json()

    @staticmethod
    async def _backoff(attempt: int, why: str) -> None:
        delay = min(30.0, (2 ** attempt) * 0.5) + random.uniform(0, 0.3)
        log.warning("%s; retrying in %.1fs", why, delay)
        await asyncio.sleep(delay)
