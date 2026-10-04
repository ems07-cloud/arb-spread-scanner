"""Асинхронный HTTP-клиент: рейт-лимиты по хосту, ретраи с бэкоффом, таймауты."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("arb.http")


class _Retryable(Exception):
    def __init__(self, msg: str, retry_after: float = 0.0, status: int = 0):
        super().__init__(msg)
        self.retry_after = retry_after
        self.status = status


class HttpError(Exception):
    """Невосстановимая HTTP-ошибка (4xx, кроме 429) — ретраить бессмысленно."""
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class HttpClient:
    """Тонкая обёртка над aiohttp.ClientSession.

    - На каждый хост держим минимальный интервал между запросами (троттлинг).
    - Семафор ограничивает общую параллельность.
    - Ретраи на 429/5xx/таймаут/сетевые ошибки с экспоненциальным бэкоффом,
      уважаем заголовок Retry-After.
    """

    def __init__(self, session: aiohttp.ClientSession, *, timeout: float = 10.0,
                 max_retries: int = 3, concurrency: int = 8):
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_retries = max_retries
        self._sem = asyncio.Semaphore(concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._host_interval: dict[str, float] = {}     # текущий интервал (может быть раздут после 429)
        self._host_base: dict[str, float] = {}         # базовый интервал из конфига
        self._host_ok: dict[str, int] = {}             # серия успехов подряд для возврата к базе

    # Адаптивный троттлинг: при 429 интервал хоста временно растёт, при череде успехов
    # плавно возвращается к базовому. Это надёжнее фиксированного rate_min_interval —
    # P2P-витрины (Bybit/HTX) банят за частоту, а степень их «нервозности» плавает.
    _BACKOFF_FACTOR = 2.0
    _BACKOFF_MIN = 0.5      # ниже этого раздувать нет смысла
    _BACKOFF_CAP = 30.0     # потолок интервала
    _RECOVER_AFTER = 10     # столько успехов подряд -> снижаем интервал

    def set_rate(self, host: str, min_interval: float) -> None:
        iv = max(min_interval, 0.0)
        self._host_base[host] = iv
        self._host_interval[host] = iv

    def _penalize(self, host: str) -> None:
        base = self._host_base.get(host, 0.2)
        cur = self._host_interval.get(host, base)
        new = min(max(cur * self._BACKOFF_FACTOR, self._BACKOFF_MIN), self._BACKOFF_CAP)
        if new > cur:
            self._host_interval[host] = new
            log.warning("хост %s: 429 — увеличиваю интервал %.2f->%.2fс", host, cur, new)
        self._host_ok[host] = 0

    def _reward(self, host: str) -> None:
        base = self._host_base.get(host, 0.2)
        cur = self._host_interval.get(host, base)
        if cur <= base:
            return
        n = self._host_ok.get(host, 0) + 1
        if n >= self._RECOVER_AFTER:
            new = max(cur * 0.5, base)
            self._host_interval[host] = new
            self._host_ok[host] = 0
            log.info("хост %s: серия успехов — снижаю интервал %.2f->%.2fс", host, cur, new)
        else:
            self._host_ok[host] = n

    async def _throttle(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        interval = self._host_interval.get(host, 0.2)
        async with lock:
            wait = interval - (time.monotonic() - self._host_last.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def request_json(self, method: str, url: str, *, headers: dict | None = None,
                           json_body: dict | None = None, params: dict | None = None):
        host = urlparse(url).netloc
        attempt = 0
        while True:
            attempt += 1
            await self._throttle(host)
            try:
                async with self._sem:
                    async with self.session.request(
                        method, url, headers=headers, json=json_body,
                        params=params, timeout=self.timeout,
                    ) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            ra = resp.headers.get("Retry-After")
                            raise _Retryable(f"HTTP {resp.status}", float(ra) if ra else 0.0,
                                             status=resp.status)
                        if resp.status >= 400:
                            # 4xx (кроме 429): детерминированный отказ — без ретраев
                            body = await resp.text()
                            raise HttpError(resp.status, body)
                        data = await resp.json(content_type=None)
                        self._reward(host)   # успех -> постепенно возвращаем интервал к базе
                        return data
            except (aiohttp.ClientError, asyncio.TimeoutError, _Retryable) as e:
                if getattr(e, "status", 0) == 429:
                    self._penalize(host)   # хост просит сбавить — раздуваем интервал
                if attempt > self.max_retries:
                    log.warning("запрос не удался url=%s попыток=%d ошибка=%s", url, attempt, e)
                    raise
                backoff = getattr(e, "retry_after", 0.0) or min(2 ** attempt * 0.5, 15.0)
                log.debug("ретрай url=%s попытка=%d пауза=%.1fс ошибка=%s", url, attempt, backoff, e)
                await asyncio.sleep(backoff)
