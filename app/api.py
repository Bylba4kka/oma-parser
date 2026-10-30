import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import defer

from app.db import Session, init_db
from app.export import build_xlsx, export_dir
from app.extract import parse_page
from app.http import Fetcher
from app.models import Listing, Product, Status
from app.proxy import ProxyPool, load_proxies
from app.redis import RESET_BANS_KEY, RUNNING_KEY, SEED_KEY, key, rate_per_minute, redis
from app.settings import settings

_fetcher: Fetcher | None = None
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    if _fetcher:
        await _fetcher.close()


app = FastAPI(title=settings.title, lifespan=lifespan)


async def _counts(*group) -> list:
    async with Session() as s:
        return (await s.execute(select(*group, func.count()).group_by(*group))).all()


@app.get("/api/stats")
async def stats():
    products = {status: n for status, n in await _counts(Product.status)}
    listings_raw = await _counts(Listing.kind, Listing.status)
    listings: dict = {}
    by_kind: dict = {}
    for kind, status, n in listings_raw:
        listings[status] = listings.get(status, 0) + n
        by_kind.setdefault(kind, {})[status] = n

    done = products.get(Status.DONE, 0)
    total = sum(products.values()) - products.get(Status.SKIPPED, 0) - products.get(Status.GONE, 0)
    remaining = products.get(Status.PENDING, 0) + products.get(Status.IN_PROGRESS, 0)
    listings_left = listings.get(Status.PENDING, 0) + listings.get(Status.IN_PROGRESS, 0)

    speed = await rate_per_minute("products_done")
    heartbeat = await redis.get(key("worker", "heartbeat"))
    proxy_stats = await redis.get(key("proxy", "stats"))
    finished_at = await redis.get(key("finished_at"))
    counters = await redis.mget([key("stat", n) for n in ("requests", "blocked", "net_errors", "failures")])

    async with Session() as s:
        last_parsed = await s.scalar(select(func.max(Product.parsed_at)))

    return {
        "running": (await redis.get(RUNNING_KEY)) == b"1",
        "worker": json.loads(heartbeat) if heartbeat else None,
        "finished": bool(finished_at) and remaining == 0 and listings_left == 0,
        "discovery_in_progress": listings_left > 0,
        "products": {
            "total": total,
            "done": done,
            "remaining": remaining,
            "errors": products.get(Status.ERROR, 0),
            "gone": products.get(Status.GONE, 0),
            "skipped_not_product": products.get(Status.SKIPPED, 0),
            "by_status": products,
            "percent": round(done / total * 100, 2) if total else 0,
        },
        "listings": {
            "total": sum(listings.values()),
            "done": listings.get(Status.DONE, 0),
            "remaining": listings_left,
            "errors": listings.get(Status.ERROR, 0),
            "by_kind": by_kind,
        },
        "speed": {
            "products_per_min": round(speed, 1),
            "requests_per_min": round(await rate_per_minute("requests"), 1),
            "blocked_per_min": round(await rate_per_minute("blocked"), 1),
            "failures_per_min": round(await rate_per_minute("failures"), 1),
        },
        "eta_minutes": round(remaining / speed, 1) if speed else None,
        "proxies": json.loads(proxy_stats) if proxy_stats else None,
        "totals": dict(
            zip(("requests", "blocked", "net_errors", "failures"), (int(c or 0) for c in counters), strict=True)
        ),
        "last_parsed_at": last_parsed,
    }


@app.post("/api/parser/start")
async def start():
    await redis.set(RUNNING_KEY, "1")
    return {"running": True}


@app.post("/api/parser/stop")
async def stop():
    await redis.set(RUNNING_KEY, "0")
    return {"running": False}


@app.post("/api/proxies/reset-bans")
async def reset_proxy_bans():
    """Снять кулдауны со всех прокси (воркер подхватит в течение ~3 секунд)."""
    await redis.set(RESET_BANS_KEY, "1", ex=60)
    return {"ok": True}


@app.post("/api/parser/retry-errors")
async def retry_errors():
    async with Session() as s:
        res_p = await s.execute(
            update(Product).where(Product.status == Status.ERROR).values(status=Status.PENDING, attempts=0)
        )
        res_l = await s.execute(
            update(Listing).where(Listing.status == Status.ERROR).values(status=Status.PENDING, attempts=0)
        )
        await s.commit()
    return {"products": res_p.rowcount, "listings": res_l.rowcount}


