"""Fetch vintage-specific Wine Check averages through Wine-Searcher's HTTPS API."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


@dataclass(frozen=True)
class Quote:
    price: str
    currency: str
    checked_at: str
    matched_name: str
    source: str


@dataclass(frozen=True)
class Lookup:
    status: str
    detail: str = ""
    quote: Quote | None = None


class ProviderError(Exception):
    """A provider or connection error that ends the current collection pass."""


def normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value)).casefold()
    return unicodedata.normalize("NFC", "".join(char for char in decomposed if char.isalnum()))


def vintage_of(value: object) -> str | None:
    if value is None:
        return None
    year = str(value).strip()
    if re.fullmatch(r"(?:18|19|20)\d{2}(?:\.0+|\s*년)?", year):
        return year[:4]
    if normalize(year) in {"nv", "nonvintage", "nonmillesime", "논빈티지", "무빈티지"}:
        return "NV"
    return None


def decimal_of(value: object) -> Decimal | None:
    number = str(value).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", number):
        return None
    result = Decimal(number.replace(",", ""))
    return result if 0 < result < 1_000_000_000_000 else None


def wine_searcher_link(name: str, vintage: str | None) -> str:
    suffix = f"/{vintage}" if vintage else ""
    return f"https://www.wine-searcher.com/find/{quote_plus(name)}{suffix}"


def _fields(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {normalize(key): item for key, item in value.items()}


def _get(fields: dict, *keys: str) -> object:
    for key in keys:
        value = fields.get(normalize(key))
        if value is not None and value != "":
            return value
    return None


def _same_wine(query: str, name: object, lwin: object) -> bool:
    query_lwin = re.fullmatch(r"LWIN(\d{7})(?:\d{4}|\d{11})?", query, re.IGNORECASE)
    if query_lwin and lwin:
        result_lwin = re.fullmatch(r"(?:LWIN)?(\d{7})(?:\d{4}|\d{11})?", str(lwin), re.IGNORECASE)
        return bool(result_lwin and result_lwin[1] == query_lwin[1])
    return bool(name and normalize(name) == normalize(query))


def parse_wine_check(payload: object, query: str, vintage: str, currency: str) -> Lookup:
    if not isinstance(payload, dict):
        raise ProviderError("Wine Check 응답이 JSON 객체 형식과 다릅니다.")
    root = _fields(payload)
    status_fields = _fields(_get(root, "return", "status"))
    code = _get(status_fields, "return-code", "code")
    if code is None:
        code = _get(root, "return-code", "status")
    code = str(code).casefold() if code is not None else None
    if code == "5":
        raise ProviderError("Wine Check API 일일 호출 한도에 도달했습니다.")
    if code in {"4", "6", "7", "10"}:
        raise ProviderError(f"Wine Check API가 요청을 거절했습니다 (코드 {code}).")
    if code == "8":
        return Lookup("identity_review", "Wine-Searcher에서 이름에 해당하는 상품을 여러 개 찾았습니다.")
    if code not in {None, "0", "success", "ok"}:
        detail = f" (코드 {code})" if code and code.isascii() and code.isdecimal() else ""
        return Lookup("no_average", f"Wine Check에서 비교 가능한 응답을 받지 못했습니다{detail}.")

    result = _fields(_get(root, "data", "result", "response")) or root
    wine = _get(result, "wine", "wine-details", "wines")
    if isinstance(wine, list):
        if len(wine) > 1:
            return Lookup("identity_review", "Wine Check가 여러 상품을 반환했습니다.")
        wine = wine[0] if wine else None
    details = _fields(wine) or result
    price_fields = _fields(_get(details, "prices", "pricing"))
    list_fields = _fields(_get(result, "list", "list-info", "summary"))
    # Read only quote-level or product-level fields. Merchant offers are separate.
    fields = {**root, **result, **list_fields, **details, **price_fields}
    for key in ("list-location", "location", "market", "scope"):
        scope = _get(fields, key)
        if scope is not None and normalize(scope) not in {"worldwide", "global", "allcountries", "world", "all", "any"}:
            return Lookup("basis_review", "응답의 가격 집계 지역이 전 세계로 확인되지 않았습니다.")
    tax = _get(fields, "tax-basis", "tax", "price-tax-basis")
    if tax is not None and normalize(tax) not in {"ex", "extax", "excludingtax", "beforetax", "exvat"}:
        return Lookup("basis_review", "응답의 세금 기준이 세전으로 확인되지 않았습니다.")
    basis_ml = _get(fields, "basis-ml", "price-basis-ml", "bottle-ml")
    if basis_ml is not None and decimal_of(basis_ml) != 750:
        return Lookup("basis_review", "응답의 평균가 환산 기준이 750mL로 확인되지 않았습니다.")

    raw_average = _get(fields, "global-avg-price", "global-average-price", "price-average",
                       "average-price", "avg-price", "price-avg", "price-ave", "ave-price")
    if raw_average is None:
        return Lookup("no_average", "응답에 Wine Check 평균가가 없습니다.")
    if str(_get(fields, "currency", "currency-code", "list-currency-code")).upper() != currency:
        return Lookup("basis_review", f"응답의 통화가 {currency}로 확인되지 않았습니다.")
    if isinstance(raw_average, str):
        raw_average = raw_average.removeprefix(currency).strip()
        if currency == "KRW":
            raw_average = raw_average.removeprefix("₩").removesuffix("원").strip()
    amount = decimal_of(raw_average)
    if amount is None:
        return Lookup("no_average", "비교 가능한 양수 평균가를 받지 못했습니다.")

    name = _get(details, "wine-name", "product-name", "name", "wine") or _get(list_fields, "wine-name", "list-wine-name")
    year = _get(details, "vintage", "wine-vintage", "year") or _get(list_fields, "vintage", "list-vintage")
    returned_vintage = vintage_of(year)
    lwin = _get(details, "lwin", "lwin-code") or _get(list_fields, "lwin", "lwin-code")
    if returned_vintage is None and lwin:
        match = re.fullmatch(r"(?:LWIN)?\d{7}(\d{4})(?:\d{7})?", str(lwin), re.IGNORECASE)
        if match:
            returned_vintage = vintage_of(match[1])
    if returned_vintage != vintage or not _same_wine(query, name, lwin):
        return Lookup("identity_review", "응답의 와인명 또는 빈티지가 조회 조건과 일치하는지 확인해 주세요.")
    return Lookup("matched", quote=Quote(
        price=str(amount), currency=currency, checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        matched_name=str(name or query), source="Wine Check API",
    ))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The request URL includes a credential.
        return None


class WineCheck:
    def __init__(self, currency: str = "KRW"):
        self.key = os.environ.get("WINE_SEARCHER_API_KEY", "").strip()
        self.url = os.environ.get("WINE_SEARCHER_API_URL", "").strip()
        if not self.key or not self.url:
            raise ProviderError("WINE_SEARCHER_API_KEY와 WINE_SEARCHER_API_URL 환경변수를 설정해 주세요.")
        try:
            endpoint = urlsplit(self.url)
            host = endpoint.hostname or ""
            valid = (endpoint.scheme == "https" and (host == "wine-searcher.com" or host.endswith(".wine-searcher.com"))
                     and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
                     and endpoint.port in {443, None} and endpoint.path not in {"", "/"})
        except ValueError:
            valid = False
        if not valid:
            raise ProviderError("Wine-Searcher에서 발급한 HTTPS API 주소를 쿼리 문자열 없이 설정해 주세요.")
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ProviderError("API 통화는 ISO 3자리 코드로 지정해 주세요.")
        self.currency = currency
        self.opener = build_opener(_NoRedirect())

    def lookup(self, query: str, vintage: str) -> Lookup:
        params = urlencode({
            "api_key": self.key, "winename": query, "vintage": vintage,
            "currencycode": self.currency, "format": "J",
        })
        request = Request(f"{self.url}?{params}", headers={
            "Accept": "application/json", "User-Agent": "WineDashboard/1.0",
        })
        try:
            with self.opener.open(request, timeout=25) as response:
                payload = json.loads(response.read(2_000_000), parse_float=Decimal)
        except HTTPError as exc:
            raise ProviderError(f"Wine Check HTTP {exc.code}. API 주소와 계정 상태를 확인해 주세요.") from None
        except (URLError, OSError):
            raise ProviderError("Wine Check API에 연결하지 못했습니다.") from None
        except (ValueError, UnicodeError):
            raise ProviderError("Wine Check 응답을 JSON으로 읽지 못했습니다. 발급받은 API 주소와 응답 형식을 확인해 주세요.") from None
        return parse_wine_check(payload, query, vintage, self.currency)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wine-Searcher의 Wine Check API로 와인 한 건을 조회합니다.")
    parser.add_argument("name", help="Wine-Searcher의 정확한 와인명 또는 LWIN 코드")
    parser.add_argument("vintage", help="연도 또는 NV")
    parser.add_argument("--confirm-global-basis", action="store_true",
                        help="발급받은 API가 전 세계 750mL 세전 평균가를 제공함을 확인했습니다.")
    args = parser.parse_args()
    vintage = vintage_of(args.vintage)
    if vintage is None or not args.name.strip():
        parser.error("와인명과 정확한 연도 또는 NV가 필요합니다.")
    if not args.confirm_global_basis:
        parser.error("API의 세계 세전 750mL 가격 기준을 확인한 뒤 --confirm-global-basis를 지정해 주세요.")
    try:
        result = WineCheck().lookup(args.name.strip(), vintage)
    except ProviderError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if result.quote is None:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
