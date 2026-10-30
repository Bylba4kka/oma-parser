# OMA.by parser

Парсит весь каталог [oma.by](https://www.oma.by) http + curl_cffi + прокси (https://proxys.io). Сохраняет товары в PostgreSQL. FastAPI-приложение показывает прогресс и выгружает весь реестр в Excel.

## Запуск

```bash
cp .env.example .env
echo "PROXYS_IO_API_KEY=ваш_токен" >> .env

docker compose up --build
```

Дашборд: **http://localhost:8080**
Swagger: http://localhost:8080/docs


## Файловая шара
- `shared/exports/*.xlsx` — выгрузки реестра, попадает, после парсинга


## API

| Метод | Что делает |
|---|---|
| `GET /api/stats` | прогресс: всего / спарсено / осталось / ошибки, скорость, ETA, состояние прокси |
| `POST /api/parser/start` · `stop` | старт / пауза |
| `POST /api/proxies/reset-bans` | снять кулдауны со всех прокси без перезапуска воркера |
| `POST /api/parser/retry-errors` | вернуть ошибочные в очередь |
| `POST /api/parser/restart` | полный перепарс каталога |
| `GET /api/products?q=&limit=&offset=` | товары; `GET /api/products/{id}` — карточка целиком |
| `GET /api/errors` | последние ошибки |
| `GET /export/xlsx` | скачать реестр (`?wide=50` — 50 самых частых характеристик отдельными колонками, `?all=true` — включая неспарсенные) |
| `GET /api/exports` | список выгрузок в шаре |
| `GET /debug/parse?url=...&links=true` | скачать одну страницу через прокси и показать, что из неё извлеклось |


## Настройки (.env)

| Переменная | По умолчанию | |
|---|---|---|
| `CONCURRENCY` | 400 | одновременных задач в воркере |
| `PROXY_MAX_CONCURRENT` | 4 | параллельных запросов на один прокси |
| `PROXY_MIN_INTERVAL` | 0.25 | секунд между запросами через один прокси |
| `PROXY_BAN_SECONDS` | 120 | базовый кулдаун после блокировки |
| `LISTING_SHARE` | 0.15 | доля слотов под обход категорий, остальное — товарам |
| `LISTING_URL_RE` | `-c/?$\|/catalog/?$` | какие ссылки из HTML обходить как листинги |
| `MAX_ATTEMPTS` | 6 | попыток на страницу, потом статус `error` |
| `COOKIES` | — | строка Cookie из браузера, если без неё не пускает |
| `PROXYS_IO_API_KEY` | — | **токен proxys.io — источник прокси** |
| `PROXYS_IO_VIA_PROXY` | — | прокси для запроса к API proxys.io, если напрямую не открывается |
| `PROXY_REFRESH_SECONDS` | 600 | как часто обновлять список прокси |
| `PROXIES` | — | запасной список прокси через запятую |
| `STORE_HTML` | false | сохранять сжатый HTML страниц |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 20 / 20 | соединений к Postgres на процесс; сумма по всем процессам должна быть < 300 (`max_connections`) |
