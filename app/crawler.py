import asyncio
import json
import logging
import re
import time
import zlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.db import Session
from app.export import build_xlsx
from app.extract import is_product_url, menu_urls, normalize_url, parse_page, parse_sitemap, sitemaps_from_robots
from app.http import Fetcher, FetchError, NotFound
from app.models import Listing, Product, Status
from app.redis import RESET_BANS_KEY, RUNNING_KEY, SEED_KEY, count, key, redis
from app.settings import settings

log = logging.getLogger(__name__)

STALE = "interval '5 minutes'"
PRODUCT_FIELDS = (
    "external_id",
    "sku",
    "name",
    "brand",
    "category",
    "price",
    "old_price",
    "currency",
    "availability",
    "description",
    "params",
    "images",
    "breadcrumbs",
    "card",
)


def seed_listings() -> list[dict]:
    base = settings.base_url.rstrip("/")
    return [
        {"url": f"{base}/robots.txt", "kind": "robots", "name": "robots.txt"},
        {"url": f"{base}/sitemap.xml", "kind": "sitemap", "name": "sitemap.xml"},
        {"url": f"{base}/api-v2/catalog/menu", "kind": "menu", "name": "Меню каталога"},
        {"url": f"{base}/catalog/", "kind": "category", "name": "Каталог товаров"},
        {"url": f"{base}/", "kind": "category", "name": "Главная"},
    ]


LISTING_RE = re.compile(settings.listing_url_re)


def is_listing_url(url: str) -> bool:
    return bool(LISTING_RE.search(urlsplit(url).path))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _product_values(data: dict, page_html: str | None) -> dict:
    values = {k: data.get(k) for k in PRODUCT_FIELDS}
    values["image"] = (data.get("images") or [None])[0]
    values.update(
        status=Status.DONE,
        error=None,
        locked_at=None,
        parsed_at=_now(),
        html=zlib.compress(page_html.encode()) if page_html and settings.store_html else None,
    )
    return values


class DbWriter:
    def __init__(self) -> None:
        self.new_products: set[str] = set()
        self.new_listings: dict[str, dict] = {}
        self.saved: dict[str, dict] = {}
        self.marks: list[tuple[type, int, dict]] = []

    def __len__(self) -> int:
        return len(self.new_products) + len(self.new_listings) + len(self.saved) + len(self.marks)

    async def backpressure(self) -> None:
        while len(self.saved) + len(self.marks) > settings.db_buffer_limit:
            await asyncio.sleep(0.1)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(settings.db_flush_interval)
            await self.flush()

    async def flush(self) -> None:
        if not len(self):
            return
        new_products, self.new_products = self.new_products, set()
        new_listings, self.new_listings = self.new_listings, {}
        saved, self.saved = self.saved, {}
        marks, self.marks = self.marks, []
        try:
            async with Session() as s:
                rows = [{"url": u} for u in sorted(new_products - saved.keys())]
                for i in range(0, len(rows), 1000):
                    await s.execute(
                        insert(Product).values(rows[i : i + 1000]).on_conflict_do_nothing(index_elements=["url"])
                    )
                rows = [new_listings[u] for u in sorted(new_listings)]
                for i in range(0, len(rows), 1000):
                    await s.execute(
                        insert(Listing).values(rows[i : i + 1000]).on_conflict_do_nothing(index_elements=["url"])
                    )
                rows = [{"url": u, **saved[u]} for u in sorted(saved)]
                for i in range(0, len(rows), 200):
                    stmt = insert(Product).values(rows[i : i + 200])
                    cols = [c for c in rows[0] if c != "url"]
                    await s.execute(
                        stmt.on_conflict_do_update(index_elements=["url"], set_={c: stmt.excluded[c] for c in cols})
                    )
                for model, row_id, values in sorted(marks, key=lambda m: (m[0].__tablename__, m[1])):
                    await s.execute(update(model).where(model.id == row_id).values(locked_at=None, **values))
                await s.commit()
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось записать пачку в БД — повторю в следующий раз: %r", getattr(e, "orig", e))
            self.new_products |= new_products
            self.new_listings = new_listings | self.new_listings
            self.saved = saved | self.saved
            self.marks = marks + self.marks
            await asyncio.sleep(1)


writer = DbWriter()


async def add_listings(items: list[dict]) -> None:
    for item in items:
        if item["kind"] != "category" or is_listing_url(item["url"]):
            writer.new_listings.setdefault(item["url"], {"name": None, **item})


async def add_products(urls: set[str]) -> None:
    writer.new_products |= urls


