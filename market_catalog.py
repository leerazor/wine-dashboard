"""Combine the two store-grade flyers and apply the recorded market prices."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from build_dashboard import HEADERS, PAGE, WineRow, compare, tables, update_page, wine_rows
from naver_prices import read_aliases, read_page
from naver_snapshot import SPECIAL_SIZE
from wine_searcher import Lookup, decimal_of, normalize, wine_searcher_link
from google_prices import canonical_name


ROOT = PAGE.parent.parent
CATALOG = re.compile(r'<script type="application/json" id="wine-catalog">(.*?)</script>', re.S)
SOURCE_RANK = {"google_snippet": 1, "google_ai": 2}


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


def query_plan(rows: list[dict]) -> list[dict]:
    queries = {}
    for row in sorted(rows, key=lambda row: len(row["grades"]) != 1 or row["grades"][0] != "A"):
        for vintage in vintages(row):
            name = query_name(row)
            key = (normalize(name), vintage)
            if key not in queries:
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


def apply_prices(data: dict, previous: list[dict], observations: list[dict], fx: dict) -> None:
    rates = {currency: decimal_of(rate) for currency, rate in fx.get("krw_per_unit", {}).items()}
    rates["KRW"] = Decimal(1)
    existing = defaultdict(list)
    unspecified = defaultdict(list)
    refreshed = defaultdict(list)
    attempted = set()
    searches = {}
    for row in previous:
        key = normalize(row.get("lookup") or row["name"])
        english = normalize(query_name(row))
        for search in row.get("google_searches", []):
            searches[english, search.get("vintage")] = search
        for vintage in row.get("google_checked_vintages", []):
            attempted.add((english, vintage))
        for quote in row.get("search_prices", []):
            if quote.get("source") not in SOURCE_RANK:
                continue
            marked = dict(quote)
            query_vintage = quote.get("query_vintage", row.get("vintage") if not row.get("vintage_options") else quote.get("vintage"))
            attempted.add((english, query_vintage))
            if quote.get("google_url"):
                current = searches.get((english, query_vintage))
                if current is None or quote["checked_at"] >= current["checked_at"]:
                    searches[english, query_vintage] = {"vintage": query_vintage, "url": quote["google_url"], "checked_at": quote["checked_at"]}
            existing[key, quote.get("vintage")].append(marked)
            if not row.get("vintage") and not row.get("vintage_options"):
                unspecified[key].append(marked)
    for observation in observations:
        key = normalize(canonical_name(observation["name"])), observation.get("vintage")
        attempted.add(key)
        if observation.get("google_url"):
            current = searches.get(key)
            if current is None or observation["checked_at"] >= current["checked_at"]:
                searches[key] = {"vintage": key[1], "url": observation["google_url"], "checked_at": observation["checked_at"]}
        if observation.get("quote"):
            if observation["quote"].get("source") not in SOURCE_RANK:
                raise ValueError("Google AI 개요 또는 Google 검색 요약의 가격만 사용할 수 있습니다.")
            refreshed[key].append(observation["quote"])

    def rank(quote):
        return quote["checked_at"], SOURCE_RANK[quote["source"]], quote["scope"] == "worldwide"

    for row in data["rows"]:
        if row.get("search_name"):
            row["search_name"] = canonical_name(row["search_name"])
        english = normalize(query_name(row))
        options = vintages(row)
        korean = normalize(row["lookup"])
        for field in ("market_krw", "market_original", "market_currency", "checked_at", "matched_name", "source",
                      "savings_krw", "discount_pct", "reference_savings_krw", "reference_discount_pct"):
            row[field] = None
        row["comparison_note"] = ""
        selected = []
        for vintage in options:
            new = refreshed[english, vintage]
            old = existing[korean, vintage] if vintage is not None else unspecified[korean] or existing[korean, None]
            candidates = new + old
            if not candidates:
                continue
            selected.append(max(candidates, key=rank))
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
        row["comparison_note"] = " · ".join(notes)
        primary = min(row["search_prices"], key=lambda quote: quote["discount_pct"])
        row["reference_savings_krw"] = primary["savings_krw"]
        row["reference_discount_pct"] = primary["discount_pct"]
        row["assumed_volume"] = row["volume_ml"] is None and not row["pack"] and not SPECIAL_SIZE.search(row["name"])

    rows = data["rows"]
    expected = {(normalize(entry["name"]), entry["vintage"]) for entry in query_plan(rows)}
    data["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["provider"] = "Google 검색"
    data["search_info"] = {"fx": fx, "google_queries": len(attempted & expected), "google_checked": sum(row["google_checked"] for row in rows),
                           "expected_queries": len(expected), "pending_queries": len(expected - attempted),
                           "google_prices": sum(any(q["source"].startswith("google_") for q in row["search_prices"]) for row in rows),
                           "prices": sum(bool(row["search_prices"]) for row in rows)}


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
    parser.add_argument("--query-plan", type=Path)
    args = parser.parse_args()
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
    observations = []
    for path in args.observations:
        with path.open(encoding="utf-8") as source:
            observations.extend(json.loads(line) for line in source if line.strip())
    if args.captures:
        from google_prices import observation

        for path in args.captures:
            with path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    observations.append(observation(item["snapshot"], item.get("match_name", item["name"]), item.get("vintage"),
                                                    item["url"], item["checked_at"]))
    fx = json.loads(args.fx.read_text(encoding="utf-8")) if args.fx else data.get("search_info", {}).get("fx", {})
    apply_prices(data, previous, observations, fx)
    update_page(args.output, data, PAGE)
    if args.query_plan:
        args.query_plan.parent.mkdir(parents=True, exist_ok=True)
        args.query_plan.write_text(json.dumps(query_plan(data["rows"]), ensure_ascii=False), encoding="utf-8")
    print(json.dumps({**data["grade_info"], **data["search_info"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
