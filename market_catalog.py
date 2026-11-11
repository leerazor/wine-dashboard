"""Combine the two store-grade flyers and apply the recorded market prices."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from build_dashboard import HEADERS, PAGE, WineRow, compare, tables, update_page, wine_rows
from naver_prices import read_aliases, read_page
from naver_snapshot import SPECIAL_SIZE
from wine_searcher import Lookup, decimal_of, normalize, wine_searcher_link
from google_prices import canonical_name, observation as parse_observation, source_market


ROOT = PAGE.parent.parent
CATALOG = re.compile(r'<script type="application/json" id="wine-catalog">(.*?)</script>', re.S)
GOOGLE_SOURCES = {"google_snippet", "google_ai"}
WORLDWIDE_SCOPES = {"worldwide", "ai_worldwide"}
NEARBY_VINTAGE_YEARS = 3


def offer_key(row: WineRow) -> tuple:
    return (normalize(row.lookup), row.vintage, tuple(sorted(row.vintage_options)),
            row.volume, row.pack, row.price, row.card, normalize(row.section), normalize(row.terms))


def vintages(row: dict) -> list[str | None]:
    return row.get("vintage_options") or [row.get("vintage")]


def query_name(row: dict) -> str:
    return canonical_name(row.get("search_name") or row.get("lookup") or row["name"])


def catalog_metadata() -> dict:
    with (ROOT / "grade_catalog.tsv").open(encoding="utf-8", newline="") as source:
        return {normalize(name): (english, kind, country, region)
                for name, english, kind, country, region in csv.reader(source, delimiter="\t")}


def combine(a_path: Path, b_path: Path, base: Path) -> tuple[dict, list[dict]]:
    data = read_page(base)
    embedded = CATALOG.search(base.read_text(encoding="utf-8"))
    legacy_catalog = json.loads(embedded[1]) if embedded else {}
    aliases = {normalize(name): english for name, english in read_aliases(ROOT / "wine_aliases.tsv").items()}
    products = catalog_metadata()

    previous = data["rows"]
    known_catalog = {}
    for row in previous:
        metadata = legacy_catalog.get(row["name"]) or [row.get("type"), row.get("country"), row.get("region")]
        if metadata[0] and metadata[0] != "unknown":
            known_catalog.setdefault(normalize(row["lookup"]), metadata)

    offers = {}
    counts = {}
    for grade, path in (("B", b_path), ("A", a_path)):
        items = wine_rows(tables(path, HEADERS, {"wine", "price"}), False)
        counts[grade] = len(items)
        for item in items:
            key = offer_key(item)
            if key not in offers:
                row = compare(item, Lookup("awaiting_prices", "Google 검색 가격 수집 대기"), Decimal(1))
                korean = key[0]
                english, kind, country, region = products.get(korean, ("", "", "", ""))
                match = legacy_catalog.get(row["name"]) or known_catalog.get(korean)
                if match and not kind:
                    kind, country, region = match
                english = english or aliases.get(korean, item.lookup)
                if re.search(r"[가-힣]", english):
                    english = ""
                row.update({"id": f"{grade}!{item.row}", "type": kind or "unknown",
                            "country": country or "", "region": region or "", "offer_sources": [],
                            "pack": item.pack, "search_name": english,
                            "search_url": wine_searcher_link(english, item.vintage) if english else row["url"]})
                if not kind and item.pack:
                    row["type"] = "mixed"
                offers[key] = row
            offers[key]["offer_sources"].append({"grade": grade, "location": item.origin})

    data["rows"] = list(offers.values())
    for row in data["rows"]:
        row["grades"] = sorted({source["grade"] for source in row["offer_sources"]})
    data["grade_info"] = {"source_counts": counts, "offer_counts": dict(Counter("/".join(r["grades"]) for r in data["rows"]))}
    return data, previous


def query_plan(rows: list[dict], *, unpriced_only: bool = False) -> list[dict]:
    queries = {}
    priced = {(normalize(query_name(row)), vintage) for row in rows for vintage in vintages(row)
              if any(vintage is None or quote.get("compare_vintage", quote.get("vintage")) == vintage
                     for quote in row.get("search_prices", []))} if unpriced_only else set()
    for row in sorted(rows, key=lambda row: len(row["grades"]) != 1 or row["grades"][0] != "A"):
        for vintage in vintages(row):
            name = query_name(row)
            key = (normalize(name), vintage)
            if key not in queries and key not in priced:
                queries[key] = {"name": name, "vintage": vintage}
    return list(queries.values())


def priced_quote(quote: dict, rates: dict, event: float) -> dict:
    price = decimal_of(quote["price"])
    high = decimal_of(quote.get("price_high"))
    if price is None or (high is not None and high < price):
        raise ValueError(f"평균가가 유효하지 않습니다 ({quote['name']}).")
    currency = quote["currency"]
    rate = rates.get(currency)
    if rate is None and (quote.get("krw") is None or high is not None):
        raise ValueError(f"{currency}의 원화 환율이 필요합니다 ({quote['name']}).")
    krw = int((price * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if rate else quote["krw"]
    if not krw or krw <= 0:
        raise ValueError(f"원화로 환산한 평균가가 유효하지 않습니다 ({quote['name']}).")
    result = dict(quote, krw=krw, savings_krw=int(krw - event), discount_pct=round((krw - event) / krw * 100, 2))
    if high is not None:
        krw_high = int((high * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        result.update(krw_high=krw_high, savings_krw_high=int(krw_high - event),
                      discount_pct_high=round((krw_high - event) / krw_high * 100, 2))
    return result


def reference_market(quote: dict) -> str:
    if quote.get("scope") in WORLDWIDE_SCOPES:
        return ""
    return source_market(quote.get("url") or "")


def reference_match(quote: dict, vintage: str | None) -> tuple[str, int | None] | None:
    actual = quote.get("vintage")
    if vintage is None:
        return "event_unspecified", None
    if actual == vintage:
        return "exact", None
    if quote["source"] != "google_snippet":
        return None
    if actual is None:
        return "unspecified", None
    if re.fullmatch(r"\d{4}", vintage) and re.fullmatch(r"\d{4}", actual):
        gap = abs(int(actual) - int(vintage))
        if gap <= NEARBY_VINTAGE_YEARS:
            return "nearby", gap
    return None


def select_reference(quotes: list[dict], vintage: str | None) -> dict | None:
    best = None
    best_rank = None
    for quote in quotes:
        match = reference_match(quote, vintage)
        if match is None:
            continue
        match_type, gap = match
        actual = quote.get("vintage")
        market = reference_market(quote)
        if vintage is None:
            vintage_rank = int(actual is None)
            release_year = int(actual) if actual and re.fullmatch(r"\d{4}", actual) else 0
        else:
            vintage_rank = {"exact": 2, "nearby": 1, "unspecified": 0}[match_type]
            release_year = 0
        rank = (not market, quote["source"] == "google_snippet", vintage_rank,
                -(gap or 0), release_year, quote.get("scope") in WORLDWIDE_SCOPES, quote["checked_at"])
        if best_rank is None or rank > best_rank:
            best = dict(quote, compare_vintage=vintage, match_type=match_type, vintage_gap=gap,
                        reference_market=market)
            best_rank = rank
    return best


def apply_prices(data: dict, previous: list[dict], observations: list[dict], fx: dict) -> None:
    rates = {currency: decimal_of(rate) for currency, rate in fx.get("krw_per_unit", {}).items()}
    rates["KRW"] = Decimal(1)
    references = defaultdict(list)
    attempted = set()
    searches = {}
    missing_fx = set()

    def remember_search(name, vintage, url, checked_at):
        key = name, vintage
        attempted.add(key)
        if url and (key not in searches or checked_at >= searches[key]["checked_at"]):
            searches[key] = {"vintage": vintage, "url": url, "checked_at": checked_at}

    def can_convert(quote):
        currency = quote["currency"]
        if rates.get(currency) or (quote.get("krw") and quote.get("price_high") is None):
            return True
        missing_fx.add(currency)
        return False

    for row in previous:
        english = normalize(query_name(row))
        for search in row.get("google_searches", []):
            remember_search(english, search.get("vintage"), search.get("url"), search["checked_at"])
        for vintage in row.get("google_checked_vintages", []):
            attempted.add((english, vintage))
        for quote in row.get("search_prices", []):
            if quote.get("source") not in GOOGLE_SOURCES or not can_convert(quote):
                continue
            query_vintage = quote.get("query_vintage", row.get("vintage") if not row.get("vintage_options") else quote.get("vintage"))
            remember_search(english, query_vintage, quote.get("google_url"), quote["checked_at"])
            references[english].append(quote)
    for observation in observations:
        english = normalize(canonical_name(observation["name"]))
        remember_search(english, observation.get("vintage"), observation.get("google_url"), observation["checked_at"])
        candidates = list(observation.get("organic_quotes") or [])
        selected_quote = observation.get("quote")
        if selected_quote and selected_quote not in candidates:
            candidates.append(selected_quote)
        for quote in candidates:
            if quote.get("source") not in GOOGLE_SOURCES:
                raise ValueError("Google AI 개요 또는 Google 검색 요약의 가격만 사용할 수 있습니다.")
            if not reference_market(quote) and can_convert(quote):
                references[english].append(quote)

    for row in data["rows"]:
        if row.get("search_name"):
            row["search_name"] = canonical_name(row["search_name"])
        english = normalize(query_name(row))
        options = vintages(row)
        for field in ("market_krw", "market_original", "market_currency", "checked_at", "matched_name", "source",
                      "savings_krw", "discount_pct", "reference_savings_krw", "reference_discount_pct"):
            row[field] = None
        row["comparison_note"] = ""
        candidates = references[english]
        selected = [reference for vintage in options if (reference := select_reference(candidates, vintage))]
        row["search_prices"] = [priced_quote(quote, rates, row["event_krw"]) for quote in selected]
        row["google_checked_vintages"] = [vintage for vintage in options if (english, vintage) in attempted]
        row["google_searches"] = [searches[english, vintage] for vintage in options if (english, vintage) in searches]
        row["google_checked"] = all((english, vintage) in attempted for vintage in options)
        row["status"] = "search_reference" if selected else "no_average" if row["google_checked"] else "awaiting_prices"
        row["search_detail"] = (
            "묶음·선택 상품의 단품 가격 확인 필요" if row["pack"] else
            "영문 상품명 확인 필요" if not row["search_name"] else
            "Google 검색에서 일치하는 세전 750mL 가격 미확인" if row["google_checked"] else
            "Google 검색 가격 수집 대기"
        ) if not selected else ""
        if not selected:
            continue
        notes = []
        if not row.get("vintage"):
            notes.append("복수 빈티지" if row.get("vintage_options") else "행사 빈티지 미표기")
        if row["pack"] or SPECIAL_SIZE.search(row["name"]) or row["volume_ml"] not in (None, 750):
            notes.append("행사 용량·구성 차이")
        if any(quote.get("citation_note") for quote in selected):
            notes.append("AI 인용 빈티지 확인 필요")
        if any(quote["match_type"] == "nearby" for quote in selected):
            notes.append("다른 빈티지 비교")
        if any(quote["match_type"] == "unspecified" for quote in selected):
            notes.append("검색가 빈티지 미지정")
        if any(quote["reference_market"] for quote in selected):
            notes.append("지역 조건 가격")
        row["comparison_note"] = " · ".join(notes)
        primary = min(row["search_prices"], key=lambda quote: quote["discount_pct"])
        row["reference_savings_krw"] = primary["savings_krw"]
        row["reference_discount_pct"] = primary["discount_pct"]
        row["assumed_volume"] = row["volume_ml"] is None and not row["pack"] and not SPECIAL_SIZE.search(row["name"])

    rows = data["rows"]
    expected = {(normalize(entry["name"]), entry["vintage"]) for entry in query_plan(rows)}
    data["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["provider"] = "Google 검색"
    quotes = [quote for row in rows for quote in row["search_prices"]]
    data["search_info"] = {"fx": fx, "google_queries": len(attempted & expected), "google_checked": sum(row["google_checked"] for row in rows),
                           "expected_queries": len(expected), "pending_queries": len(expected - attempted),
                           "google_prices": sum(any(q["source"].startswith("google_") for q in row["search_prices"]) for row in rows),
                           "prices": sum(bool(row["search_prices"]) for row in rows),
                           "quote_sources": dict(Counter(quote["source"] for quote in quotes)),
                           "match_types": dict(Counter(quote["match_type"] for quote in quotes)),
                           "nearby_vintage_years": NEARBY_VINTAGE_YEARS}
    if missing_fx:
        data["search_info"]["missing_fx"] = sorted(missing_fx)


def read_jsonl(paths: list[Path]):
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    yield json.loads(line)


def parse_capture(item: dict) -> dict:
    return parse_observation(item["snapshot"], item.get("match_name", item["name"]), item.get("vintage"),
                             item["url"], item["checked_at"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grade-a", type=Path)
    parser.add_argument("--grade-b", type=Path)
    parser.add_argument("--refresh-page", action="store_true", help="현재 대시보드의 목록을 유지하고 Google 가격 갱신")
    parser.add_argument("--base", type=Path, default=PAGE)
    parser.add_argument("--output", type=Path, default=PAGE)
    parser.add_argument("--fx", type=Path, help="원화 환율 JSON. 생략하면 기준 대시보드의 환율을 사용합니다.")
    parser.add_argument("--observations", type=Path, nargs="+", default=[])
    parser.add_argument("--captures", type=Path, nargs="+", default=[], help="브라우저에서 저장한 Google 검색 결과 JSONL")
    parser.add_argument("--workers", type=int, default=1, help="검색 기록 판독에 사용할 프로세스 수")
    parser.add_argument("--query-plan", type=Path)
    parser.add_argument("--unpriced-only", action="store_true", help="조회 계획에 평균가가 없는 상품·빈티지만 포함")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers는 1 이상이어야 합니다.")
    if args.refresh_page:
        if args.grade_a or args.grade_b:
            parser.error("--refresh-page와 PDF 입력을 함께 지정할 수 없습니다.")
        data = read_page(args.base)
        previous = data["rows"]
        products = catalog_metadata()
        for row in data["rows"]:
            if row.get("search_name"):
                continue
            metadata = products.get(normalize(row["lookup"]))
            if metadata and metadata[0]:
                row["search_name"], row["type"], row["country"], row["region"] = metadata
    else:
        if not (args.grade_a and args.grade_b):
            parser.error("두 등급의 PDF를 지정하거나 --refresh-page를 사용하세요.")
        data, previous = combine(args.grade_a, args.grade_b, args.base)
    observations = list(read_jsonl(args.observations))
    if args.captures:
        captures = read_jsonl(args.captures)
        if args.workers == 1:
            observations.extend(map(parse_capture, captures))
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                observations.extend(pool.map(parse_capture, captures, chunksize=12))
    fx = json.loads(args.fx.read_text(encoding="utf-8")) if args.fx else data.get("search_info", {}).get("fx", {})
    apply_prices(data, previous, observations, fx)
    update_page(args.output, data, PAGE)
    if args.query_plan:
        args.query_plan.parent.mkdir(parents=True, exist_ok=True)
        args.query_plan.write_text(json.dumps(query_plan(data["rows"], unpriced_only=args.unpriced_only), ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**data["grade_info"], **data["search_info"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
