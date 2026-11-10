"""Extract each printed price row from E-Mart's two-panel wine catalog."""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError


HEADER = ("상품명", "행사가(원)", "카드사 적용가", "행사 구분", "판매 조건", "원본 위치", "빈티지", "Wine-Searcher 검색명")
PRICE = re.compile(r"\d{1,3}(?:,\d{3})+")
RATING = re.compile(r"\s*\((?:R\.?\s*P\.?|W\.?\s*S\.?|W\.?\s*A\.?)\s*\d{2,3}\)\s*$", re.IGNORECASE)
YEAR_TOKEN = r"(?:(?:18|19|20)\d{2}|\d{2})"
VINTAGE_LIST = re.compile(rf"\s({YEAR_TOKEN}(?:\s*[/,]\s*{YEAR_TOKEN})+)\s*$")
VINTAGE_FULL = re.compile(r"(?<!\d)((?:18|19|20)\d{2})\s*$")
VINTAGE_SHORT = re.compile(r"(?<=[\s가-힣])(\d{2})\s*$")
VINTAGE_NV = re.compile(r"\s+(N\.?V\.?|논빈티지|무빈티지)\s*$", re.IGNORECASE)
LABEL_NUMBER = re.compile(r"(?:\b(?:bin|no\.?|lot)|빈|넘버|로트)\s*$", re.IGNORECASE)


def _mid(word: dict, start: str, end: str) -> float:
    return (word[start] + word[end]) / 2


def _year(value: str) -> str | None:
    if len(value) == 4:
        return value
    number = int(value)
    if number <= 26:
        return f"20{value}"
    if number >= 70:
        return f"19{value}"
    return None


def _query(name: str) -> tuple[str, str]:
    title = RATING.sub("", name).strip()
    # A stated volume can sit between the wine name and vintage.
    title = re.sub(r"(?<!\w)\d+(?:\.\d+)?\s*(?:ml|cl|l|㎖|ℓ)(?!\w)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+(?:매그넘|마그넘|magnum)\s*$", "", title, flags=re.IGNORECASE)
    title = " ".join(title.split())
    series = VINTAGE_LIST.search(title)
    if series:
        years = [_year(token) for token in re.split(r"\s*[/,]\s*", series[1])]
        return "/".join(years) if all(years) else series[1], title[:series.start()].strip()
    nv = VINTAGE_NV.search(title)
    if nv:
        return "NV", title[:nv.start()].strip()
    full = VINTAGE_FULL.search(title)
    short = VINTAGE_SHORT.search(title)
    match = full or short
    if match and not LABEL_NUMBER.search(title[:match.start()]):
        year = _year(match[1])
        if year:
            return year, title[:match.start()].strip()
    return "", title


def _sale_terms(panel, table_top: float) -> tuple[str, str]:
    banner = panel.crop((panel.bbox[0], panel.bbox[1], panel.bbox[2], table_top)).extract_text() or ""
    lines = [line.strip() for line in banner.splitlines()]
    for line in lines:
        if line.startswith("■"):
            return line.removeprefix("■").strip(), ""
    for line in lines:
        if re.search(r"2\s*병\s*구매\s*시", line):
            return "토일월화수 2병 행사", f"{line.strip('()')} · 교차구매 가능"
    date_line = next((line for line in lines if re.fullmatch(r"\d{1,2}/\d{1,2}\([가-힣]\)", line)), "")
    for line in lines:
        if line.startswith("일별초특가") and "준비수량" not in line and "상품 오른쪽" not in line:
            return " ".join(part for part in (date_line, line) if part), ""
        if any(line.startswith(label) for label in ("인기와인", "화이트 스파클링", "단종 특가", "토일월화수 특가", "요일별 특가")):
            return line, ""
    return "이마트 와인장터", ""


def _panel_rows(panel, page_number: int, side: str) -> list[tuple]:
    words = panel.extract_words(x_tolerance=1, y_tolerance=2)
    item = next((word for word in words if word["text"] == "상품명"), None)
    sale = next((word for word in words if word["text"] == "판매가"), None)
    if not (item and sale):
        return []
    location = f"PDF {page_number}쪽 {side}"
    rules = sorted({round(line["top"], 2) for line in panel.lines
                    if line["x0"] <= item["x0"] and line["x1"] >= item["x1"]
                    and abs(line["top"] - line["bottom"]) < 0.1 and line["top"] > sale["top"]})
    if len(rules) < 2:
        raise ValueError(f"{location}에서 상품 행 경계를 찾지 못했습니다.")
    card = next((word for word in words if word["text"] in {"적용가", "할인가"}
                 and word["x0"] > sale["x1"] and abs(word["top"] - sale["top"]) < 12), None)
    section, terms = _sale_terms(panel, item["top"])

    def prices(row: list[dict], label: dict) -> list[dict]:
        x = _mid(label, "x0", "x1")
        return [word for word in row if PRICE.fullmatch(word["text"]) and abs(_mid(word, "x0", "x1") - x) < 25]

    result = []
    for number, (upper, lower) in enumerate(zip(rules, rules[1:]), 1):
        row = [word for word in words if upper < _mid(word, "top", "bottom") < lower]
        regular = prices(row, sale)
        discounted = prices(row, card) if card else []
        if len(regular) != 1 or (card and len(discounted) != 1):
            raise ValueError(f"{location} {number}행에서 판매가를 하나로 확인할 수 없습니다.")
        name = " ".join(word["text"] for word in row if word["x1"] < regular[0]["x0"]).strip()
        if not name:
            raise ValueError(f"{location} {number}행의 상품명이 비어 있습니다.")
        vintage, query = _query(name)
        result.append((name, regular[0]["text"], discounted[0]["text"] if discounted else None,
                       section, terms, f"{location} · {number}행", vintage, query))
    return result


def pdf_rows(path: Path) -> list[tuple]:
    result = [HEADER]
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                x0, y0, x1, y1 = page.bbox
                mid = (x0 + x1) / 2
                for start, end, side in ((x0, mid, "왼쪽"), (mid, x1, "오른쪽")):
                    result.extend(_panel_rows(page.crop((start, y0, end, y1)), page.page_number, side))
    except (PDFSyntaxError, PDFPasswordIncorrect) as exc:
        raise ValueError("PDF를 읽을 수 없습니다. 열 수 있는 이마트 장터 목록 PDF를 지정해 주세요.") from exc
    if len(result) == 1:
        raise ValueError("이마트 와인장터 PDF에서 상품명과 판매가가 있는 표를 찾지 못했습니다.")
    return result
