import copy
import gzip
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree, html

from app.settings import settings

BASE = urlsplit(settings.base_url)
HOSTS = {BASE.netloc, BASE.netloc.removeprefix("www."), "www." + BASE.netloc.removeprefix("www.")}
PRODUCT_RE = re.compile(settings.product_url_re)
SKIP_RE = re.compile(settings.skip_url_re) if settings.skip_url_re else None
PAGE_PARAM_RE = re.compile(r"^(PAGEN_\d+|page)$", re.I)
SKIP_EXT = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".pdf",
    ".css",
    ".js",
    ".zip",
    ".rar",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".mp4",
    ".ico",
    ".woff2",
)
SKIP_PARTS = ("/filter/", "/apply/", "/clear/", "/compare", "/basket", "javascript:", "mailto:", "tel:")

PARAM_HINTS = ("char", "propert", "spec", "param", "harakter", "attribute", "feature", "tech-info")
DESCR_HINTS = ("description", "detail-text", "detail_text", "opisanie", "product-text", "product-about")
CARD_HINTS = (
    "product-card",
    "product-item",
    "catalog-item",
    "item-card",
    "goods-item",
    "product_item",
    "catalog-product",
    "products-list",
    "product-tile",
)
WS_RE = re.compile(r"\s+")
NUM_RE = re.compile(r"\d[\d\s  ]*(?:[.,]\d+)?")


@dataclass
class PageResult:
    product: dict | None = None
    product_links: set[str] = field(default_factory=set)
    listing_links: set[str] = field(default_factory=set)
    title: str | None = None
    expected_total: int | None = None


def normalize_url(href: str | None, base: str | None = None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if any(p in href.lower() for p in ("javascript:", "mailto:", "tel:")):
        return None
    absolute = urljoin(base or settings.base_url, href)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in HOSTS:
        return None
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    low = path.lower()
    if low.endswith(SKIP_EXT) or any(p in low for p in SKIP_PARTS):
        return None
    if any(low.startswith(p) for p in settings.exclude_prefix_list):
        return None
    if SKIP_RE and SKIP_RE.search(path):
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query) if PAGE_PARAM_RE.match(k)]
    query = [(k, v) for k, v in query if v not in ("1", "")]
    return urlunsplit((BASE.scheme, BASE.netloc, path, urlencode(query), ""))


def is_product_url(url: str) -> bool:
    return bool(PRODUCT_RE.search(urlsplit(url).path))


def external_id_from_url(url: str) -> str | None:
    m = re.search(r"-p(\d+)/?$", urlsplit(url).path) or re.search(r"(\d{4,})/?$", urlsplit(url).path)
    return m.group(1) if m else None


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = WS_RE.sub(" ", str(text)).strip().replace(" ,", ",")
    return text or None


