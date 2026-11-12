"""Match the E-Mart catalog to public AWINE product pages and update the dashboard."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree import ElementTree

from lxml import html

from build_dashboard import PACK as MULTIPACK, PAGE, update_page
from naver_prices import read_page
from wine_searcher import vintage_of


ORIGIN = "https://awine.kr"
SITEMAP = f"{ORIGIN}/sitemap.xml"
BUNDLE_DATA = Path(__file__).with_name("awine_bundles.json")
YEAR = re.compile(r"(?<!\d)(?:18[789]\d|19\d{2}|20[0-3]\d)(?!\d)")
NV = re.compile(r"\bn[. /-]*v\b\.?|논\s*빈티지|non[- ]vintage", re.I)
VOLUME = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})|\d+(?:\.\d+)?)\s*(ml|mℓ|㎖|cl|l|ℓ|밀리리터|리터)(?![a-z])", re.I)
PACK = re.compile(r"세트|패키지|\d+\s*(?:병|본)\s*입|\d+\s*종|\b(?:set|bundle|pack)\b", re.I)
FORMAT = re.compile(r"(?:더블\s*)?(?:매그넘|마그넘)|(?:double\s+)?magnum|하프\s*(?:보틀|병)|half[ -]*bottle|반병", re.I)
MONEY = re.compile(r"(?<![\d,])([\d,]+)\s*원")
EVENT_LABEL = re.compile(r"\[(?:행사|프로모션|특가|[^\]]*에디션|(?:non[- ]?)?case)[^\]]*\]|\([^)]*(?:r\.?p\.?|w\.?s\.?|제임스서클링)\s*\d+[^)]*\)", re.I)
GIFT_BOX = re.compile(r"[\[(]\s*(?:case|케이스|gift\s*box)(?:\s*포함)?\s*[\])]", re.I)
BORDEAUX_CLASS = re.compile(r"그랑\s*크뤼\s*[1-5]\s*(?:st|nd|rd|th|등급)|grand\s+cru\s+classe(?:\s+[1-5](?:st|nd|rd|th))?", re.I)
SHORT_YEAR = re.compile(r"(?<=[가-힣)])\s*'?([0-2]\d|[6-9]\d)\s*$")
NUMBERED_CUVEE = re.compile(r"(?:빈|넘버|로트|시리즈)\s*\d+\s*$")
EN_STOP = set("a an and of by de del delle della di da du d la le les des el los the et chateau domaine dom maison winery wine wines vineyard vineyards estate tenuta cantina cantine bodega bodegas vina new event promotion nv non case bottle".split())
KO_PREFIX = re.compile(r"^(?:샤토|도멘|메종|와이너리|테누타|보데가스?|비냐)")
KO_SPELLING = {
    "샤또": "샤토", "무똥": "무통", "까베르네": "카베르네", "까베네": "카베르네",
    "쇼비뇽": "소비뇽", "쏘비뇽": "소비뇽", "쏘비농": "소비뇽", "소비농": "소비뇽",
    "샤도네이": "샤르도네", "샤르도네이": "샤르도네", "피노누아르": "피노누아",
    "클라우드베이": "클라우디베이", "뀌베": "퀴베", "스폐셜": "스페셜",
    "브륏": "브뤼", "볼렝저": "볼랭저", "뵈브끌리꼬": "뵈브클리코", "끌리코": "클리코",
    "꽁쎄이앙뜨": "꽁세이앙뜨", "뻬데스클로": "페데스클로", "스튜아트": "스튜어트",
    "퀴베드라": "퀴베라", "끼안티": "키안티", "상파뉴": "샹파뉴", "세라": "쎄라",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def cleaned(value: str) -> str:
    text = fold(EVENT_LABEL.sub(" ", GIFT_BOX.sub(" ", value)))
    text = VOLUME.sub(" ", text)
    text = FORMAT.sub(" ", BORDEAUX_CLASS.sub(" ", text))
    text = NV.sub(" ", YEAR.sub(" ", text))
    return text if NUMBERED_CUVEE.search(text) else SHORT_YEAR.sub("", text)


def english_words(value: str) -> tuple[str, ...]:
    text = re.sub(r"\bv\.?\s*v\b\.?", "vieilles vignes", cleaned(value))
    text = re.sub(r"\blussots\b", "lussot", text)
    return tuple(word for word in re.findall(r"[a-z]+|\d+", text) if word not in EN_STOP)


def korean_key(value: str) -> str:
    key = "".join(re.findall(r"[가-힣]+", cleaned(value)))
    for before, after in KO_SPELLING.items():
        key = key.replace(before, after)
    return KO_PREFIX.sub("", key)


def trigrams(value: str) -> frozenset[str]:
    return frozenset(value[i:i + 3] for i in range(len(value) - 2))


@dataclass(frozen=True)
class WineName:
    ko: str
    en: frozenset[str]
    grams: frozenset[str]

    @classmethod
    def from_titles(cls, korean: str, english: str) -> WineName:
        ko = korean_key(korean)
        return cls(ko, frozenset(english_words(english)), trigrams(ko))


def bottle_size(text: str) -> int | None:
    matches = []
    for number, unit in VOLUME.findall(fold(text)):
        amount = float(number.replace(",", ""))
        scale = 1000 if unit in {"l", "ℓ", "리터"} else 10 if unit == "cl" else 1
        if 90 <= amount * scale <= 30000:
            matches.append(int(round(amount * scale)))
    if matches:
        return matches[0] if len(set(matches)) == 1 else None
    if re.search(r"더블\s*(?:매그넘|마그넘)|double\s+magnum", text, re.I):
        return 3000
    if re.search(r"매그넘|마그넘|\bmagnum\b", text, re.I):
        return 1500
    if re.search(r"하프\s*(?:보틀|병)|half[ -]*bottle|반병", text, re.I):
        return 375
    return None


def product_vintage(korean: str, english: str) -> str | None:
    korean = EVENT_LABEL.sub(" ", korean)
    english = EVENT_LABEL.sub(" ", english)
    for title in (korean, english):
        years = set(YEAR.findall(VOLUME.sub(" ", title)))
        if len(years) == 1:
            return years.pop()
        if len(years) > 1:
            return None
    short_title = BORDEAUX_CLASS.sub("", korean).strip().replace("()", "")
    short_year = None if NUMBERED_CUVEE.search(short_title) else SHORT_YEAR.search(short_title)
    if short_year:
        year = int(short_year[1])
        return str((2000 if year < 30 else 1900) + year)
    return "NV" if NV.search(f"{korean} {english}") else None


def awine_url(value: str, prefix: str) -> str | None:
    p = urlsplit(value)
    if p.scheme != "https" or p.netloc != "awine.kr" or not p.path.startswith(prefix) or p.query or p.fragment:
        return None
    path = quote(unquote(p.path), safe="/!$&'()*+,;=:@-._~")
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


class Fetcher:
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = Lock()
        self.next_request = 0.0

    def get(self, url: str) -> tuple[bytes, str]:
        with self.lock:
            delay = self.next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_request = time.monotonic() + self.interval
        response = subprocess.run(
            ["curl", "--silent", "--show-error", "--location", "--compressed", "--proto", "=https",
             "--max-time", "22", "--retry", "1", "--retry-max-time", "28",
             "--write-out", "\n%{http_code} %{url_effective}", url],
            capture_output=True, check=False,
        )
        if response.returncode:
            raise OSError(response.stderr.decode(errors="replace").strip())
        body, result = response.stdout.rsplit(b"\n", 1)
        status, final_url = result.decode().split(" ", 1)
        if status != "200":
            raise OSError(f"HTTP {status}")
        return body, final_url


def read_sitemap(path: Path, url: str, fetcher: Fetcher) -> ElementTree.Element:
    if path.exists():
        try:
            return ElementTree.parse(path).getroot()
        except ElementTree.ParseError:
            pass
    payload, _ = fetcher.get(url)
    root = ElementTree.fromstring(payload)
    path.write_bytes(payload)
    return root


def catalog_urls(directory: Path, fetcher: Fetcher) -> dict[str, str]:
    root = read_sitemap(directory / "sitemap.xml", SITEMAP, fetcher)
    sitemaps = [node.text for node in root.findall("./{*}sitemap/{*}loc")
                if node.text and awine_url(node.text, "/wine-sitemap")]
    if not sitemaps:
        raise ValueError("에이와인 와인 사이트맵을 찾을 수 없습니다.")
    urls = {}
    for sitemap in sitemaps:
        page = read_sitemap(directory / Path(urlsplit(sitemap).path).name, sitemap, fetcher)
        for item in page.findall("./{*}url"):
            url = awine_url(item.findtext("{*}loc", ""), "/wine/")
            if url:
                urls[url] = item.findtext("{*}lastmod", "")
    return urls


def name_similarity(first: WineName, second: WineName) -> tuple[float, float]:
    shared = len(first.en & second.en)
    en = 0.68 * shared / max(1, len(first.en)) + 0.32 * shared / max(1, len(second.en)) if shared else 0.0
    if shared < min(2, len(first.en)):
        en *= 0.63
    if len(first.ko) >= 4 and first.ko == second.ko:
        ko = 1.04
    elif min(len(first.ko), len(second.ko)) >= 5 and (first.ko in second.ko or second.ko in first.ko):
        ko = 0.84 + 0.12 * min(len(first.ko), len(second.ko)) / max(len(first.ko), len(second.ko))
    else:
        ko = 2 * len(first.grams & second.grams) / max(1, len(first.grams) + len(second.grams))
    return en, ko


class Catalog:
    def __init__(self, urls: dict[str, str]):
        self.urls = urls
        self.names = {}
        self.words = defaultdict(set)
        self.grams = defaultdict(set)
        self.years = {}
        self.sizes = {}
        for url in urls:
            slug = unquote(urlsplit(url).path).strip("/").split("/")[-1].replace("-", " ")
            name = self.names[url] = WineName.from_titles(slug, slug)
            self.years[url] = set(YEAR.findall(slug))
            self.sizes[url] = bottle_size(slug)
            for word in name.en:
                self.words[word].add(url)
            for gram in name.grams:
                self.grams[gram].add(url)

    def candidates(self, row: dict, limit: int, *, minimum: float = 0.53) -> list[str]:
        name = row_name(row)
        possible = set()
        for word in name.en:
            if len(self.words.get(word, ())) < 2400:
                possible.update(self.words.get(word, ()))
        for gram in name.grams:
            if len(self.grams.get(gram, ())) < 1200:
                possible.update(self.grams.get(gram, ()))
        targets = set(row.get("vintage_options") or [row.get("vintage")]) - {None, "NV"}
        event_size = event_bottle_size(row)
        size = event_size or 750
        matches = []
        for url in possible:
            en, ko = name_similarity(name, self.names[url])
            confidence = max(en, ko) + 0.09 * min(en, ko)
            if confidence < minimum:
                continue
            size_agrees = self.sizes[url] in (None, size)
            exact_year = bool(targets & self.years[url])
            score = confidence + 0.065 * exact_year - 0.16 * (not size_agrees)
            matches.append((score, self.urls[url], url, confidence))
        matches.sort(reverse=True)
        selected = [url for hint in row.get("awine_urls", []) if (url := awine_url(hint, "/wine/"))]
        selected.extend(url for _, _, url, _ in matches[:limit])
        for year in sorted(targets):
            exact = [url for _, _, url, confidence in matches if confidence >= 0.74 and year in self.years[url]]
            selected.extend(exact[:2])
        if event_size:
            formats = [url for _, _, url, confidence in matches if confidence >= 0.73 and self.sizes[url] == size]
            selected.extend(formats[:2])
        return list(dict.fromkeys(selected))


def row_name(row: dict) -> WineName:
    return WineName.from_titles(row.get("lookup") or row["name"], row.get("search_name") or "")


def event_bottle_size(row: dict) -> int | None:
    return bottle_size(row["name"]) or (None if row.get("assumed_volume") else row.get("volume_ml"))


def node_text(node) -> str:
    return " ".join(node.text_content().split())


def read_product(body: bytes, url: str) -> dict:
    document = html.fromstring(body, parser=html.HTMLParser(encoding="utf-8"))
    heading = document.xpath("//main//h1[@id='kor_title']")
    if not heading:
        raise ValueError("상품명 확인 실패")
    section = heading[0].getparent()
    korean = node_text(heading[0])
    english = (section.xpath("string(h2[@id='eng_title'])") or "").strip()
    schema = {}
    for script in document.xpath("//script[@type='application/ld+json']"):
        try:
            value = json.loads(script.text or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("@type") == "Product":
            schema = value
            break
    fields = {item.get("name"): str(item.get("value") or "") for item in schema.get("additionalProperty", [])
              if isinstance(item, dict)}
    price_box = section.xpath(".//p[@id='price']")
    price_text = node_text(price_box[0]) if price_box else ""
    amounts = [int(value.replace(",", "")) for value in MONEY.findall(price_text)]
    offer = schema.get("offers") or {}
    structured = str(offer.get("price") or "") if isinstance(offer, dict) else ""
    current = int(structured) if structured.isdecimal() and int(structured) in amounts else next(iter(amounts), 0)
    statuses = section.xpath(".//*[contains(concat(' ',normalize-space(@class),' '),' stock_status ')]")
    stock_note = ""
    for item in statuses:
        text = " ".join(" ".join(item.xpath(".//text()[not(ancestor::button)]")).split())
        if text:
            stock_note = text[:130]
            break
    buttons = {node_text(button) for button in section.xpath(".//button")}
    if re.search(r"품절|재입고 알림|입고 알림", stock_note) or any("입고 알림" in button for button in buttons):
        stock = "sold_out"
    elif "픽업" in stock_note:
        stock = "pickup"
    elif any("장바구니 담기" in button for button in buttons):
        stock = "cart"
    elif any("문의" in button for button in buttons):
        stock = "inquiry"
    else:
        stock = "unknown"
    brand = schema.get("brand") or {}
    title = f"{korean} {english}"
    vintage = product_vintage(korean, english)
    if vintage is None and re.search(r"\bNV\s+(?:샴페인|스파클링)|논빈티지\s+(?:샴페인|스파클링)", section.xpath("string(../blockquote)"), re.I):
        vintage = "NV"
    return {"url": url, "name": korean, "english": english, "vintage": vintage,
            "volume_ml": bottle_size(title) or bottle_size(fields.get("용량", "")),
            "pack": bool(PACK.search(title) or MULTIPACK.search(title)),
            "type": fields.get("와인타입", ""), "descriptors": " ".join([
                brand.get("name", "") if isinstance(brand, dict) else str(brand),
                fields.get("생산지역", ""), fields.get("포도품종", "")]),
            "price_krw": current or None, "stock": stock, "stock_note": stock_note,
            "has_case": bool(GIFT_BOX.search(korean) or "케이스 포함" in korean), "checked_at": now()}


def fetch_product(url: str, fetcher: Fetcher) -> tuple[str, dict | None, str]:
    try:
        body, final = fetcher.get(url)
        canonical = awine_url(final, "/wine/")
        if canonical is None:
            raise ValueError("에이와인 상품 주소 확인 실패")
        return url, read_product(body, canonical), ""
    except (OSError, ValueError) as error:
        if str(error) in {"HTTP 404", "HTTP 410"}:
            return url, {"missing": True, "checked_at": now()}, ""
        return url, None, str(error)[:170]


def read_cache(path: Path) -> dict[str, dict]:
    cached = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            product = entry["product"]
            if not product.get("missing"):
                if product["vintage"] is None:
                    product["vintage"] = product_vintage(product["name"], product["english"])
                title = f"{product['name']} {product['english']}"
                product["pack"] = bool(PACK.search(title) or MULTIPACK.search(title))
                product["has_case"] = bool(GIFT_BOX.search(product["name"]) or "케이스 포함" in product["name"])
            cached[entry["requested_url"]] = product
    return cached


def collect(urls: list[str], cache: Path, fetcher: Fetcher, workers: int) -> tuple[dict[str, dict], Counter]:
    products = read_cache(cache)
    pending = [url for url in urls if url not in products]
    print(f"AWINE 후보 {len(urls):,}개 · 캐시 {len(urls) - len(pending):,}개 · 조회 {len(pending):,}개", flush=True)
    errors = Counter()
    with cache.open("a", encoding="utf-8") as saved, ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(pending), 80):
            batch = pending[start:start + 80]
            for url, product, error in pool.map(lambda url: fetch_product(url, fetcher), batch):
                if product is None:
                    errors[error] += 1
                    continue
                products[url] = product
                saved.write(json.dumps({"requested_url": url, "product": product}, ensure_ascii=False) + "\n")
            saved.flush()
            done = min(start + len(batch), len(pending))
            print(f"AWINE 상세 {done:,}/{len(pending):,} · 응답 실패 {sum(errors.values()):,}", flush=True)
            if errors["HTTP 429"] >= 3 or errors["HTTP 403"] >= 12:
                print("AWINE 접근 오류가 반복되어 상세 조회를 종료했습니다.", flush=True)
                break
    return products, errors


def identity(row: dict, product: dict, event_name: WineName) -> bool:
    if bool(row.get("pack")) != product["pack"]:
        return False
    actual_type = {"레드와인": "red", "화이트와인": "white", "로제와인": "rose",
                   "샴페인": "sparkling", "스파클링와인": "sparkling"}.get(product["type"].replace(" ", ""))
    if actual_type and row.get("type") in {"red", "white", "rose", "sparkling"} and actual_type != row["type"]:
        return False
    name = WineName.from_titles(product["name"], product["english"])
    if row.get("pack"):
        event_pack = re.sub(r"\W+", "", cleaned(row.get("lookup") or row["name"]))
        awine_pack = re.sub(r"\W+", "", cleaned(product["name"]))
        return len(event_pack) >= 8 and event_pack == awine_pack
    common = event_name.en & name.en
    ko_exact = event_name.ko == name.ko and (len(event_name.ko) >= 4 or (len(event_name.ko) >= 2 and any(len(word) >= 4 for word in common)))
    if ko_exact:
        return True
    en, ko = name_similarity(event_name, name)
    korean_agrees = (not event_name.ko or not name.ko or ko >= 0.28 or
                     SequenceMatcher(None, event_name.ko, name.ko).ratio() >= 0.62)
    if not korean_agrees:
        return False
    if len(event_name.en) >= 2 and event_name.en == name.en:
        return True
    left, right = event_name.en - name.en, name.en - event_name.en
    descriptors = set(english_words(product["descriptors"]))
    descriptors.update({"red": {"red", "rouge", "rosso", "tinto"}, "white": {"white", "blanc", "bianco", "blanco"},
                        "sparkling": {"brut", "champagne"}}.get(actual_type, set()))
    if len(common) >= 2 and (not left or not right) and (left | right) <= descriptors:
        return True
    if ko >= 0.93 and min(len(event_name.ko), len(name.ko)) >= 8:
        extra = (name.ko.replace(event_name.ko, "", 1) if event_name.ko in name.ko
                 else event_name.ko.replace(name.ko, "", 1) if name.ko in event_name.ko else "")
        return bool(extra) and extra in {"브뤼", "와인", "레드와인", "화이트와인", "샴페인"} and en >= 0.57
    return False


def vintage_match(target: str | None, actual: str | None) -> tuple[str, int] | None:
    if target == actual:
        return ("exact" if target else "unspecified"), 0
    if target is None:
        return ("nv" if actual == "NV" else "event_unspecified"), 0
    if actual is None:
        return "unspecified", 0
    if target.isdecimal() and actual.isdecimal():
        gap = abs(int(target) - int(actual))
        if gap <= 3:
            return "nearby", gap
    return None


def best_offer(row: dict, products: list[dict], vintage: str | None) -> dict | None:
    event_size = event_bottle_size(row)
    size = event_size or 750
    rankings = {"exact": 4, "nv": 4, "nearby": 3, "event_unspecified": 2, "unspecified": 1}
    eligible = []
    for product in products:
        actual_size = product["volume_ml"]
        if (actual_size or 750) != size or (size != 750 and actual_size is None):
            continue
        match = vintage_match(vintage, product["vintage"])
        if match is None or not product["price_krw"]:
            continue
        kind, gap = match
        stock = product["stock"]
        rank = (rankings[kind], -gap, stock in {"cart", "pickup"}, stock != "sold_out",
                not product["has_case"], -product["price_krw"], product["url"])
        eligible.append((rank, kind, product))
    if not eligible:
        return None
    _, kind, product = max(eligible, key=lambda item: item[0])
    notes = []
    if kind == "nearby":
        notes.append(f"행사 {vintage} · 에이와인 {product['vintage']}")
    elif kind == "event_unspecified":
        notes.append(f"행사 빈티지 미표기 · 에이와인 {product['vintage']}")
    elif kind == "unspecified":
        notes.append("에이와인 빈티지 미표기")
    if not event_size or not product["volume_ml"]:
        notes.append("750mL 가정")
    if product["has_case"]:
        notes.append("케이스 포함")
    price = product["price_krw"]
    event_price = row.get("event_krw")
    card_price = row.get("card_krw")
    stock_note = product["stock_note"] if not re.search(r"수량\s*:|총액\s*:", product["stock_note"]) else ""
    return {key: product[key] for key in ("name", "url", "vintage", "volume_ml", "price_krw", "stock", "stock_note", "checked_at")} | {
        "event_vintage": vintage, "match": kind, "notes": notes, "stock_note": stock_note,
        "event_delta": round(price - event_price) if event_price is not None else None,
        "card_delta": round(price - card_price) if card_price is not None else None,
    }


def compare(row: dict, urls: list[str], products: dict[str, dict]) -> dict:
    name = row_name(row)
    candidates = [products[url] for url in urls if url in products]
    matched = [product for product in candidates if not product.get("missing") and identity(row, product, name)]
    vintages = [vintage_of(year) for year in row.get("vintage_options") or [row.get("vintage")]]
    offers = [offer for year in vintages if (offer := best_offer(row, matched, year))]
    if offers:
        offers.sort(key=lambda offer: (offer["match"] not in {"exact", "nv"}, offer["stock"] == "sold_out", offer["price_krw"]))
        return {"status": "priced", "offers": offers}
    if len(candidates) < len(urls):
        return {"status": "lookup_error"}
    if matched:
        candidate = min(matched, key=lambda product: (product["volume_ml"] not in (None, event_bottle_size(row) or 750),
                         product["vintage"] not in vintages, not product["price_krw"]))
        return {"status": "incompatible" if candidate["price_krw"] else "price_hidden", "url": candidate["url"]}
    return {"status": "not_matched"}


def bundle_components(row: dict, bundle: dict) -> list[dict]:
    parts = bundle["components"]
    if parts and sum(part["quantity"] for part in parts) != bundle["bottles"]:
        raise ValueError(f"세트 병 수 불일치: {row['name']}")
    return [part | {"id": f"{row['id']}:bundle:{index}", "volume_ml": bundle["volume_ml"]}
            for index, part in enumerate(parts)]


def compare_bundle(row: dict, bundle: dict, parts: list[dict], plan: dict, products: dict[str, dict]) -> dict:
    components = []
    for part in parts:
        result = compare(part, plan[part["id"]], products)
        component = {key: part.get(key) for key in ("name", "vintage", "quantity")}
        component["status"] = result["status"]
        if result.get("offers"):
            component["offer"] = result["offers"][0]
        elif result.get("url"):
            component["url"] = result["url"]
        components.append(component)
    details = {"bottles": bundle["bottles"], "volume_ml": bundle["volume_ml"], "components": components}
    if not components:
        return {"status": "bundle_unknown", "bundle": details}
    if any("offer" not in component for component in components):
        return {"status": "bundle_incomplete", "bundle": details}

    price = sum(part["offer"]["price_krw"] * part["quantity"] for part in components)
    offers = [part["offer"] for part in components]
    reference = any(item["match"] not in {"exact", "nv"} or not item["volume_ml"] or item["stock"] == "sold_out"
                    for item in offers)
    total = {"name": row["name"], "price_krw": price, "match": "bundle",
             "checked_at": max(item["checked_at"] for item in offers),
             "notes": ["참고 합계" if reference else "구성품 합계"]}
    for kind in ("event", "card"):
        original = row.get(f"{kind}_krw")
        total[f"{kind}_delta"] = round(price - original) if original is not None else None
    return {"status": "priced", "offers": [total], "bundle": details | {"reference": reference}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=Path, default=PAGE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cache", type=Path, required=True, help="이번 조회의 사이트맵·상품 상세 캐시 디렉터리")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--expand", type=int, default=8, help="동일 빈티지·규격을 찾지 못한 행의 후보 수")
    parser.add_argument("--plan", action="store_true", help="상품 상세 조회 없이 후보 수만 확인")
    parser.add_argument("--cached-only", action="store_true", help="이미 저장된 상품 상세로 대시보드 생성")
    parser.add_argument("--bundles-only", action="store_true", help="구성품이 있는 세트 상품의 에이와인 가격만 갱신")
    args = parser.parse_args()
    if min(args.workers, args.candidates, args.expand) < 1:
        parser.error("작업자 수와 후보 수는 1 이상이어야 합니다.")
    args.cache.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(interval=0.19)
    data = read_page(args.page)
    bundles = json.loads(BUNDLE_DATA.read_text(encoding="utf-8"))
    bundle_rows = {row["id"]: row for row in data["rows"] if row.get("pack") and row["name"] in bundles}
    component_rows = {row_id: bundle_components(row, bundles[row["name"]]) for row_id, row in bundle_rows.items()}
    regular_rows = [] if args.bundles_only else [row for row in data["rows"] if row["id"] not in bundle_rows]
    for row in regular_rows:
        if not row.get("pack") and (size := bottle_size(row["name"])):
            row.update(volume_ml=size, assumed_volume=False)
    query_rows = regular_rows + [part for parts in component_rows.values() for part in parts]
    if args.bundles_only and not bundle_rows:
        parser.error("이 대시보드에 에이와인 세트 비교 대상이 없습니다.")
    source_urls = catalog_urls(args.cache, fetcher)
    catalog = Catalog(source_urls)
    plan = {row["id"]: catalog.candidates(row, args.candidates) for row in query_rows}
    requested = list(dict.fromkeys(url for candidates in plan.values() for url in candidates))
    print(f"사이트맵 {len(source_urls):,}개 상품 · 조회 대상 {len(plan):,}개 · 상세 후보 {len(requested):,}개", flush=True)
    if args.plan:
        return
    cache = args.cache / "products.jsonl"
    products, errors = (read_cache(cache), Counter()) if args.cached_only else collect(requested, cache, fetcher, args.workers)
    if args.expand > args.candidates and errors["HTTP 429"] < 3 and errors["HTTP 403"] < 12:
        expanded = {}
        for row in query_rows:
            candidate_urls = plan[row["id"]]
            result = compare(row, candidate_urls, products)
            offers = result.get("offers", [])
            required = len(row.get("vintage_options") or [row.get("vintage")])
            missing_vintage = any(offer["event_vintage"] and offer["match"] in {"nearby", "unspecified"} for offer in offers)
            if len(offers) < required or missing_vintage:
                expanded[row["id"]] = list(dict.fromkeys(candidate_urls + catalog.candidates(row, args.expand, minimum=0.69)))
        plan.update(expanded)
        requested = list(dict.fromkeys(url for candidates in plan.values() for url in candidates))
        if not args.cached_only:
            products, next_errors = collect(requested, cache, fetcher, args.workers)
            errors.update(next_errors)
    for row in regular_rows:
        row["awine"] = compare(row, plan[row["id"]], products)
    for row_id, row in bundle_rows.items():
        row["awine"] = compare_bundle(row, bundles[row["name"]], component_rows[row_id], plan, products)
    counts = Counter(row["awine"]["status"] for row in data["rows"] if "awine" in row)
    observed = [products[url]["checked_at"] for url in requested if url in products and not products[url].get("missing")]
    missing = sum(bool(products.get(url, {}).get("missing")) for url in requested)
    if requested and not observed and not args.cached_only:
        raise RuntimeError("에이와인 상품 상세를 수집하지 못했습니다.")
    stats = {"checked_at": max(observed, default=now()), "catalog_products": len(source_urls),
             "candidate_pages": len(requested), "pages_read": len(observed), "pages_removed": missing,
             "rows": len(regular_rows) + len(bundle_rows), "status_counts": dict(counts), "fetch_errors": sum(errors.values())}
    if args.bundles_only and data.get("awine_info"):
        stats["status_counts"] = dict(Counter(row["awine"]["status"] for row in bundle_rows.values()))
        data["awine_info"].update(bundles=stats, status_counts=dict(counts))
    else:
        data["awine_info"] = stats
    update_page(args.output or args.page, data, args.page)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    if errors:
        print("AWINE 상세 조회 오류", dict(errors), flush=True)


if __name__ == "__main__":
    main()
