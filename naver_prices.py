"""Collect Naver web search results for the E-Mart wine catalog."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from build_dashboard import DATA_BLOCK, PAGE


ENDPOINT = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
SUFFIX = "wine searcher price"
CACHE = PAGE.parent.parent / ".work" / "naver-search.sqlite3"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class SearchError(Exception):
    pass


class NaverSearch:
    def __init__(self, client_id: str, client_secret: str, requests_per_second: float):
        self.headers = {
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
            "Accept": "application/json",
        }
        self.interval = 1 / requests_per_second
        self.next_request = 0.0
        self.pace_lock = threading.Lock()
        self.stopped = threading.Event()

    def _pace(self) -> None:
        with self.pace_lock:
            now = time.monotonic()
            delay = max(0, self.next_request - now)
            self.next_request = now + delay + self.interval
        time.sleep(delay)

    def search(self, query: str) -> list[dict]:
        params = urlencode({"query": query, "display": 25, "start": 1, "format": "json"})
        request = Request(f"{ENDPOINT}?{params}", headers=self.headers)
        opener = build_opener(NoRedirect())
        for attempt in range(3):
            if self.stopped.is_set():
                raise SearchError("인증 또는 호출 한도로 인해 중지됨")
            self._pace()
            try:
                with opener.open(request, timeout=14) as response:
                    result = json.load(response)
                if not isinstance(result.get("items"), list):
                    raise SearchError("검색 응답에 items 목록이 없습니다")
                return result["items"]
            except HTTPError as error:
                status = error.code
                error.close()
                if status in (401, 403):
                    self.stopped.set()
                    raise SearchError(f"네이버 API HTTP {status}") from None
                if status not in (429, 500, 502, 503, 504) or attempt == 2:
                    if status == 429:
                        self.stopped.set()
                    raise SearchError(f"네이버 API HTTP {status}") from None
            except (URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 2:
                    raise SearchError("네이버 API 응답을 가져오지 못했습니다") from None
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")


def read_page(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    match = DATA_BLOCK.search(html)
    if match is None:
        raise ValueError("대시보드에서 와인 목록을 찾을 수 없습니다")
    return json.loads(html[match.end(1):match.start(2)])


def price_query(name: str, vintage: str | None = None) -> str:
    return " ".join(part for part in (name.strip(), vintage or "", SUFFIX) if part)


def read_aliases(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as source:
        return {key: name for key, name in csv.reader(source, delimiter="\t")}


def catalog_queries(rows: list[dict], aliases: dict[str, str]) -> list[str]:
    queries = []
    for row in rows:
        name = row["lookup"] or row["name"]
        vintages = row.get("vintage_options") or [row.get("vintage")]
        for query_name in dict.fromkeys([name, aliases.get(name, name)]):
            queries.extend(price_query(query_name, vintage) for vintage in vintages)
    return list(dict.fromkeys(queries))


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE IF NOT EXISTS searches (query TEXT PRIMARY KEY, checked_at TEXT NOT NULL, items TEXT NOT NULL)")
    return database


def collect(args: argparse.Namespace, queries: list[str]) -> int:
    with closing(open_cache(args.cache)) as database:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.cache_hours)).isoformat()
        cached = {row[0] for row in database.execute("SELECT query FROM searches WHERE checked_at >= ?", (cutoff,))}
        pending = [query for query in queries if query not in cached]
        print(f"검색어 {len(queries)}개 · 캐시 {len(queries) - len(pending)}개 · 신규 {len(pending)}개", flush=True)
        if not pending:
            return 0
        client_id = os.environ.get("NAVER_CLIENT_ID") or getpass.getpass("NAVER Client ID: ")
        client_secret = os.environ.get("NAVER_CLIENT_SECRET") or getpass.getpass("NAVER Client Secret: ")
        if not client_id or not client_secret:
            raise SearchError("NAVER Client ID와 Client Secret을 입력해 주세요")
        search = NaverSearch(client_id, client_secret, args.requests_per_second)
        failures = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(search.search, query): query for query in pending}
            for index, future in enumerate(as_completed(futures), 1):
                query = futures[future]
                try:
                    items = future.result()
                    now = datetime.now(timezone.utc).isoformat()
                    with database:
                        database.execute("INSERT OR REPLACE INTO searches VALUES (?, ?, ?)",
                                         (query, now, json.dumps(items, ensure_ascii=False)))
                except SearchError as error:
                    failures += 1
                    if failures <= 5:
                        print(f"검색 실패 · {query} · {error}", file=sys.stderr)
                if index % 50 == 0 or index == len(pending):
                    print(f"검색 {index}/{len(pending)} · 오류 {failures}", flush=True)
        return 2 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", type=Path, default=PAGE)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--aliases", type=Path, help="원본 검색명과 영문 상품명이 담긴 탭 구분 파일")
    parser.add_argument("--queries", type=Path, help="줄마다 한 개씩 지정한 추가 검색어")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--requests-per-second", type=float, default=8)
    parser.add_argument("--cache-hours", type=float, default=24)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32 or not 0 < args.requests_per_second <= 16 or args.cache_hours < 0:
        parser.error("동시 요청 수, 초당 요청 수, 캐시 시간을 확인해 주세요")
    try:
        aliases = read_aliases(args.aliases) if args.aliases else {}
        if args.queries:
            lines = args.queries.read_text(encoding="utf-8").splitlines()
            queries = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
        else:
            queries = catalog_queries(read_page(args.page)["rows"], aliases)
        raise SystemExit(collect(args, queries))
    except (OSError, ValueError, sqlite3.Error, SearchError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
