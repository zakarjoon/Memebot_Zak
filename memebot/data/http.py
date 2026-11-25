"""HTTP with retry and exponential backoff. Failure returns UNKNOWN, never a guess."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..models import UNKNOWN

log = logging.getLogger("memebot.http")


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class RateLimiter:
    """Minimum interval between calls to one host."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(time.monotonic(), self._next) + self.interval


class Http:
    def __init__(self, timeout: float, retries: int, backoff: float, limits: dict[str, int] | None = None, transport=None,
                 rate_limit_wait: float = 20.0):
        self.rate_limit_wait = rate_limit_wait
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport,
                                        headers={"User-Agent": "memebot/0.1", "Accept": "application/json"})
        self.retries = retries
        self.backoff = backoff
        self.limiters = {host: RateLimiter(n) for host, n in (limits or {}).items()}

    async def get_json(self, url: str, params: dict | None = None, headers: dict | None = None, ok_statuses: tuple[int, ...] = ()) -> Any:
        """Returns parsed JSON, or {"_status": code, "_body": ...} for statuses in ok_statuses, or UNKNOWN."""
        host = httpx.URL(url).host
        for attempt in range(self.retries + 1):
            if host in self.limiters:
                await self.limiters[host].wait()
            try:
                resp = await self.client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in ok_statuses:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = resp.text
                    return {"_status": resp.status_code, "_body": body}
                if resp.status_code == 429:
                    # rate limited: wait out the window (Retry-After if given), not a 1-2s blip
                    retry_after = _f(resp.headers.get("Retry-After"))
                    # GeckoTerminal sends "Retry-After: 0"; treat a non-positive value as absent
                    wait = retry_after if retry_after and retry_after > 0 else self.rate_limit_wait * (attempt + 1)
                    log.warning("GET %s -> 429, waiting %.0fs (attempt %d)", host, wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code not in (500, 502, 503, 504):
                    log.warning("GET %s -> %s", url, resp.status_code)
                    return UNKNOWN
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("GET %s failed (%s), attempt %d", url, exc, attempt + 1)
            await asyncio.sleep(self.backoff * (2 ** attempt))
        return UNKNOWN

    async def close(self) -> None:
        await self.client.aclose()
