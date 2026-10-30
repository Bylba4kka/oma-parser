import asyncio
import logging
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

from app.proxy import ProxyPool
from app.redis import count
from app.settings import settings

log = logging.getLogger(__name__)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "доступ запрещ",
    "too many requests",
    "проверка браузера",
    "checking your browser",
)


class FetchError(Exception):
    pass


class NotFound(FetchError):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    content: bytes


def looks_blocked(status: int, text: str) -> str | None:
    if status in (403, 429, 503, 520, 521, 522):
        return f"http {status}"
    if status == 200 and len(text) < 20000:
        low = text.lower()
        for marker in BLOCK_MARKERS:
            if marker in low:
                return f"marker {marker!r}"
    return None


class Fetcher:
    def __init__(self, pool: ProxyPool) -> None:
        self.pool = pool
        self._sessions: dict[str, AsyncSession] = {}

    def _session(self, proxy_id: str) -> AsyncSession:
        session = self._sessions.get(proxy_id)
        if session is None:
            session = AsyncSession(
                impersonate=settings.impersonate,
                timeout=settings.request_timeout,
                max_clients=settings.proxy_max_concurrent,
                headers=HEADERS,
                allow_redirects=True,
            )
            if settings.cookies:
                for part in settings.cookies.split(";"):
                    if "=" in part:
                        name, value = part.strip().split("=", 1)
                        session.cookies.set(name, value, domain=".oma.by")
            self._sessions[proxy_id] = session
        return session

    async def get(self, url: str, tries: int = 4, accept: str | None = None) -> Response:
        last: Exception | None = None
        for _ in range(tries):
            async with self.pool.acquire() as proxy:
                session = self._session(proxy.id)
                headers = {"Accept": accept} if accept else None
                try:
                    resp = await session.get(url, proxy=proxy.url or None, headers=headers)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    await self.pool.report_error(proxy)
                    await count("net_errors")
                    last = FetchError(f"{type(e).__name__}: {e}"[:300])
                    continue

                await count("requests")

                if resp.status_code in (404, 410):
                    await self.pool.report_ok(proxy)
                    raise NotFound(f"http {resp.status_code}")

                text = resp.text if resp.content else ""
                blocked = looks_blocked(resp.status_code, text)
                if blocked:
                    await self.pool.report_ban(proxy, blocked)
                    await count("blocked")
                    session.cookies.clear()
                    last = FetchError(f"blocked: {blocked}")
                    continue

                if resp.status_code >= 400:
                    await self.pool.report_error(proxy)
                    last = FetchError(f"http {resp.status_code}")
                    continue

                await self.pool.report_ok(proxy)
                return Response(str(resp.url), resp.status_code, text, resp.content)
        raise last or FetchError("unknown")

    async def close(self) -> None:
        for session in self._sessions.values():
            await session.close()
