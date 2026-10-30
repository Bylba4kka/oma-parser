import logging
import time

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import RedisError

from app.settings import settings

log = logging.getLogger(__name__)

redis = Redis(
    connection_pool=BlockingConnectionPool(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_user,
        password=settings.redis_user_password,
        max_connections=settings.redis_max_connections,
        timeout=30,
    ),
    decode_responses=False,
)


def key(*parts: str) -> str:
    return ":".join((settings.redis_prefix, *parts))


RUNNING_KEY = key("running")
SEED_KEY = key("seed")
RESET_BANS_KEY = key("proxy", "reset_bans")

BUCKET = 10


async def count(name: str, n: int = 1) -> None:
    bucket = key("stat", name, str(int(time.time()) // BUCKET))
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incrby(key("stat", name), n)
            pipe.incrby(bucket, n)
            pipe.expire(bucket, 900)
            await pipe.execute()
    except RedisError as e:
        log.warning("Не удалось обновить счётчик %s: %r", name, e)


async def rate_per_minute(name: str, window: int = 60) -> float:
    now = int(time.time()) // BUCKET
    buckets = window // BUCKET
    keys = [key("stat", name, str(now - i)) for i in range(1, buckets + 1)]
    values = await redis.mget(keys)
    return sum(int(v) for v in values if v) * 60 / window
