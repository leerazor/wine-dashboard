"""Read the E-Mart price list, look up each wine, and update the static dashboard."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zipfile import BadZipFile

from wine_searcher import Lookup, ProviderError, Quote, WineCheck, decimal_of, normalize, vintage_of, wine_searcher_link


ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "docs" / "index.html"
DATA_BLOCK = re.compile(r'(<script type="application/json" id="wine-data">\s*).*?(\s*</script>)', re.DOTALL)
HEADERS = {
    "wine": ("와인명", "와인명(국문)", "와인명(한글)", "한글명", "상품명", "제품명", "품명", "와인", "wine", "wine name", "product name"),
    "price": ("행사가", "행사가(원)", "이마트 행사가", "이마트 행사가(원)", "이마트 가격", "장터가", "장터가(원)",
              "행사 가격", "행사 가격(원)", "판매가", "판매가(원)", "가격", "가격(원)", "event_price_krw", "event price", "price"),
    "vintage": ("빈티지", "연도", "vintage", "year"),
    "size": ("용량", "용량(mL)", "병 용량", "병 용량(mL)", "bottle_ml", "size", "volume"),
    "lookup": ("Wine-Searcher 검색명", "Wine-Searcher 와인명", "wine_searcher_name", "영문명", "와인명(영문)", "영문 와인명", "english name"),
    "card": ("카드사 적용가", "카드 적용가", "card_price_krw"),
    "section": ("행사 구분", "promotion"),
    "terms": ("판매 조건", "offer_terms"),
    "origin": ("원본 위치", "source_location"),
}
REPORT_HEADERS = {
    "wine": ("Product Name", "Wine Name"),
    "vintage": ("Vintage",),
    "average": ("Global Avg Price", "Global Average Price"),
    "reference": ("Your Refence Code", "Your Reference Code", "Reference Code"),
}
TITLE_VINTAGE = re.compile(r"(?:\s+|\()((?:18|19|20)\d{2}|N\.?V\.?|논빈티지|무빈티지)\)?\s*$", re.IGNORECASE)
LEADING_VINTAGE = re.compile(r"^((?:18|19|20)\d{2})\s+")
SIZE = re.compile(r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(ml|cl|l|㎖|ℓ)", re.IGNORECASE)
PACK = re.compile(r"\b(?:case of|\d+\s*pack)\b|(?:[2-9]|\d{2,})\s*[x×]\s*\d|[x×]\s*(?:[2-9]|\d{2,})\b|"
                  r"(?:[2-9]|\d{2,})\s*(?:병|본입|종)|\s[+＋]\s|병\s*\+|세트|묶음|패키지", re.IGNORECASE)


@dataclass(frozen=True)
class Table:
    title: str
    rows: list[tuple[int, tuple]]
    header: int
    columns: dict[str, int]
    labels: tuple
    is_pdf: bool = False


@dataclass(frozen=True)
class WineRow:
    sheet: str
    row: int
    name: str
    lookup: str
    vintage: str | None
    volume: int | None
    assumed_volume: bool
    pack: bool
    price: Decimal | None
    card: Decimal | None = None
    section: str = ""
    terms: str = ""
    origin: str = ""
    vintage_options: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.sheet}!{self.row}"


def clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def sheets(path: Path):
    if path.suffix.lower() == ".pdf":
        from emart_pdf import pdf_rows

        yield "원본 PDF", list(enumerate(pdf_rows(path), 1))
        return
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as source:
            yield "CSV", [(i, tuple(row)) for i, row in enumerate(csv.reader(source), 1) if any(map(clean, row))]
        return
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("이마트 장터 PDF, 엑셀(.xlsx, .xlsm) 또는 UTF-8 CSV를 지정해 주세요.")
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        with closing(load_workbook(path, read_only=True, data_only=True)) as workbook:
            for sheet in workbook.worksheets:
                yield sheet.title, [(i, row) for i, row in enumerate(sheet.values, 1) if any(map(clean, row))]
    except (BadZipFile, InvalidFileException) as exc:
        raise ValueError("엑셀을 열 수 없습니다. 유효한 .xlsx 또는 .xlsm 파일인지 확인해 주세요.") from exc


def tables(path: Path, aliases: dict, required: set, *, header: int | None = None,
           overrides: dict | None = None, titles: list[str] | None = None) -> list[Table]:
    candidates = []
    selected = set(titles or [])
    overrides = overrides or {}
    required = required | {key for key, value in overrides.items() if value}
    for title, rows in sheets(path):
        if selected and title not in selected:
            continue
        for index, labels in rows:
            if index != header and (header is not None or index > 30):
                continue
            columns = {}
            labels_normalized = [normalize(label) for label in labels]
            for field, names in aliases.items():
                options = (overrides[field],) if overrides.get(field) else names
                hits = [i for name in options for i, label in enumerate(labels_normalized) if label == normalize(name)]
                if hits:
                    columns[field] = hits[0]
            if required <= columns.keys():
                candidates.append(Table(title, rows, index, columns, labels, path.suffix.lower() == ".pdf"))
                break
    if selected and selected != {table.title for table in candidates}:
        missing = selected - {table.title for table in candidates}
        raise ValueError(f"선택한 시트에서 필요한 헤더를 찾을 수 없습니다 ({', '.join(sorted(missing))}).")
    if not candidates:
        raise ValueError("와인명과 가격 등 필요한 헤더를 찾을 수 없습니다. 시트와 헤더 행 또는 열 이름을 직접 지정해 주세요.")
    return candidates


def cell(table: Table, row: tuple, field: str) -> str:
    column = table.columns.get(field)
    return clean(row[column]) if column is not None and column < len(row) else ""


def records(table: Table):
    for number, row in table.rows:
        if number <= table.header:
            continue
        values = {key: cell(table, row, key) for key in table.columns}
        if not any(values.values()):
            continue
        if all(value == clean(table.labels[table.columns[key]]) for key, value in values.items()):
            continue
        yield number, values


def event_price(value: str) -> Decimal | None:
    value = value.upper().removeprefix("KRW").strip().removeprefix("₩").strip()
    if value.endswith("만원"):
        amount = decimal_of(value.removesuffix("만원"))
        return amount * 10000 if amount is not None and amount < 100_000_000 else None
    return decimal_of(value.removesuffix("원"))


def bottle_size(value: str) -> int | None:
    text = value.strip().casefold()
    measured = SIZE.fullmatch(text)
    if measured:
        number = Decimal(measured[1].replace(",", "")) * {"ml": 1, "㎖": 1, "cl": 10, "l": 1000, "ℓ": 1000}[measured[2]]
    else:
        number = decimal_of(text)
    if number is None or not (0 < number <= 30000) or number != int(number):
        return None
    return int(number)


def split_title(value: str) -> tuple[str, str | None]:
    title = re.sub(rf"\s*\(?{SIZE.pattern}\)?\s*$", "", value, flags=re.IGNORECASE).strip()
    match = TITLE_VINTAGE.search(title) or LEADING_VINTAGE.search(title)
    if match:
        return (title[:match.start()] + title[match.end():]).strip(), vintage_of(match[1])
    return title, None


def wine_rows(inputs: list[Table], assume_750: bool) -> list[WineRow]:
    rows = []
    for table in inputs:
        for number, row in records(table):
            name = row.get("wine", "")
            query = row.get("lookup") or name
            raw_vintage = row.get("vintage", "")
            raw_size = row.get("size", "")
            pack = bool(PACK.search(f"{name} {raw_size}"))
            title_size = re.search(rf"(?<!\w){SIZE.pattern}(?!\w)", name, re.IGNORECASE)
            if not raw_size and title_size:
                raw_size = title_size[0]
            if not raw_size and re.search(r"magnum|매그넘|마그넘|half bottle|하프보틀", name, re.IGNORECASE):
                raw_size = "용량 확인 필요"
            vintage = vintage_of(raw_vintage)
            if not table.is_pdf:
                query, named_vintage = split_title(query)
                _, title_vintage = split_title(name)
                lwin = re.fullmatch(r"LWIN\d{7}(\d{4})(?:\d{7})?", query, re.IGNORECASE)
                lwin_vintage = vintage_of(lwin[1]) if lwin else None
                vintages = {named_vintage, title_vintage, lwin_vintage, vintage} - {None}
                vintage = next(iter(vintages)) if len(vintages) == 1 and (not raw_vintage or vintage) else None
            alternatives = tuple(vintage_of(value) for value in re.split(r"[/,]", raw_vintage))
            vintage_options = tuple(dict.fromkeys(alternatives)) if len(alternatives) > 1 and all(alternatives) else ()
            volume = bottle_size(raw_size)
            if volume is not None and title_size and volume != bottle_size(title_size[0]):
                volume = None
            assumed = not raw_size and assume_750 and not pack
            rows.append(WineRow(
                table.title, number, name, query, vintage, 750 if assumed else volume, assumed, pack,
                event_price(row.get("price", "")), event_price(row.get("card", "")), row.get("section", ""),
                row.get("terms", ""), row.get("origin", ""), vintage_options,
            ))
    return rows


def apply_lookup_map(rows: list[WineRow], path: Path) -> list[WineRow]:
    known = {row.id: row for row in rows}
    updated = {}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not {"Your Reference Code", "Product Name", "Vintage"} <= set(reader.fieldnames or []):
            raise ValueError("조회명 CSV에는 Your Reference Code, Product Name, Vintage 열이 필요합니다.")
        for entry in reader:
            code = clean(entry["Your Reference Code"])
            if code not in known or code in updated:
                raise ValueError(f"조회명 CSV의 참조 코드가 원본과 일치하지 않거나 중복입니다 ({code}).")
            row = known[code]
            year = clean(entry["Vintage"])
            vintage = vintage_of(year) if year else row.vintage
            if year and (vintage is None or (row.vintage and vintage != row.vintage)
                         or (row.vintage_options and vintage not in row.vintage_options)):
                raise ValueError(f"조회명 CSV의 빈티지가 원본 표기와 일치하지 않습니다 ({code}).")
            raw_volume = clean(entry.get("Bottle mL"))
            volume = bottle_size(raw_volume) if raw_volume else row.volume
            if raw_volume and (volume is None or (row.volume and not row.assumed_volume and volume != row.volume)):
                raise ValueError(f"조회명 CSV의 용량이 원본 표기와 일치하지 않습니다 ({code}).")
            updated[code] = replace(row, lookup=clean(entry["Product Name"]) or row.lookup, vintage=vintage,
                                    volume=volume, assumed_volume=row.assumed_volume and not raw_volume)
    return [updated.get(row.id, row) for row in rows]


class MarketReport:
    def __init__(self, path: Path, currency: str, report_date: date):
        self.currency = currency
        self.report_date = report_date
        self.by_name = defaultdict(list)
        self.by_reference = defaultdict(list)
        report_tables = tables(path, REPORT_HEADERS, {"wine", "vintage", "average"})
        for table in report_tables:
            for _, row in records(table):
                name, vintage = row["wine"], vintage_of(row["vintage"])
                if not name or vintage is None:
                    continue
                amount = decimal_of(row["average"])
                self.by_name[(normalize(name), vintage)].append((name, vintage, amount))
                if row.get("reference"):
                    self.by_reference[row["reference"]].append((name, vintage, amount))

    def lookup(self, row: WineRow) -> Lookup:
        matches = self.by_reference.get(row.id) or self.by_name.get((normalize(row.lookup), row.vintage), [])
        if not matches:
            return Lookup("no_average", "보고서에서 이 와인과 빈티지의 세계 평균가를 찾지 못했습니다.")
        identities = {(normalize(name), vintage, amount) for name, vintage, amount in matches}
        if len(identities) > 1 or any(name != normalize(row.lookup) or vintage != row.vintage for name, vintage, _ in identities):
            return Lookup("identity_review", "보고서의 와인명, 빈티지 또는 참조 코드가 서로 다릅니다.")
        name, _, amount = matches[0]
        if amount is None:
            return Lookup("no_average", "보고서의 Global Avg Price가 비어 있거나 숫자 형식이 다릅니다.")
        return Lookup("matched", quote=Quote(str(amount), self.currency, self.report_date.isoformat(), name,
                                              "Market Pricing Report"))


class CachedWineCheck:
    def __init__(self, api: WineCheck, cache_path: Path, max_requests: int | None, hours: float):
        self.api = api
        self.max_requests = max_requests
        self.ttl = hours * 3600
        self.started = time.time()
        self.attempts = 0
        self.error = ""
        self.next_request = 0.0
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = sqlite3.connect(cache_path)
        self.cache.execute("CREATE TABLE IF NOT EXISTS lookups (key TEXT PRIMARY KEY, created REAL, result TEXT)")

    def lookup(self, row: WineRow) -> Lookup:
        key = json.dumps(["global-750ml-v1", self.api.url, self.api.currency, row.lookup.casefold(), row.vintage])
        threshold = time.time() - self.ttl if self.ttl else self.started
        cached = self.cache.execute("SELECT result FROM lookups WHERE key = ? AND created > ?",
                                    (key, threshold)).fetchone()
        if cached:
            result = json.loads(cached[0])
            quote = Quote(**result["quote"]) if result["quote"] else None
            return Lookup(result["status"], result["detail"], quote)
        if self.error:
            return Lookup("deferred", "API 수집이 중단되어 다음 실행에서 조회할 항목입니다.")
        if self.max_requests is not None and self.attempts >= self.max_requests:
            return Lookup("deferred", "이번 실행에 지정한 호출 한도에 도달했습니다.")
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.attempts += 1
        started = time.time()
        try:
            result = self.api.lookup(row.lookup, row.vintage)
        except ProviderError as exc:
            self.error = str(exc)
            return Lookup("fetch_error", self.error)
        finally:
            self.next_request = time.monotonic() + 1
        self.cache.execute("INSERT INTO lookups VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                           "created=excluded.created, result=excluded.result WHERE excluded.created >= lookups.created",
                           (key, started, json.dumps(asdict(result), ensure_ascii=False)))
        self.cache.commit()
        return result


def compare(row: WineRow, result: Lookup, rate: Decimal) -> dict:
    quote = result.quote
    market_price = Decimal(quote.price) * rate if quote else None
    status, detail = result.status, result.detail
    percent, savings = None, None
    if market_price is not None:
        if row.price is None:
            status, detail = "price_review", "원본 행사가를 확인해 주세요."
        elif row.pack or row.volume != 750:
            status, detail = "size_review", "750mL 단품 여부를 확인해 주세요."
        else:
            difference = market_price - row.price
            savings = float(difference)
            percent = float(difference / market_price * 100)
            status = "compared"
    return {
        "id": row.id, "sheet": row.sheet, "row": row.row, "name": row.name or row.lookup,
        "lookup": row.lookup, "vintage": row.vintage, "vintage_options": row.vintage_options, "volume_ml": row.volume,
        "assumed_volume": row.assumed_volume, "event_krw": float(row.price) if row.price else None,
        "card_krw": float(row.card) if row.card else None, "section": row.section, "terms": row.terms, "origin": row.origin,
        "market_krw": int(market_price.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if market_price else None,
        "market_original": quote.price if quote else None, "market_currency": quote.currency if quote else None,
        "checked_at": quote.checked_at if quote else None, "matched_name": quote.matched_name if quote else None,
        "source": quote.source if quote else None, "savings_krw": savings, "discount_pct": percent,
        "status": status, "detail": detail,
        "url": wine_searcher_link(row.lookup, row.vintage) if row.lookup else None,
    }


def update_page(path: Path, payload: dict, template: Path = PAGE) -> None:
    html = template.read_text(encoding="utf-8")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    html, count = DATA_BLOCK.subn(lambda match: f"{match[1]}{encoded}{match[2]}", html)
    if count != 1:
        raise ValueError("대시보드 HTML에서 가격 데이터 영역을 하나만 찾아야 합니다.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(html)
    Path(temp.name).replace(path)


def export_queries(path: Path, rows: list[WineRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as out:
        writer = csv.writer(out)
        writer.writerow(["Your Reference Code", "Product Name", "Vintage", "Bottle mL", "Stated Vintages", "Source Location"])
        for row in rows:
            volume = str(row.volume or "") if not row.assumed_volume else ""
            values = [row.id, row.lookup, row.vintage or "", volume, "/".join(row.vintage_options), row.origin]
            writer.writerow([f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])


def build(args: argparse.Namespace, inputs: list[Table]) -> int:
    rows = wine_rows(inputs, args.assume_750ml)
    if not rows:
        raise ValueError("헤더 아래에 가져올 가격표 행이 없습니다.")
    destinations = [args.output] + ([args.export_queries] if args.export_queries else [])
    paths = [path.resolve() for path in destinations]
    sources = {path.resolve() for path in (args.workbook, args.report, args.lookup_map) if path}
    if sources.intersection(paths) or len(paths) != len(set(paths)):
        raise ValueError("원본과 각 출력에 서로 다른 파일 경로를 지정해 주세요.")
    if args.lookup_map:
        rows = apply_lookup_map(rows, args.lookup_map)
    if args.export_queries:
        export_queries(args.export_queries, rows)
    rate = Decimal(1)
    report = None
    api = None
    if args.report:
        if not args.report_currency or not re.fullmatch(r"[A-Z]{3}", args.report_currency) or not args.report_as_of:
            raise ValueError("--report-currency와 --report-as-of를 함께 지정해 주세요.")
        if args.report_currency != "KRW":
            rate = decimal_of(args.fx_rate)
            if rate is None or not args.fx_source:
                raise ValueError("외화 보고서는 원화 환산 환율 --fx-rate와 출처 --fx-source가 필요합니다.")
        elif args.fx_rate or args.fx_source:
            raise ValueError("원화 보고서는 환율 입력 없이 실행해 주세요.")
        report = MarketReport(args.report, args.report_currency, args.report_as_of)
    if args.api:
        if not args.confirm_global_basis:
            raise ValueError("API가 세계 세전 750mL 평균가를 제공함을 확인하고 --confirm-global-basis를 지정해 주세요.")
        api = CachedWineCheck(WineCheck(), args.cache, args.max_requests, args.cache_hours)

    result_rows = []
    try:
        for index, row in enumerate(rows, 1):
            if row.pack and row.origin:
                lookup = Lookup("size_review", "세트 또는 여러 상품으로 표기되어 개별 상품 확인이 필요합니다.")
            elif not row.lookup:
                lookup = Lookup("identity_review", "원본 행에 조회할 와인명이 없습니다.")
            elif row.vintage is None and row.vintage_options:
                lookup = Lookup("identity_review", "원본에 빈티지가 여러 개 표기되어 있습니다. 실제 판매 빈티지를 확인해 주세요.")
            elif row.vintage is None:
                lookup = Lookup("identity_review", "빈티지 연도 또는 NV를 확인해 주세요.")
            elif report:
                lookup = report.lookup(row)
            elif api:
                lookup = api.lookup(row)
            else:
                lookup = Lookup("awaiting_prices", "Wine-Searcher 세계 평균가 수집을 기다리는 항목입니다.")
            result_rows.append(compare(row, lookup, rate))
            if index % 50 == 0 or index == len(rows):
                print(f"처리 {index}/{len(rows)}", file=sys.stderr)
    finally:
        if api:
            api.cache.close()

    counts = Counter(row["status"] for row in result_rows)
    payload = {
        "mode": "results", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": "Wine Check API" if api else "Market Pricing Report" if report else None,
        "fx": {"krw_per_unit": str(rate), "source": args.fx_source, "currency": args.report_currency} if args.fx_source else None,
        "rows": result_rows,
    }
    update_page(args.output, payload)
    print(f"{args.output} · 원본 {len(rows)}행 · 비교 {counts['compared']}행 · 확인 필요 {len(rows) - counts['compared']}행")
    if api:
        print(f"이번 실행 Wine Check 요청 {api.attempts}건")
        if api.error:
            print(api.error, file=sys.stderr)
            return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="이마트 PDF 또는 엑셀의 인식된 상품 행 수 확인")
    builder = commands.add_parser("build", help="원본 전체 행을 비교해 GitHub Pages용 HTML 업데이트")
    for command in (inspect, builder):
        command.add_argument("workbook", type=Path)
        command.add_argument("--sheet", action="append", help="원본 와인 목록 시트. 여러 시트는 반복 지정")
        command.add_argument("--header-row", type=int, help="엑셀 기준 헤더 행 번호")
        for column in HEADERS:
            command.add_argument(f"--{column}-column", help=f"{column}에 사용할 정확한 열 제목")
    provider = builder.add_mutually_exclusive_group()
    provider.add_argument("--api", action="store_true", help="Wine Check API에서 세계 세전 평균가 요청")
    provider.add_argument("--report", type=Path, help="Wine-Searcher Market Pricing Report의 CSV 또는 엑셀")
    builder.add_argument("--confirm-global-basis", action="store_true", help="API 계정의 전 세계 750mL 세전 평균가 기준 확인")
    builder.add_argument("--report-currency", help="보고서 ISO 통화 코드")
    builder.add_argument("--report-as-of", type=date.fromisoformat, help="보고서 가격 기준일 YYYY-MM-DD")
    builder.add_argument("--fx-rate", help="보고서 외화 1단위당 원화 환율")
    builder.add_argument("--fx-source", help="환율 출처와 기준일")
    builder.add_argument("--assume-750ml", action="store_true", help="용량 표시가 없는 단품을 750mL로 가정")
    builder.add_argument("--max-requests", type=int, help="이번 실행의 API 요청 한도. 미조회 행은 다음 실행에서 조회")
    builder.add_argument("--cache", type=Path, default=ROOT / ".work" / "wine-check.sqlite3")
    builder.add_argument("--cache-hours", type=float, default=168, help="API 응답 재사용 시간. 0은 실행 간 캐시 무시")
    builder.add_argument("--export-queries", type=Path, help="조회명, 빈티지, 행 참조 코드와 원본 위치를 CSV로 내보냄")
    builder.add_argument("--lookup-map", type=Path, help="확인한 영문 상품명, 빈티지, 용량을 행 참조 코드로 연결한 CSV")
    builder.add_argument("--output", type=Path, default=PAGE, help="갱신할 HTML 경로")
    args = parser.parse_args()
    try:
        inputs = tables(args.workbook, HEADERS, {"wine", "price"}, header=args.header_row,
                        overrides={key: getattr(args, f"{key}_column") for key in HEADERS}, titles=args.sheet)
        if args.command == "inspect":
            for table in inputs:
                mapped = ", ".join(f"{field}={table.labels[column]}" for field, column in table.columns.items())
                print(f"{table.title} · 헤더 {table.header}행 · 데이터 {sum(1 for _ in records(table))}행 · {mapped}")
            return
        if len(inputs) > 1 and not args.sheet:
            names = ", ".join(table.title for table in inputs)
            raise ValueError(f"후보 시트가 여러 개입니다 ({names}). 원본 목록에 해당하는 --sheet를 각각 지정해 주세요.")
        if args.max_requests is not None and args.max_requests < 1:
            raise ValueError("--max-requests는 1 이상이어야 합니다.")
        if not 0 <= args.cache_hours <= 8760:
            raise ValueError("--cache-hours는 0부터 8760까지 지정할 수 있습니다.")
        raise SystemExit(build(args, inputs))
    except (OSError, ValueError, ProviderError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