async def save_product(url: str, data: dict, page_html: str | None) -> None:
    await writer.backpressure()
    writer.saved[url] = _product_values(data, page_html)
    await count("products_done")


async def mark(model, row_id: int, **values) -> None:
    await writer.backpressure()
    writer.marks.append((model, row_id, values))


async def fail(model, row, error: str) -> None:
    attempts = row["attempts"] + 1
    status = Status.ERROR if attempts >= settings.max_attempts else Status.PENDING
    await mark(model, row["id"], attempts=attempts, error=error[:1000], status=status)
    await count("failures")


async def claim(model, n: int) -> list:
    if n <= 0:
        return []
    table = model.__tablename__
    order = "(kind = 'category'), id" if model is Listing else "id"
    sql = text(f"""
        UPDATE {table} SET status = 'in_progress', locked_at = now()
        WHERE id IN (
            SELECT id FROM {table} WHERE status = 'pending'
            ORDER BY {order} LIMIT :n FOR UPDATE SKIP LOCKED
        )
        RETURNING *
    """)
    async with Session() as s:
        rows = (await s.execute(sql, {"n": n})).mappings().all()
        await s.commit()
    return rows


async def requeue_stale() -> None:
    """Задачи, зависшие в in_progress (упал воркер), — обратно в очередь."""
    async with Session() as s:
        for table in ("products", "listings"):
            await s.execute(
                text(f"""
                UPDATE {table} SET status = 'pending', locked_at = NULL
                WHERE id IN (
                    SELECT id FROM {table}
                    WHERE status = 'in_progress' AND locked_at < now() - {STALE}
                    FOR UPDATE SKIP LOCKED
                )
            """)
            )
            await s.commit()


