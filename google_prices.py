"""Collect exact-product Wine-Searcher references from visible Google results."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote_plus, urlsplit

from naver_snapshot import APPENDED_WORDS, AVERAGE as SNIPPET_AVERAGE, SYMBOLS, name_words, parse_quote
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
PRODUCER_WORDS = {"chateau", "domaine", "maison", "estate", "vineyards", "vineyard", "winery", "wines", "cantina", "cellars",
                  "castello", "weingut", "kellerei", "co", "company", "figli", "vignoble"}
GEOGRAPHY = APPENDED_WORDS | set("veneto friuli isonzo venezia giulia istriana appellation docg cote beaune nuits sauternes barsac menfi barossa".split())
IDENTITY_ALIASES = {
    normalize("Dal Forno Romano Amarone della Valpolicella"): ("Dal Forno Romano Vigneto Monte Lodoletta Amarone della Valpolicella DOCG Italy",),
    normalize("Dal Forno Romano Valpolicella Superiore"): ("Dal Forno Romano Vigneto Monte Lodoletta Valpolicella Superiore",),
    normalize("Les Tourelles de Longueville"): ("Les Tourelles de Pichon Baron",),
    normalize("Clos Quebrada de Macul Domus Aurea"): ("Vina Quebrada de Macul Domus Aurea Cabernet Sauvignon",),
    normalize("Antinori Solaia"): ("Marchesi Antinori Solaia Toscana IGT",),
    normalize("Chateau La Tour Carnet"): ("Bernard Magrez Chateau La Tour Carnet",),
    normalize("The Prisoner Cabernet Sauvignon"): ("The Prisoner Wine Co Cabernet Sauvignon",),
    normalize("Ferraton Pere et Fils Ermitage Les Miaux"): ("Ferraton Pere & Fils Hermitage Les Miaux",),
    normalize("Bava Rosetta"): ("Bava Rosetta Malvasia di Castelnuovo Don Bosco",),
    normalize("Francois Martenot Chablis Montflory"): ("Francois Martenot Chablis Monflory",),
    normalize("Domaine Berthenet Bourgogne Chardonnay Pur Sang"): ("Domaine Berthenet Bourgogne Chardonnay Pur Sans",),
    normalize("Henri de Boursaulx Chablis"): ("Henry de Boursaulx Chablis",),
    normalize("Jean-Marie Fourrier Vigne Comte de Chapelle Meursault Blanc"): ("Jean-Marie Fourrier Vigne Comte de Chapelle Meursault",),
    normalize("Michel Magnien Cote de Nuits Villages Rouge"): ("Domaine Michel Magnien Cote de Nuits-Villages",),
    normalize("Domaine Matrot Auxey-Duresses Rouge"): ("Domaine Matrot Auxey-Duresses",),
    normalize("Domaine Drouhin Oregon Laurene Pinot Noir"): ("Domaine Drouhin Laurene Pinot Noir",),
    normalize("Domaine Drouhin Oregon Arthur Chardonnay"): ("Domaine Drouhin Arthur Chardonnay",),
    normalize("Vidal Fleury Crozes-Hermitage Rouge"): ("J. Vidal-Fleury Crozes-Hermitage",),
    normalize("Vidal Fleury Hermitage Rouge"): ("J. Vidal-Fleury Hermitage",),
    normalize("Andre Clouet Dream Vintage"): ("Andre Clouet Dream Vintage Brut",),
    normalize("Bernard Bremont Cuvee Prestige Grand Cru"): ("Bernard Bremont Cuvee Prestige Brut",),
    normalize("Batasiolo Pinot Chardonnay Spumante Brut"): ("Beni di Batasiolo Pinot Noir Chardonnay Spumante Brut",),
    normalize("Miteruno Mayacaba"): ("Mi Terruno Mayacaba Malbec",),
    normalize("Alibranzia Barolo Poiana"): ("Alibrianza Cascina Radice Poiane Barolo DOCG",),
    normalize("Alibranzia Barbaresco"): ("Alibrianza Barbaresco",),
    normalize("Erickson & Caradine Napa Valley Cabernet Sauvignon"): ("Erikson & Caradin Napa Valley Cabernet Sauvignon",),
    normalize("Erickson & Caradine Napa Valley Chardonnay"): ("Erikson & Caradin Napa Valley Chardonnay",),
    normalize("Harmos Pole"): ("El Chantre Ramos Paul", "Ramos-Paul"),
}
SEARCH_NAME_CORRECTIONS = {key: IDENTITY_ALIASES[key][0] for key in (
    normalize("Francois Martenot Chablis Montflory"), normalize("Domaine Berthenet Bourgogne Chardonnay Pur Sang"),
    normalize("Henri de Boursaulx Chablis"), normalize("Miteruno Mayacaba"), normalize("Alibranzia Barolo Poiana"),
    normalize("Alibranzia Barbaresco"), normalize("Erickson & Caradine Napa Valley Cabernet Sauvignon"),
    normalize("Erickson & Caradine Napa Valley Chardonnay"), normalize("Harmos Pole"))}


def canonical_name(name: str) -> str:
    return SEARCH_NAME_CORRECTIONS.get(normalize(name), name)
SLUG_WORDS = {"cab": "cabernet", "sauv": "sauvignon", "chard": "chardonnay", "dom": "domaine",
              "superior": "superiore", "hand": "hands", "shiraz": "syrah", "rsrv": "reserve"}


def cited_product(url: str, name: str, vintage: str | None, checked_at: str) -> dict | None:
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "find":
        return None
    code = parts[2].upper() if len(parts) > 2 else "1"
    year = code if re.fullmatch(r"(?:18|19|20)\d{2}|NV", code) else None
    if vintage is not None and year not in (None, vintage):
        return None
    slug = " ".join(SLUG_WORDS.get(word, word) for word in unquote_plus(parts[1]).split())
    expected = " ".join(SLUG_WORDS.get(word.lower(), word) for word in name.split())
    if not identity_matches(expected, slug):
        return None
    return {"name": name, "vintage": vintage or year, "currency": "USD", "url": clean_source(url),
            "checked_at": checked_at, "seller_market": "", "price_month": None,
            "citation_note": "AI 인용 페이지의 빈티지 미표기" if vintage and year is None else ""}


def identity_matches(expected: str, actual: str, full_product: str = "") -> bool:
    def words(value: str) -> frozenset[str]:
        value = re.sub(r"\bg[.\s]*d[.\s]+vajra\b", "gd vajra", value, flags=re.I)
        return name_words(value) - PRODUCER_WORDS

    expected_names = (expected, *IDENTITY_ALIASES.get(normalize(expected), ()))
    candidates = (actual, full_product)
    for name in expected_names:
        target = words(name)
        for candidate in candidates:
            found = words(candidate)
            if target and (found == target or (target < found and found - target <= GEOGRAPHY)):
                return True
    return False


def search_url(name: str, vintage: str | None) -> str:
    query = " ".join(part for part in (vintage, name, SUFFIX) if part)
    return "https://www.google.com/search?q=" + quote(query)


def clean_source(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()


def join_text_fragments(lines: list[str]) -> list[str]:
    """Rejoin a snippet split into text/emphasis nodes without crossing links."""
    joined = []
    previous_indent = None
    for line in lines:
        fragment = re.fullmatch(r"(\s*)- (?:text|strong|emphasis):\s*(.+)", line)
        if fragment:
            indent, value = fragment[1], fragment[2].strip('"')
            if previous_indent == indent:
                joined[-1] += " " + value
            else:
                joined.append(f"{indent}- text: {value}")
            previous_indent = indent
        else:
            joined.append(line)
            previous_indent = None
    return joined


def organic_quotes(lines: list[str], name: str, vintage: str | None, checked_at: str) -> list[dict]:
    lines = join_text_fragments(lines)
    quotes = []
    for index, line in enumerate(lines):
        if not re.search(r"(?:avg\.?|average) price\s*\(ex.tax\)", line, re.I) or not UNIT.search(line):
            continue
        block = lines[max(0, index - 9):index]
        source = next((match[0] for context in reversed(block) if (match := WS_LINK.search(context))), "")
        product = next((context[context.lower().index("find the best "):].strip('"')
                        for context in reversed(block) if "find the best " in context.lower()), "")
        match = TEXT.search(line)
        title_line = next((context for context in reversed(block) if 'heading "' in context and '[level=3]' in context), "")
        title = re.sub(r'^.*?heading "|" \[level=3\].*$', "", title_line)
        title = re.sub(r"^Best local price for\s+", "", title, flags=re.I)
        title = re.split(r"\s+-\s+stores near|\s+-\s+Wine-Searcher", title, maxsplit=1, flags=re.I)[0]
        if not (source and match):
            continue
        average = re.sub(r"(?<![\w$])\$(?=\s*\d)", "US$", match[1].strip('"').replace(";", " "))
        result = parse_quote({"link": source, "description": f"{product} {average}", "title": title}, checked_at)
        full_product = re.sub(r"^find the best (?:local )?price for\s+", "", product, flags=re.I)
        full_product = re.sub(r"^(?:18|19|20)\d{2}\s+", "", full_product).rstrip(".")
        if result and (vintage is None or result.vintage == vintage) and identity_matches(name, result.name, full_product):
            quotes.append(asdict(result))
            continue
        # Some Google snippets display just a title and an average-price line.
        year_match = re.fullmatch(r"https://www\.wine-searcher\.com/find/[^/]+/((?:18|19|20)\d{2}|NV|1)(?:/.*)?(?:\?.*)?", source, re.I)
        amount = SNIPPET_AVERAGE.search(average)
        actual = re.sub(r"^(?:18|19|20)\d{2}\s+", "", title)
        if not (year_match and amount and identity_matches(name, actual, full_product)):
            continue
        year = None if year_match[1] == "1" else year_match[1].upper()
        if vintage is not None and year != vintage:
            continue
        quotes.append({"name": actual or name, "vintage": year, "price": amount["amount"].replace(",", ""),
                       "currency": SYMBOLS.get(amount["currency"].upper(), amount["currency"].upper()),
                       "scope": "worldwide" if amount["scope"] else "unspecified", "seller_market": "",
                       "url": clean_source(source), "checked_at": checked_at, "price_month": None})
    return quotes


def overview_quote(lines: list[str], matched: list[dict], name: str, vintage: str | None, checked_at: str) -> dict | None:
    # Validate the displayed identity and price against a cited, matching product page.
    links = {clean_source(match[0]) for line in lines if (match := WS_LINK.search(line))}
    cited = [quote for quote in matched if quote["url"] in links]
    if not cited:
        cited = [candidate for url in sorted(links) if (candidate := cited_product(url, name, vintage, checked_at))]
    if not cited:
        return None
    rendered = [match[1].strip('"') for line in lines if (match := TEXT.search(line))]
    body = " ".join(rendered)
    lead = body[:650]
    if not (TAX.search(lead) and UNIT.search(lead) and AVERAGE.search(lead)):
        return None
    if vintage and not re.search(rf"(?<![\w\d]){re.escape(vintage)}(?![A-Za-z0-9])", lead, re.I):
        return None
    if re.search(r"no (?:published|confirmed|direct|specific) |could not (?:find|verify)|unable to (?:find|verify)|확인할 수 없|찾을 수 없", lead, re.I):
        return None
    headline = USD_PRICE.search(lead)
    if not headline or not AVERAGE.search(lead[:headline.start()]):
        return None
    identity = lead[:headline.start()]
    labels = (name, canonical_name(name), *IDENTITY_ALIASES.get(normalize(name), ()))
    if not any(normalize(label) in normalize(identity) for label in labels) and not identity_matches(name, identity):
        return None
    first_year = re.search(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)", identity)
    if vintage and vintage != "NV" and first_year and first_year[0] != vintage:
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
            "snippet_price": source.get("price"), "snippet_currency": source["currency"] if source.get("price") else None}


def observation(snapshot: str, name: str, vintage: str | None, url: str, checked_at: str) -> dict:
    lines = snapshot.splitlines()
    start = next((i for i, line in enumerate(lines) if OVERVIEW.search(line)), None)
    end = next((i for i, line in enumerate(lines) if (WEB_RESULTS.search(line) or
               (start is not None and re.search(r'^\s*- heading .*\[level=2\]', line))) and
               (start is None or i > start)), len(lines))
    organic = lines[:start] + lines[end:] if start is not None else lines
    matches = organic_quotes(organic, name, vintage, checked_at)
    ai = overview_quote(lines[start + 1:end], matches, name, vintage, checked_at) if start is not None else None
    ordinary = max(matches, key=lambda q: (q["scope"] == "worldwide", q["vintage"] == vintage,
                                           not q["seller_market"]), default=None)
    selected = ai or ({**ordinary, "source": "google_snippet"} if ordinary else None)
    if selected:
        selected["google_url"] = url
        selected["query_vintage"] = vintage
    return {"name": name, "vintage": vintage, "checked_at": checked_at, "google_url": url, "quote": selected}


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
                    cached.add((normalize(canonical_name(item["name"])), item.get("vintage")))
    pending = [entry for entry in entries if (normalize(canonical_name(entry["name"])), entry.get("vintage")) not in cached]
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
