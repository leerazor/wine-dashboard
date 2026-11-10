"""Match Wine-Searcher prices from Naver search summaries to the event catalog."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from calendar import month_name
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit

from build_dashboard import PACK, PAGE, update_page
from naver_prices import CACHE, open_cache, price_query, read_aliases, read_page
from wine_searcher import decimal_of, normalize, wine_searcher_link


SYMBOLS = {
    "₩": "KRW", "US$": "USD", "CA$": "CAD", "AU$": "AUD", "NZ$": "NZD",
    "HK$": "HKD", "SG$": "SGD", "S$": "SGD", "A$": "AUD", "€": "EUR", "£": "GBP",
}
CURRENCIES = {*SYMBOLS, *SYMBOLS.values(), "CHF", "JPY", "CNY", "ZAR"}
CURRENCY = "|".join(map(re.escape, sorted(CURRENCIES, key=len, reverse=True)))
AVERAGE = re.compile(
    rf"\b(?:most recent\s+)?(?P<scope>(?:global|worldwide)\s+)?(?:avg\.?|average)\s+price\s*\(ex[\s-]*tax\)[\s·:]*"
    rf"(?P<currency>{CURRENCY})\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*/\s*750\s*ml\b", re.I,
)
PRODUCT = re.compile(r"\bfind the best (?:local )?price for\s+(.+?)\.\s*$", re.I)
YEAR = re.compile(r"^(?:18|19|20)\d{2}$")
NV = re.compile(r"\b(?:n\.?v\.?|non[ -]vintage)\b", re.I)
SPECIAL_SIZE = re.compile(r"magnum|매그넘|마그넘|half[ -]?bottle|하프\s*보틀", re.I)
NAME_FILLER = set("a d de del della di da du des le la les el the and et of aus en in los las".split())
NAME_VARIANTS = {"st": "saint", "ste": "saint", "1er": "premier", "1ere": "premier",
                 "imperiale": "imperial", "docg": "doc", "igp": "igt", "cab": "cabernet",
                 "sauv": "sauvignon", "jd": "jadot", "premiere": "premier"}
APPENDED_WORDS = set(
    "australia bordeaux bourgogne burgundy california central champagne chile coast county doc doca "
    "france germany igp igt italy marlborough medoc napa new north oregon pauillac pessac leognan "
    "rioja south spain toscana tuscany united states usa valley zealand producer wine".split()
)
GENERIC_NAMES = {"chateau", "domaine", "maison", "estate", "vineyards", "cellars", "cantina", "red", "blanc"}
MONTHS = {name.lower(): index for index, name in enumerate(month_name) if name}


@dataclass(frozen=True)
class SearchQuote:
    name: str
    vintage: str | None
    price: str
    currency: str
    scope: str
    seller_market: str
    url: str
    checked_at: str
    price_month: str | None


def visible(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", "", value)).split())


def name_words(value: str) -> frozenset[str]:
    value = re.sub(r"[’'‘]", "", value)
    value = re.sub(r"\bg\.?\s+d\.?\s+vajra\b", "gd vajra", value, flags=re.I)
    words = (normalize(word) for word in re.findall(r"[^\W_]+", value))
    return frozenset(NAME_VARIANTS.get(word, word) for word in words if word and word not in NAME_FILLER)


def same_product(expected: frozenset[str], actual: frozenset[str]) -> bool:
    if not expected or not actual or not ((expected & actual) - GENERIC_NAMES - APPENDED_WORDS):
        return False
    if expected == actual:
        return True
    if expected < actual:
        return actual - expected <= APPENDED_WORDS
    return actual < expected and expected - actual <= APPENDED_WORDS


def parse_quote(item: dict, checked_at: str) -> SearchQuote | None:
    url = urlsplit(str(item.get("link", "")))
    if url.scheme != "https" or url.netloc != "www.wine-searcher.com":
        return None
    parts = url.path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "find" or not parts[1]:
        return None
    description = visible(str(item.get("description", "")))
    price = AVERAGE.search(description)
    if price is None:
        return None
    prefix = description[:price.start()].strip()
    product = PRODUCT.search(prefix)
    if product:
        product_name = product[1].split(",", 1)[0].strip()
    else:
        title = visible(str(item.get("title", "")))
        product_name = re.split(r",|\s+\||\s+-\s+Wine-Searcher", title, maxsplit=1)[0].strip()
        title_words = name_words(product_name)
        if not title_words or (prefix and not title_words <= name_words(prefix)):
            return None
    vintage_code = parts[2].upper() if len(parts) >= 3 else "1"
    leading_year = product_name.split(" ", 1)[0]
    if YEAR.fullmatch(vintage_code):
        if leading_year == vintage_code:
            product_name = product_name[len(leading_year):].strip()
        elif product or YEAR.fullmatch(leading_year) or not prefix.startswith(f"{vintage_code} "):
            return None
        vintage = vintage_code
    elif vintage_code == "NV" or (vintage_code == "1" and NV.search(product_name)):
        vintage = "NV"
        product_name = NV.sub("", product_name).strip(" -")
    elif vintage_code == "1" and not YEAR.fullmatch(leading_year):
        vintage = None
    else:
        return None
    currency = price["currency"].upper()
    value = decimal_of(price["amount"])
    if value is None or not 0 < value < 100_000_000:
        return None
    market = re.search(r"\bnear you in ([^.;]{1,75})", description[price.end():], re.I)
    region = market[1].strip() if market else ""
    if not region and len(parts) > 3 and parts[3] != "-":
        region = unquote_plus(parts[3]).title()
    dated = re.match(r"[\s·;]*From\s+([A-Za-z]+)\s+((?:19|20)\d{2})\b", description[price.end():], re.I)
    month = MONTHS.get(dated[1].lower()) if dated else None
    price_month = f"{dated[2]}-{month:02d}" if month else None
    return SearchQuote(product_name, vintage, str(value), SYMBOLS.get(currency, currency),
                       "worldwide" if price["scope"] else "unspecified", region,
                       url._replace(query="", fragment="").geturl(), checked_at, price_month)


def select_quotes(candidates: list[SearchQuote], vintages: list[str]) -> list[SearchQuote]:
    best = {}
    for quote in candidates:
        if vintages and quote.vintage not in vintages:
            continue
        rank = (quote.scope == "worldwide", quote.price_month or "", quote.currency == "KRW",
                len(urlsplit(quote.url).path.split("/")) <= 4)
        previous = best.get(quote.vintage)
        if previous is None or rank > previous[0]:
            best[quote.vintage] = (rank, quote)
    if vintages:
        return [best[vintage][1] for vintage in vintages if vintage in best]
    if not best:
        return []
    order = sorted(best, key=lambda vintage: (vintage is None, vintage == "NV", vintage or ""), reverse=True)
    primary = best[order[0]][1]
    if primary.scope == "worldwide":
        return [primary]
    global_prices = [quote for _, quote in best.values() if quote.scope == "worldwide"]
    if not global_prices:
        return [primary]
    recent_global = max(global_prices, key=lambda quote: (quote.price_month or "", quote.vintage or ""))
    return [primary, recent_global]


def enrich(data: dict, cached: list[tuple[str, str, str]], aliases: dict[str, str], fx: dict) -> dict:
    queries = set()
    by_query = defaultdict(list)
    by_name = defaultdict(list)
    for query, checked_at, raw in cached:
        queries.add(query)
        for item in json.loads(raw):
            quote = parse_quote(item, checked_at)
            if quote:
                by_query[query].append(quote)
                by_name[name_words(quote.name)].append(quote)
    rates = {code: Decimal(str(rate)) for code, rate in fx.get("krw_per_unit", {}).items()}
    rates["KRW"] = Decimal(1)
    stats = Counter()
    for row in data["rows"]:
        key = row["lookup"] or row["name"]
        alias = aliases.get(key, key)
        if alias != key:
            row["search_name"] = alias
            row["search_url"] = wine_searcher_link(alias, row.get("vintage"))
        expected = name_words(alias)
        vintages = row.get("vintage_options") or ([row["vintage"]] if row.get("vintage") else [])
        row_queries = [price_query(name, vintage) for name in dict.fromkeys([alias, key]) for vintage in vintages or [None]]
        candidates = []
        for query in row_queries:
            candidates.extend(quote for quote in by_query[query] if same_product(expected, name_words(quote.name)))
        for words, quotes in by_name.items():
            if same_product(expected, words):
                candidates.extend(quotes)
        pack = bool(PACK.search(row["name"]))
        wine_and_glass = bool(re.search(r"잔\s*패키지", row["name"]))
        matches = [] if pack and not wine_and_glass else select_quotes(candidates, vintages)
        has_query = bool(matches) or any(query in queries for query in row_queries)
        row["search_prices"] = [dict(asdict(quote), krw=(int((Decimal(quote.price) * rates[quote.currency]).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)) if quote.currency in rates else None)) for quote in matches]
        row["search_checked"] = has_query
        row["search_detail"] = ("묶음 상품 · 단품 평균가 확인 필요" if pack else
                                "API 검색 응답 확인 필요" if not has_query else
                                "행사 빈티지의 평균가 미확인" if candidates and not matches else
                                "조건에 맞는 가격 요약을 찾지 못했습니다" if not matches else "")
        row["reference_discount_pct"] = None
        row["reference_savings_krw"] = None
        standard_bottle = not pack and not SPECIAL_SIZE.search(row["name"]) and row.get("volume_ml") in (None, 750)
        month = matches[0].price_month if matches else None
        dated_price_is_old = month and (datetime.fromisoformat(matches[0].checked_at).date() -
                                        date.fromisoformat(f"{month}-01")).days > 184
        if dated_price_is_old:
            row["search_detail"] = "가격 기준 월이 6개월 이상 이전입니다"
        if matches and row.get("vintage") and standard_bottle and row.get("event_krw") and not dated_price_is_old:
            krw = row["search_prices"][0]["krw"]
            if krw:
                savings = krw - row["event_krw"]
                row["reference_discount_pct"] = round(savings / krw * 100, 2)
                row["reference_savings_krw"] = savings
        stats["searched"] += has_query
        stats["prices"] += bool(matches)
        stats["reference_comparisons"] += row["reference_discount_pct"] is not None
        stats["worldwide"] += any(quote.scope == "worldwide" for quote in matches)
    data["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["search_info"] = {**stats, "queries": len(queries), "fx": fx}
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=Path, default=PAGE)
    parser.add_argument("--output", type=Path, default=PAGE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--aliases", type=Path, required=True)
    parser.add_argument("--fx", type=Path, help="기준일, 출처와 통화별 원화 환율이 담긴 JSON")
    args = parser.parse_args()
    with closing(open_cache(args.cache)) as database:
        cached = database.execute("SELECT query, checked_at, items FROM searches ORDER BY checked_at DESC").fetchall()
    if not cached:
        parser.error("조회 결과가 없습니다")
    fx = json.loads(args.fx.read_text(encoding="utf-8")) if args.fx else {}
    data = enrich(read_page(args.page), cached, read_aliases(args.aliases), fx)
    update_page(args.output, data, args.page)
    print(json.dumps(data["search_info"], ensure_ascii=False))


if __name__ == "__main__":
    main()
