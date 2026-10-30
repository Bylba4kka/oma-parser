import asyncio
import logging

from app.crawler import Crawler
from app.db import init_db
from app.http import Fetcher
from app.proxy import ProxyPool, load_proxies
from app.redis import RUNNING_KEY, redis
from app.settings import settings

log = logging.getLogger("worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()

    proxies = await load_proxies(redis)
    if proxies:
        log.info("Загружено прокси: %s", len(proxies))
    else:
        log.warning("Прокси не получены — работаем напрямую, снизьте CONCURRENCY")

    if settings.autostart and not await redis.exists(RUNNING_KEY):
        await redis.set(RUNNING_KEY, "1")

    pool = ProxyPool(proxies, redis)
    fetcher = Fetcher(pool)
    refresher = asyncio.create_task(refresh_proxies(pool))
    try:
        await Crawler(fetcher).run()
    finally:
        refresher.cancel()
        await fetcher.close()


async def refresh_proxies(pool: ProxyPool) -> None:
    while True:
        await asyncio.sleep(settings.proxy_refresh_seconds)
        pool.update(await load_proxies(redis, force=True))


if __name__ == "__main__":
    asyncio.run(main())