@app.post("/api/parser/restart")
async def restart():
    """Полный перепарс: всё обратно в очередь, найденные товары сохраняются и обновятся."""
    async with Session() as s:
        await s.execute(update(Listing).values(status=Status.PENDING, attempts=0, error=None))
        await s.execute(
            update(Product)
            .where(Product.status != Status.SKIPPED)
            .values(status=Status.PENDING, attempts=0, error=None)
        )
        await s.commit()
    await redis.set(SEED_KEY, "1")
    await redis.set(RUNNING_KEY, "1")
    return {"ok": True}


@app.get("/api/products")
async def products(
    limit: int = Query(50, le=500),
    offset: int = 0,
    q: str | None = None,
    status: str | None = Status.DONE,
):
    stmt = select(Product).options(defer(Product.html), defer(Product.card))
    if status:
        stmt = stmt.where(Product.status == status)
    if q:
        stmt = stmt.where(or_(Product.name.ilike(f"%{q}%"), Product.sku == q, Product.url.ilike(f"%{q}%")))
    stmt = stmt.order_by(Product.parsed_at.desc().nullslast(), Product.id.desc()).limit(limit).offset(offset)
    async with Session() as s:
        rows = (await s.scalars(stmt)).all()
    return [_product_dict(p) for p in rows]


@app.get("/api/products/{product_id}")
async def product(product_id: int):
    async with Session() as s:
        p = await s.get(Product, product_id, options=[defer(Product.html)])
    if not p:
        raise HTTPException(404)
    return _product_dict(p) | {"card": p.card}


@app.get("/api/errors")
async def errors(limit: int = Query(50, le=500)):
    async with Session() as s:
        prods = (
            (
                await s.execute(
                    select(Product.id, Product.url, Product.status, Product.attempts, Product.error)
                    .where(Product.error.is_not(None), Product.status.in_([Status.ERROR, Status.PENDING]))
                    .order_by(Product.id.desc())
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        lists = (
            (
                await s.execute(
                    select(Listing.id, Listing.url, Listing.kind, Listing.status, Listing.attempts, Listing.error)
                    .where(Listing.error.is_not(None))
                    .order_by(Listing.id.desc())
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
    return {"products": prods, "listings": lists}


def _product_dict(p: Product) -> dict:
    return {
        "id": p.id,
        "url": p.url,
        "status": p.status,
        "external_id": p.external_id,
        "sku": p.sku,
        "name": p.name,
        "brand": p.brand,
        "category": p.category,
        "price": float(p.price) if p.price is not None else None,
        "old_price": float(p.old_price) if p.old_price is not None else None,
        "currency": p.currency,
        "availability": p.availability,
        "description": p.description,
        "image": p.image,
        "params": p.params,
        "images": p.images,
        "breadcrumbs": p.breadcrumbs,
        "parsed_at": p.parsed_at,
        "error": p.error,
    }


# ---------- выгрузка ----------


@app.get("/export/xlsx")
async def export_xlsx(
    all: bool = Query(False, description="включить и неспарсенные строки"),
    wide: int = Query(0, ge=0, le=300, description="разложить N частых характеристик по колонкам"),
):
    """Собирает свежий реестр, кладёт копию в шару shared/exports и отдаёт файл."""
    path = await build_xlsx(only_done=not all, wide_params=wide)
    return FileResponse(path, filename=Path(path).name, media_type=XLSX)


@app.get("/api/exports")
async def exports():
    """Готовые выгрузки в файловой шаре."""
    files = sorted(export_dir().glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [
        {
            "name": f.name,
            "size_mb": round(f.stat().st_size / 2**20, 2),
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime)),
            "url": f"/exports/{f.name}",
        }
        for f in files
    ]


@app.get("/exports/{name}")
async def download_export(name: str):
    path = export_dir() / Path(name).name
    if path.suffix != ".xlsx" or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name, media_type=XLSX)


@app.get("/debug/parse")
async def debug_parse(url: str, links: bool = False):
    """Скачать одну страницу через прокси и показать, что из неё достаётся."""
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher(ProxyPool(await load_proxies(redis), redis))
    resp = await _fetcher.get(url)
    page = parse_page(resp.url or url, resp.text)
    out = {
        "url": resp.url,
        "status": resp.status,
        "html_size": len(resp.text),
        "title": page.title,
        "is_product": bool(page.product),
        "product": page.product,
        "product_links_count": len(page.product_links),
        "listing_links_count": len(page.listing_links),
        "expected_total": page.expected_total,
    }
    if links:
        out["product_links"] = sorted(page.product_links)[:300]
        out["listing_links"] = sorted(page.listing_links)[:300]
    return out


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