def to_price(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = NUM_RE.search(str(value))
    if not m:
        return None
    num = re.sub(r"[\s  ]", "", m.group(0)).replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


def block_text(el) -> str | None:
    """Текст элемента с переносами строк на блочных тегах."""
    el = copy.deepcopy(el)
    for bad in el.xpath(".//script|.//style|.//noscript"):
        bad.drop_tree()
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        if node.tag in ("br", "p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "ul", "ol", "table"):
            node.tail = "\n" + (node.tail or "")
    text = el.text_content()
    lines = [WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    out = "\n".join(line for line in lines if line)
    return out or None


def _class_id(el) -> str:
    return ((el.get("class") or "") + " " + (el.get("id") or "")).lower()


def _in_chrome(el) -> bool:
    """Элемент внутри шапки/подвала/меню — там не характеристики товара."""
    for anc in el.iterancestors():
        if anc.tag in ("header", "footer", "nav"):
            return True
        ci = _class_id(anc)
        if "page-header" in ci or "footer" in ci or "mobile-menu" in ci:
            return True
    return False


def _walk_ld(obj, out: list[dict]) -> None:
    if isinstance(obj, list):
        for item in obj:
            _walk_ld(item, out)
    elif isinstance(obj, dict):
        out.append(obj)
        if "@graph" in obj:
            _walk_ld(obj["@graph"], out)


def json_ld(doc) -> list[dict]:
    out: list[dict] = []
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        raw = (script.text or "").strip()
        if not raw:
            continue
        try:
            _walk_ld(json.loads(raw, strict=False), out)
        except ValueError:
            continue
    return out


def _types(obj: dict) -> set[str]:
    t = obj.get("@type")
    if isinstance(t, list):
        return {str(x) for x in t}
    return {str(t)} if t else set()


def microdata_product(doc) -> dict:
    scopes = doc.xpath('//*[@itemscope][contains(@itemtype, "schema.org/Product")]')
    if not scopes:
        return {}
    scope = scopes[0]
    data: dict = {}
    for el in scope.xpath(".//*[@itemprop]"):
        prop = el.get("itemprop")
        value = el.get("content") or el.get("href") or el.get("src") or clean(el.text_content())
        if prop == "image":
            data.setdefault("image", []).append(value)
        elif prop not in data and value:
            data[prop] = value
    return data


def _ld_product(objs: list[dict]) -> dict | None:
    for obj in objs:
        if "Product" in _types(obj) or "IndividualProduct" in _types(obj):
            return obj
    return None


def _breadcrumbs(doc, objs: list[dict]) -> list[str]:
    for obj in objs:
        if "BreadcrumbList" in _types(obj):
            items = sorted(obj.get("itemListElement") or [], key=lambda i: i.get("position", 0))
            names = []
            for item in items:
                inner = item.get("item")
                name = item.get("name") or (inner.get("name") if isinstance(inner, dict) else None)
                if name:
                    names.append(clean(name))
            if names:
                return names
    for container in doc.xpath('//*[contains(@class, "breadcrumb")]'):
        names = [clean(a.text_content()) for a in container.xpath(".//a|.//span[not(*)]")]
        names = [n for n in dict.fromkeys(names) if n]
        if names:
            return names
    return []


def _pairs_from(container) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for tr in container.xpath(".//tr"):
        cells = tr.xpath("./th|./td")
        if len(cells) == 2:
            pairs.append((cells[0].text_content(), cells[1].text_content()))
    for dl in container.xpath(".//dl"):
        for dt in dl.xpath("./dt|./div/dt"):
            dd = dt.getnext()
            if dd is not None and dd.tag == "dd":
                pairs.append((dt.text_content(), dd.text_content()))
    for name_el in container.xpath(
        './/*[contains(@class, "name") or contains(@class, "title")'
        ' or contains(@class, "label") or contains(@class, "key")]'
    ):
        value_el = name_el.getnext()
        if value_el is None:
            continue
        vc = (value_el.get("class") or "").lower()
        if "val" in vc or "value" in vc or "desc" in vc:
            pairs.append((name_el.text_content(), value_el.text_content()))
    return pairs


def extract_params(doc, ld: dict | None) -> dict[str, str]:
    params: dict[str, str] = {}

    def add(k, v):
        k = clean(k)
        v = clean(v)
        if not k or not v:
            return
        k = k.rstrip(":").strip()
        if 0 < len(k) <= 150 and len(v) <= 3000 and k.lower() != v.lower():
            params.setdefault(k, v)

    containers = [
        el
        for el in doc.xpath("//*[@class or @id]")
        if any(h in _class_id(el) for h in PARAM_HINTS) and not _in_chrome(el)
    ]
    for c in containers:
        for k, v in _pairs_from(c):
            add(k, v)

    if not params:
        main = doc.xpath("//main") or doc.xpath("//body")
        for root in main[:1]:
            for k, v in _pairs_from(root):
                if not _in_chrome(root):
                    add(k, v)

    for prop in (ld or {}).get("additionalProperty") or []:
        if isinstance(prop, dict):
            add(prop.get("name"), prop.get("value"))
    return params


def _description(doc, ld: dict | None, micro: dict) -> str | None:
    candidates = []
    for el in doc.xpath("//*[@class or @id]"):
        ci = _class_id(el)
        if any(h in ci for h in DESCR_HINTS) and not _in_chrome(el):
            text = block_text(el)
            if text and len(text) > 20:
                candidates.append(text)
    for el in doc.xpath('//*[@itemprop="description"]'):
        text = el.get("content") or block_text(el)
        if text:
            candidates.append(text)
    if candidates:
        return max(candidates, key=len)
    if ld and ld.get("description"):
        return clean(html.fromstring(f"<div>{ld['description']}</div>").text_content())
    return micro.get("description") or _meta(doc, "description") or _meta(doc, "og:description")


def _meta(doc, name: str) -> str | None:
    vals = doc.xpath(f'//meta[@name="{name}" or @property="{name}"]/@content')
    return clean(vals[0]) if vals else None


def _images(ld: dict | None, micro: dict, doc, url: str) -> list[str]:
    images: list[str] = []
    raw = (ld or {}).get("image")
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl")
        if item:
            images.append(item)
    images += [i for i in micro.get("image", []) if i]
    og = _meta(doc, "og:image")
    if og:
        images.append(og)
    return list(dict.fromkeys(urljoin(url, i) for i in images if isinstance(i, str)))


def _text(el) -> str | None:
    return clean(el.text_content()) if el is not None else None


def oma_card(doc) -> dict:
    """Точная разметка карточки oma.by. Пустой dict — если вёрстка не та."""
    sections = doc.xpath('//section[contains(concat(" ", @class, " "), " js-catalog-item ")]')
    if not sections:
        return {}
    sec = sections[0]
    a = sec.attrib
    data: dict = {
        "name": clean(a.get("data-ga-product-name")),
        "category": clean(a.get("data-ga-category-path")),
        "category_id": a.get("data-ga-category-last-id"),
        "brand": clean(a.get("data-ga-product-brand")),
        "sku": clean(a.get("data-sku")),
        "price": to_price(a.get("data-ga-product-price")),
        "old_price": to_price(a.get("data-ga-product-price-old")),
        "available": a.get("data-available") == "true" if a.get("data-available") else None,
        "country": clean(a.get("data-dy-manufacture")),
        "online_only": a.get("data-product-online-only") == "true",
    }

    raw = html.tostring(sec, encoding="unicode")
    m = re.search(r"js_cat_list_item(\d+)", raw) or re.search(r"PRODUCT_ID=(\d+)", raw, re.IGNORECASE)
    data["product_id"] = m.group(1) if m else None

    unit = sec.xpath('.//*[contains(@class, "product-unit")]')
    data["unit"] = _text(unit[0]).lstrip("/") if unit else None
    bonus = sec.xpath('.//*[contains(@class, "count-bonus_green")]')
    data["bonus"] = _text(bonus[0]) if bonus else None

    groups: list[dict] = []
    for block in sec.xpath(
        './/div[contains(@class, "product-specs-layout")]//div[contains(concat(" ", @class, " "), " product-specs ")]'
    ):
        current = None
        for el in block.iter():
            if not isinstance(el.tag, str):
                continue
            cls = el.get("class") or ""
            if "product-specs-group_title" in cls:
                current = {"group": _text(el), "items": {}}
                groups.append(current)
            elif "product-specs_item" in cls.split():
                name = el.xpath('.//*[contains(@class, "product-specs_name")]')
                value = el.xpath('.//*[contains(@class, "product-specs_value")]')
                if name and value:
                    if current is None:
                        current = {"group": None, "items": {}}
                        groups.append(current)
                    k, v = _text(name[0]), _text(value[0])
                    if k and v:
                        current["items"][k.rstrip(":")] = v
    data["specs_groups"] = [g for g in groups if g["items"]]

    info = {}
    for item in sec.xpath('.//*[contains(@class, "catalog-item-description_footer-item")]'):
        b = item.find(".//b")
        if b is None:
            continue
        key = _text(b).rstrip(":") if _text(b) else None
        value = clean(item.text_content().replace(b.text_content(), "", 1))
        if key and value:
            info[key] = value
    data["info"] = info

    descr = sec.xpath('.//*[contains(@class, "catalog-item-description-txt") and not(contains(@class, "_footer"))]')
    text = None
    if descr:
        el = copy.deepcopy(descr[0])
        for h in el.xpath(".//h2"):
            if clean(h.text_content()) == "Описание":
                h.drop_tree()
        text = block_text(el)
    data["description"] = text

    images = []
    for script in doc.xpath('//*[@data-product-gallery]/script[@type="application/json"]'):
        try:
            gallery = json.loads(script.text or "{}").get("gallery") or []
        except ValueError:
            continue
        for g in gallery:
            if isinstance(g, dict) and (g.get("original") or g.get("image")):
                images.append(g.get("original") or g.get("image"))
        if images:
            break
    data["images"] = images
    return data


def _oma_params(card: dict) -> dict[str, str]:
    params: dict[str, str] = {}
    for g in card.get("specs_groups") or []:
        is_package = "упаков" in (g["group"] or "").lower()
        for k, v in g["items"].items():
            name = f"Упаковка: {k}" if is_package else k
            if name in params and params[name] != v:
                name = f"{g['group']}: {k}"
            params.setdefault(name, v)
    for k, v in (card.get("info") or {}).items():
        params.setdefault(k, v)
    return params


def extract_product(url: str, doc) -> dict | None:
    objs = json_ld(doc)
    ld = _ld_product(objs)
    micro = microdata_product(doc)
    og_type = (_meta(doc, "og:type") or "").lower()
    if not ld and not micro and og_type != "product" and og_type != "og:product":
        return None

    ld = ld or {}
    oma = oma_card(doc)
    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    brand = ld.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    h1 = doc.xpath("//h1")
    name = (
        clean(ld.get("name"))
        or micro.get("name")
        or (clean(h1[0].text_content()) if h1 else None)
        or _meta(doc, "og:title")
    )

    price = to_price(offers.get("price") or offers.get("lowPrice") or micro.get("price"))
    old_price = None
    for el in [] if oma else doc.xpath('//*[contains(@class, "old") and contains(@class, "price")]'):
        old_price = to_price(el.text_content())
        if old_price:
            break
    availability = offers.get("availability") or micro.get("availability")
    if availability:
        availability = str(availability).rsplit("/", 1)[-1]

    breadcrumbs = _breadcrumbs(doc, objs)
    category = " / ".join(b for b in breadcrumbs[1:-1]) if len(breadcrumbs) > 2 else (ld.get("category") or None)

    rating = ld.get("aggregateRating") or {}
    sku = ld.get("sku") or ld.get("mpn") or micro.get("sku")
    if not sku and not oma:
        m = re.search(r"Артикул[:\s]*([\w\-./]+)", doc.text_content() if doc is not None else "")
        sku = m.group(1) if m else None

    card = {
        "json_ld": ld or None,
        "microdata": micro or None,
        "title": clean(doc.findtext(".//title")),
        "meta_description": _meta(doc, "description"),
        "gtin": ld.get("gtin13") or ld.get("gtin") or micro.get("gtin13"),
        "rating": rating.get("ratingValue"),
        "reviews_count": rating.get("reviewCount") or rating.get("ratingCount"),
        "price_valid_until": offers.get("priceValidUntil"),
        "seller": (offers.get("seller") or {}).get("name") if isinstance(offers.get("seller"), dict) else None,
    }

    result = {
        "external_id": str(ld.get("productID") or "") or external_id_from_url(url),
        "sku": str(sku)[:128] if sku else None,
        "name": name,
        "brand": clean(brand),
        "category": category,
        "price": price,
        "old_price": old_price if old_price and price and old_price > price else None,
        "currency": offers.get("priceCurrency") or micro.get("priceCurrency") or ("BYN" if price else None),
        "availability": availability,
        "description": None if oma else _description(doc, ld, micro),
        "params": {} if oma else extract_params(doc, ld),
        "images": _images(ld, micro, doc, url),
        "breadcrumbs": breadcrumbs,
        "card": {k: v for k, v in card.items() if v is not None},
    }

    if oma:
        result.update(
            {
                "external_id": oma["product_id"] or result["external_id"],
                "sku": oma["sku"] or result["sku"],
                "name": result["name"] or oma["name"],
                "brand": oma["brand"] or result["brand"],
                "category": oma["category"] or result["category"],
                "price": oma["price"] if oma["price"] is not None else result["price"],
                "old_price": oma["old_price"] or None,
                "description": oma["description"] or clean(ld.get("description")) or None,
                "params": _oma_params(oma) or result["params"],
                "images": [urljoin(url, i) for i in oma["images"]] or result["images"],
            }
        )
        if oma["available"] is not None and not result["availability"]:
            result["availability"] = "InStock" if oma["available"] else "OutOfStock"
        result["card"].update(
            {
                k: oma[k]
                for k in (
                    "product_id",
                    "category_id",
                    "unit",
                    "bonus",
                    "country",
                    "online_only",
                    "specs_groups",
                    "info",
                )
                if oma.get(k) not in (None, [], {})
            }
        )
    return result


def _looks_like_card_link(a) -> bool:
    for i, anc in enumerate(a.iterancestors()):
        if i > 6:
            break
        ci = (anc.get("class") or "").lower()
        if any(h in ci for h in CARD_HINTS):
            return True
    return False


def _expected_total(doc) -> int | None:
    text = WS_RE.sub(" ", doc.text_content())
    m = re.search(r"(?:[Нн]айдено|[Вв]сего|[Тт]оваров)[:\s]*(\d[\d\s ]{0,8})\s*(?:товар|шт)", text)
    if m:
        try:
            return int(re.sub(r"\D", "", m.group(1)))
        except ValueError:
            return None
    return None


def pagination_links(url: str, links: set[str]) -> set[str]:
    """Достраивает все страницы пагинации до максимальной найденной."""
    base = urlsplit(url)
    best: tuple[str, int] | None = None
    for link in links:
        parts = urlsplit(link)
        if parts.path != base.path:
            continue
        for k, v in parse_qsl(parts.query):
            if PAGE_PARAM_RE.match(k) and v.isdigit():
                if best is None or int(v) > best[1]:
                    best = (k, int(v))
    if not best:
        return set()
    param, last = best
    last = min(last, settings.max_pages_per_listing)
    return {urlunsplit((base.scheme, base.netloc, base.path, urlencode({param: n}), "")) for n in range(2, last + 1)}


def parse_page(url: str, text: str) -> PageResult:
    result = PageResult()
    try:
        doc = html.fromstring(text)
    except (etree.ParserError, ValueError):
        return result
    doc.make_links_absolute(url, resolve_base_href=False, handle_failures="discard")
    result.title = clean(doc.findtext(".//title"))

    try:
        result.product = extract_product(url, doc)
    except Exception as e:  # noqa: BLE001
        result.product = {"_error": f"{type(e).__name__}: {e}"}

    links: set[str] = set()
    for a in doc.xpath("//a[@href]"):
        link = normalize_url(a.get("href"), url)
        if not link or link == url:
            continue
        if is_product_url(link) or (not result.product and _looks_like_card_link(a)):
            result.product_links.add(link)
        else:
            links.add(link)

    if not result.product:
        h1 = doc.xpath("//h1")
        result.title = clean(h1[0].text_content()) if h1 else result.title
        result.expected_total = _expected_total(doc)
        result.listing_links = links | pagination_links(url, links)
    return result


def parse_sitemap(content: bytes) -> tuple[list[str], list[str]]:
    """-> (вложенные sitemap, url страниц)"""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError:
        return [], []
    if root is None:
        return [], []
    sitemaps, urls = [], []
    for loc in root.iter("{*}loc", "loc"):
        parent = etree.QName(loc.getparent()).localname if loc.getparent() is not None else ""
        value = (loc.text or "").strip()
        if not value:
            continue
        (sitemaps if parent == "sitemap" else urls).append(value)
    return sitemaps, urls


def sitemaps_from_robots(text: str) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]


def menu_urls(data) -> list[tuple[str, str | None]]:
    """Рекурсивно собирает (url, name) из json меню каталога любой формы."""
    out: list[tuple[str, str | None]] = []

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name") or node.get("title") or node.get("text")
            for k in ("url", "link", "href", "path", "section_page_url", "SECTION_PAGE_URL"):
                v = node.get(k)
                if isinstance(v, str) and v.startswith(("/", "http")):
                    out.append((v, name if isinstance(name, str) else None))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out
