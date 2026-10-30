import asyncio
import hashlib
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

from httpx import AsyncClient
from redis.asyncio import Redis

from app.redis import key
from app.settings import settings

log = logging.getLogger(__name__)

PROXYS_IO_URL = "https://proxys.io/ru/api/v2/ip"


class NoProxyError(Exception):
    pass


class ProxysIoManager:
    """Забирает список купленных прокси из API proxys.io и кэширует в Redis."""

    def __init__(
        self,
        api_key: str,
        redis: Redis,
        redis_key: str = "proxysio",
        cache_ttl: int = 3600,
        via_proxy: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._redis = redis
        self.redis_key = key(redis_key)
        self.cache_ttl = cache_ttl
        self.via_proxy = via_proxy or None

    async def get_proxies(self, force: bool = False) -> list[str]:
        if not force:
            cache = await self._redis.get(self.redis_key)
            if cache:
                return json.loads(cache)

        proxies = await self._fetch_proxies()
        await self._redis.set(self.redis_key, json.dumps(proxies), ex=self.cache_ttl)
        return proxies

    async def _fetch_proxies(self) -> list[str]:
        async with AsyncClient(proxy=self.via_proxy, timeout=60) as client:
            response = await client.get(PROXYS_IO_URL, params={"key": self._api_key})
            response.raise_for_status()
            data = response.json()

        now = time.time()
        proxies = []
        for order in data["data"]:
            if order["expires_at"] <= now:
                continue
            username = order["username"]
            password = order["password"]
            for item in order["list_ip"]:
                proxies.append(f"http://{username}:{password}@{item['ip']}:{item['port_http']}")

        if not proxies:
            raise NoProxyError("proxys.io вернул пустой список")

        return proxies


def normalize_proxy(raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        return raw
    if "@" in raw:
        return f"http://{raw}"
    parts = raw.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    if len(parts) == 2:
        return f"http://{raw}"
    log.warning("Не понял формат прокси: %s", raw)
    return None


def _static_proxies() -> list[str]:
    raw: list[str] = []
    if settings.proxies:
        raw += settings.proxies.replace(",", "\n").splitlines()
    path = Path(settings.proxies_file)
    if path.exists():
        raw += path.read_text().splitlines()
    return [p for p in (normalize_proxy(r) for r in raw) if p]


async def load_proxies(redis: Redis, force: bool = False) -> list[str]:
    """Прокси из API proxys.io; если токена нет или API лежит — из PROXIES/файла."""
    proxies: list[str] = []
    if settings.proxys_io_api_key:
        manager = ProxysIoManager(settings.proxys_io_api_key, redis, via_proxy=settings.proxys_io_via_proxy)
        try:
            proxies = await manager.get_proxies(force=force)
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось получить прокси из proxys.io: %r", e)
    if not proxies:
        proxies = _static_proxies()
    return list(dict.fromkeys(proxies))


def proxy_id(proxy: str) -> str:
    return hashlib.sha1(proxy.encode()).hexdigest()[:12]


class _ProxyState:
    def __init__(self, url: str) -> None:
        self.url = url
        self.id = proxy_id(url)
        self.sem = asyncio.Semaphore(settings.proxy_max_concurrent)
        self.next_at = 0.0
        self.fails = 0
        self.banned_until = 0.0
        self.ok = 0
        self.errors = 0


class ProxyPool:
    def __init__(self, proxies: list[str], redis: Redis) -> None:
        self._redis = redis
        self._states = [_ProxyState(p) for p in proxies]
        if not self._states:
            self._states = [_ProxyState("")]

    def __len__(self) -> int:
        return len(self._states)

    def update(self, proxies: list[str]) -> None:
        if not proxies:
            return
        current = {s.url: s for s in self._states}
        self._states = [current.get(p) or _ProxyState(p) for p in proxies]
        added = len(set(proxies) - set(current))
        removed = len(set(current) - set(proxies))
        if added or removed:
            log.info("Прокси обновлены: всего %s, +%s, -%s", len(proxies), added, removed)

    @property
    def direct(self) -> bool:
        return self._states[0].url == ""

    def _candidates(self) -> list[_ProxyState]:
        now = time.monotonic()
        states = [s for s in self._states if s.banned_until <= now and not s.sem.locked()]
        random.shuffle(states)
        states.sort(key=lambda s: s.next_at)
        return states

    @asynccontextmanager
    async def acquire(self):
        while True:
            for state in self._candidates():
                if state.sem.locked():
                    continue
                await state.sem.acquire()
                break
            else:
                await asyncio.sleep(0.05)
                continue
            break

        try:
            wait = state.next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            state.next_at = time.monotonic() + settings.proxy_min_interval
            yield state
        finally:
            state.sem.release()

    async def report_ok(self, state: _ProxyState) -> None:
        state.fails = 0
        state.ok += 1

    async def report_ban(self, state: _ProxyState, reason: str) -> None:
        state.fails += 1
        state.errors += 1
        cooldown = min(settings.proxy_ban_seconds * 2 ** (state.fails - 1), 1800)
        if self.direct:
            cooldown = min(cooldown, 30)
        state.banned_until = time.monotonic() + cooldown
        await self._redis.set(key("proxy", "ban", state.id), reason, ex=int(cooldown))
        log.info("Прокси %s в кулдауне на %ss: %s", state.id, cooldown, reason)

    async def report_error(self, state: _ProxyState) -> None:
        state.errors += 1
        state.fails += 1
        if state.fails >= 3:
            await self.report_ban(state, "network errors")

    async def reset_bans(self) -> int:
        """Снять кулдауны со всех прокси."""
        now = time.monotonic()
        count = sum(1 for s in self._states if s.banned_until > now)
        for state in self._states:
            state.banned_until = 0.0
            state.fails = 0
        keys = [k async for k in self._redis.scan_iter(match=key("proxy", "ban", "*"))]
        if keys:
            await self._redis.delete(*keys)
        log.info("Кулдауны сняты с %s прокси", count)
        return count

    async def publish_stats(self) -> None:
        now = time.monotonic()
        data = {
            "total": len(self._states) if not self.direct else 0,
            "banned": sum(1 for s in self._states if s.banned_until > now),
            "busy": sum(1 for s in self._states if s.sem.locked()),
        }
        await self._redis.set(key("proxy", "stats"), json.dumps(data), ex=60)