class Crawler:
    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=settings.concurrency * 2)
        self.in_flight = 0
        self.listings_active = 0

    async def seed(self, force: bool = False) -> None:
        async with Session() as s:
            has = await s.scalar(select(func.count()).select_from(Listing))
        if has and not force:
            return
        log.info("Посев: robots.txt, sitemap, меню каталога, главная")
        rows = seed_listings()
        async with Session() as s:
            stmt = insert(Listing).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["url"], set_={"status": Status.PENDING, "attempts": 0, "error": None}
            )
            await s.execute(stmt)
            await s.commit()

    async def drop_extra_listings(self) -> None:
        """Листинги, найденные до фильтра LISTING_URL_RE, в очереди больше не нужны."""
        seeds = [r["url"] for r in seed_listings()]
        async with Session() as s:
            res = await s.execute(
                text("""
                    UPDATE listings SET status = 'skipped', error = 'not a category', locked_at = NULL
                    WHERE status IN ('pending', 'in_progress') AND kind = 'category'
                      AND NOT (split_part(url, '?', 1) ~ :re) AND NOT (url = ANY(:seeds))
                """),
                {"re": settings.listing_url_re, "seeds": seeds},
            )
            await s.commit()
        if res.rowcount:
            log.info("Пропущено листингов не-категорий: %s", res.rowcount)

    async def is_running(self) -> bool:
        return (await redis.get(RUNNING_KEY)) == b"1"

    async def run(self) -> None:
        await self.seed()
        await self.drop_extra_listings()
        workers = [asyncio.create_task(self.worker(i)) for i in range(settings.concurrency)]
        background = [
            asyncio.create_task(self.stats_loop()),
            asyncio.create_task(writer.run()),
            asyncio.create_task(self.requeue_loop()),
        ]
        try:
            await self.dispatch()
        finally:
            for t in workers + background:
                t.cancel()
            await writer.flush()

    async def requeue_loop(self) -> None:
        while True:
            try:
                await requeue_stale()
            except Exception:  # noqa: BLE001
                log.exception("requeue_stale")
            await asyncio.sleep(60)

    async def dispatch(self) -> None:
        self._idle_logged = False
        while True:
            try:
                await self.dispatch_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка диспетчера, повтор через 2с")
                await asyncio.sleep(2)

    async def dispatch_step(self) -> None:
        if await redis.getdel(SEED_KEY):
            await self.seed(force=True)

        if not await self.is_running():
            await asyncio.sleep(1)
            return

        free = self.queue.maxsize - self.queue.qsize()
        if free < settings.concurrency // 2:
            await asyncio.sleep(0.2)
            return

        listing_slots = max(1, int(settings.concurrency * settings.listing_share))
        products = await claim(Product, free - min(free, max(0, listing_slots - self.listings_active)))
        listings = await claim(Listing, free - len(products)) if len(products) < free else []
        self.listings_active += len(listings)
        for row in listings:
            await self.queue.put((Listing, row))
        for row in products:
            await self.queue.put((Product, row))

        if not listings and not products:
            if self.in_flight == 0 and self.queue.empty() and not len(writer) and not self._idle_logged:
                log.info("Очередь пуста — парсинг завершён")
                await redis.set(key("finished_at"), str(time.time()))
                self._idle_logged = True
                if settings.auto_export:
                    path = await build_xlsx()
                    log.info("Реестр выгружен в шару: %s", path)
            await asyncio.sleep(2)
        else:
            self._idle_logged = False
            await redis.delete(key("finished_at"))

    async def worker(self, n: int) -> None:
        while True:
            model, row = await self.queue.get()
            self.in_flight += 1
            try:
                if model is Listing:
                    await self.process_listing(row)
                else:
                    await self.process_product(row)
            except NotFound as e:
                await mark(model, row["id"], status=Status.GONE, error=str(e), attempts=row["attempts"] + 1)
            except FetchError as e:
                await fail(model, row, str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("Ошибка обработки %s", row["url"])
                await fail(model, row, f"{type(e).__name__}: {e}")
            finally:
                self.in_flight -= 1
                if model is Listing:
                    self.listings_active -= 1
                self.queue.task_done()

    async def stats_loop(self) -> None:
        while True:
            try:
                if await redis.getdel(RESET_BANS_KEY):
                    await self.fetcher.pool.reset_bans()
                await self.fetcher.pool.publish_stats()
                await redis.set(
                    key("worker", "heartbeat"),
                    json.dumps(
                        {
                            "ts": time.time(),
                            "in_flight": self.in_flight,
                            "queue": self.queue.qsize(),
                            "db_buffer": len(writer),
                        }
                    ),
                    ex=15,
                )
            except Exception:  # noqa: BLE001
                log.exception("stats_loop")
            await asyncio.sleep(3)

    async def process_listing(self, row) -> None:
        kind, url = row["kind"], row["url"]
        found = 0
        name = row["name"]
        expected = None

        if kind == "robots":
            resp = await self.fetcher.get(url, accept="text/plain,*/*")
            maps = sitemaps_from_robots(resp.text)
            await add_listings([{"url": m, "kind": "sitemap", "name": m.rsplit("/", 1)[-1]} for m in maps])

        elif kind == "sitemap":
            resp = await self.fetcher.get(url, accept="application/xml,text/xml,*/*")
            maps, urls = parse_sitemap(resp.content)
            await add_listings([{"url": m, "kind": "sitemap", "name": m.rsplit("/", 1)[-1]} for m in maps])
            products, listings = set(), []
            for u in urls:
                n = normalize_url(u)
                if not n:
                    continue
                if is_product_url(n):
                    products.add(n)
                else:
                    listings.append({"url": n, "kind": "category"})
            await add_products(products)
            await add_listings(listings)
            found = len(products)

        elif kind == "menu":
            resp = await self.fetcher.get(url, accept="application/json, text/plain, */*")
            try:
                data = json.loads(resp.text)
            except ValueError:
                data = None
            items = []
            for u, title in menu_urls(data) if data is not None else []:
                n = normalize_url(u)
                if n:
                    items.append({"url": n, "kind": "category", "name": title})
            await add_listings(items)
            found = len(items)

        else:
            resp = await self.fetcher.get(url)
            page = parse_page(resp.url or url, resp.text)
            if page.product and "_error" not in page.product:
                await save_product(url, page.product, resp.text)
            await add_products(page.product_links)
            await add_listings([{"url": u, "kind": "category"} for u in page.listing_links])
            found = len(page.product_links)
            name = name or page.title
            expected = page.expected_total

        await mark(
            Listing,
            row["id"],
            status=Status.DONE,
            error=None,
            products_found=found,
            name=name,
            expected_total=expected,
            parsed_at=_now(),
            attempts=row["attempts"] + 1,
        )
        await count("listings_done")

    async def process_product(self, row) -> None:
        url = row["url"]
        resp = await self.fetcher.get(url)
        page = parse_page(resp.url or url, resp.text)
        await add_products(page.product_links)

        if page.product and "_error" in page.product:
            raise FetchError(f"parse error: {page.product['_error']}")
        if not page.product:
            await add_listings([{"url": u, "kind": "category"} for u in [url, *page.listing_links]])
            await mark(
                Product, row["id"], status=Status.SKIPPED, error="not a product page", attempts=row["attempts"] + 1
            )
            return
        await save_product(url, page.product, resp.text)
