"""Collect exact-product Wine-Searcher references from visible Google results."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from naver_snapshot import name_words, parse_quote, same_product
from wine_searcher import decimal_of, normalize


SUFFIX = "wine-searcher worldwide avg price ex-tax 750ml"
OVERVIEW = re.compile(r'heading "AI (?:Overview|개요)"', re.I)
WEB_RESULTS = re.compile(r'heading "(?:Web results|웹 결과)"|^\s*- main:', re.I)
GLOBAL = re.compile(r"worldwide|world-wide|global|전\s*/?\s*세계|글로벌", re.I)
TAX = re.compile(r"ex[\s-]*tax|before (?:sales )?tax|excluding tax|세전|세금\s*(?:별도|제외)|세금\s*미포함", re.I)
UNIT = re.compile(r"750\s*ml", re.I)
AVERAGE = re.compile(r"\b(?:avg\.?|average)\b|평균", re.I)
USD_PRICE = re.compile(r"(?<![\w$])(?:US\$|USD\s*|\$)\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(?:[-–—~～]|to)\s*(?:US\$|USD\s*|\$)?\s*(\d[\d,]*(?:\.\d+)?))?", re.I)
BLOCK = re.compile(r"unusual traffic|automated queries|verify you are human|비정상적인 트래픽", re.I)
WS_LINK = re.compile(r"https://www\.wine-searcher\.com/find/[^\s\"<>]+")
TEXT = re.compile(r"- (?:strong|text|emphasis):\s*(.+)")


def search_url(name: str, vintage: str | None) -> str:
    query = " ".join(part for part in (vintage, name, SUFFIX) if part)
    return "https://www.google.com/search?q=" + quote(query)


def clean_source(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()


def organic_quotes(lines: list[str], name: str, vintage: str | None, checked_at: str) -> list[dict]:
    quotes = []
    for index, line in enumerate(lines):
        if not re.search(r"(?:avg\.?|average) price\s*\(ex.tax\)", line, re.I) or not UNIT.search(line):
            continue
        block = lines[max(0, index - 9):index]
        source = next((match[0] for context in reversed(block) if (match := WS_LINK.search(context))), "")
        product = next((context[context.lower().index("find the best "):].strip('"')
                        for context in reversed(block) if "find the best " in context.lower()), "")
        match = TEXT.search(line)
        if not (source and product and match):
            continue
        average = re.sub(r"(?<![\w$])\$(?=\s*\d)", "US$", match[1].strip('"').replace(";", " "))
        result = parse_quote({"link": source, "description": f"{product} {average}"}, checked_at)
        if result and (vintage is None or result.vintage == vintage) and same_product(name_words(name), name_words(result.name)):
            quotes.append(asdict(result))
    return quotes


def overview_quote(lines: list[str], matched: list[dict], name: str, vintage: str | None) -> dict | None:
    # Validate the displayed identity and price against a cited, matching product page.
    links = {clean_source(match[0]) for line in lines if (match := WS_LINK.search(line))}
    cited = [quote for quote in matched if quote["url"] in links]
    if not cited:
        return None
    rendered = [match[1].strip('"') for line in lines if (match := TEXT.search(line))]
    body = " ".join(rendered)
    lead = body[:650]
    if normalize(name) not in normalize(lead) or not (TAX.search(lead) and UNIT.search(lead) and AVERAGE.search(lead)):
        return None
    if vintage and not re.search(rf"(?<![\w\d]){re.escape(vintage)}(?![A-Za-z0-9])", lead, re.I):
        return None
    if re.search(r"no (?:published|confirmed|direct|specific) |could not (?:find|verify)|unable to (?:find|verify)|확인할 수 없|찾을 수 없", lead, re.I):
        return None
    headline = USD_PRICE.search(lead)
    if not headline or not AVERAGE.search(lead[:headline.start()]):
        return None
    scope = "ai_worldwide" if GLOBAL.search(lead[:headline.start()]) else "unspecified"
    chosen = headline
    # A separate global line takes precedence over an opening range that can include local offers.
    for index, line in enumerate(rendered[1:], 1):
        if GLOBAL.search(line) and AVERAGE.search(line) and len(line) < 90:
            fragment = " ".join(rendered[index:index + 4])
            candidate = USD_PRICE.search(fragment)
            if candidate and UNIT.search(fragment[candidate.start():candidate.end() + 30]):
                scope, chosen = "ai_worldwide", candidate
                break
    low, high = decimal_of(chosen[1]), decimal_of(chosen[2])
    if not low or (high is not None and high < low):
        return None
    source = max(cited, key=lambda q: (q["vintage"] == vintage, not q["seller_market"]))
    return {**source, "source": "google_ai", "price": str(low), "price_high": str(high) if high else None,
            "currency": "USD", "scope": scope, "price_month": None, "seller_market": "",
            "snippet_price": source["price"], "snippet_currency": source["currency"]}


def observation(snapshot: str, name: str, vintage: str | None, url: str, checked_at: str) -> dict:
    lines = snapshot.splitlines()
    start = next((i for i, line in enumerate(lines) if OVERVIEW.search(line)), None)
    end = next((i for i, line in enumerate(lines) if WEB_RESULTS.search(line) and (start is None or i > start)), len(lines))
    matches = organic_quotes(lines[end:] if end < len(lines) else lines, name, vintage, checked_at)
    ai = overview_quote(lines[start + 1:end], matches, name, vintage) if start is not None else None
    ordinary = max(matches, key=lambda q: (q["scope"] == "worldwide", q["vintage"] == vintage,
                                           not q["seller_market"]), default=None)
    selected = ai or ({**ordinary, "source": "google_snippet"} if ordinary else None)
    if selected:
        selected["google_url"] = url
    return {"name": name, "vintage": vintage, "checked_at": checked_at, "quote": selected}


def collect(plan: Path, output: Path, limit: int | None, delay: float, executable: Path | None) -> int:
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import TimeoutError as BrowserTimeout
    from playwright.sync_api import sync_playwright

    entries = json.loads(plan.read_text(encoding="utf-8"))
    cached = set()
    if output.exists():
        with output.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    item = json.loads(line)
                    cached.add((normalize(item["name"]), item.get("vintage")))
    pending = [entry for entry in entries if (normalize(entry["name"]), entry.get("vintage")) not in cached]
    if not pending:
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    batch = pending[:limit]
    with sync_playwright() as runtime, output.open("a", encoding="utf-8") as results:
        try:
            browser = runtime.chromium.launch(executable_path=executable)
        except BrowserError as exc:
            print(f"Chromium을 시작하지 못했습니다. {str(exc).splitlines()[0]}")
            return 2
        page = browser.new_page()
        try:
            for index, entry in enumerate(batch, 1):
                name, vintage = entry["name"], entry.get("vintage")
                try:
                    page.goto(search_url(name, vintage), wait_until="domcontentloaded", timeout=20000)
                    try:
                        page.get_by_role("heading", name=re.compile(r"^AI (?:Overview|개요)$", re.I)).first.wait_for(timeout=6500)
                    except BrowserTimeout:
                        pass
                    snapshot = page.locator("body").aria_snapshot(timeout=10000)
                except BrowserTimeout:
                    print("Google 응답 시간 초과. 저장한 결과부터 다음 실행에서 이어갈 수 있습니다.")
                    return 2
                except BrowserError as exc:
                    print(f"Google 검색 페이지를 열지 못했습니다. {str(exc).splitlines()[0]}")
                    return 2
                if "/sorry/" in page.url or BLOCK.search(snapshot):
                    print("Google에서 자동 트래픽 확인을 요청해 수집을 중단했습니다.")
                    return 2
                if not (WEB_RESULTS.search(snapshot) or WS_LINK.search(snapshot)):
                    print("Google 검색 결과 페이지가 확인되지 않아 수집을 중단했습니다.")
                    return 2
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                result = observation(snapshot, name, vintage, page.url, timestamp)
                results.write(json.dumps(result, ensure_ascii=False) + "\n")
                results.flush()
                print(f"{index}/{len(batch)} {vintage or ''} {name} · {'가격 확인' if result['quote'] else '가격 미확인'}", flush=True)
                if index < len(batch):
                    time.sleep(delay)
        finally:
            browser.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query_plan", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".work/google-observations.jsonl"))
    parser.add_argument("--limit", type=int, help="이번 실행에서 새로 검색할 최대 건수")
    parser.add_argument("--delay", type=float, default=2, help="각 검색이 끝난 뒤 대기 시간(초)")
    parser.add_argument("--chromium-path", type=Path, help="Playwright에 설치된 브라우저 대신 기존 Chromium 실행 파일 사용")
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or not 1 <= args.delay <= 60:
        parser.error("--limit은 양수, --delay는 1~60초여야 합니다.")
    raise SystemExit(collect(args.query_plan, args.output, args.limit, args.delay, args.chromium_path))


if __name__ == "__main__":
    main()
