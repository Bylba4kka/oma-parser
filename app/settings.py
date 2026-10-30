from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "oma"
    postgres_user: str = "oma"
    postgres_password: str = "oma"
    postgres_echo: bool = False
    db_pool_size: int = 20
    db_max_overflow: int = 20
    db_pool_timeout: int = 120
    db_flush_interval: float = 0.5
    db_buffer_limit: int = 5000

    redis_host: str = "localhost"
    redis_port: int = 6380
    redis_user: str | None = None
    redis_user_password: str | None = None
    redis_prefix: str = "oma"
    redis_max_connections: int = 50

    proxys_io_api_key: str = ""
    proxys_io_via_proxy: str = ""
    proxy_refresh_seconds: int = 600
    proxies: str = ""
    proxies_file: str = "shared/proxies.txt"
    proxy_max_concurrent: int = 4
    proxy_min_interval: float = 0.25
    proxy_ban_seconds: int = 120

    base_url: str = "https://www.oma.by"
    impersonate: str = "firefox147"
    cookies: str = ""
    concurrency: int = 400
    request_timeout: int = 30
    max_attempts: int = 6
    autostart: bool = True
    store_html: bool = False
    product_url_re: str = r"-p/?$"
    skip_url_re: str = r"-f/?$"
    listing_url_re: str = r"-c/?$|/catalog/?$"
    listing_share: float = 0.15
    exclude_prefixes: str = (
        "/news/,/buyers/,/about/,/partners/,/reviews/,/personal/,/listovka/,"
        "/contacts/,/b2b/,/search/,/wishlist/,/basket/,/order/,/auth/,/local/,"
        "/upload/,/bitrix/,/compare/,/api-v2/"
    )
    max_pages_per_listing: int = 1000

    # app
    title: str = "OMA.by parser"
    shared_dir: str = "shared"
    auto_export: bool = True

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def exclude_prefix_list(self) -> list[str]:
        return [p.strip() for p in self.exclude_prefixes.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
