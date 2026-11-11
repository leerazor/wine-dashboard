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

from naver_snapshot import (APPENDED_WORDS, AVERAGE as SNIPPET_AVERAGE, PRODUCT as SNIPPET_PRODUCT,
                            NV, SYMBOLS, YEAR, name_words, parse_quote)
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
GEOGRAPHY = APPENDED_WORDS | set(
    "veneto friuli isonzo venezia giulia istriana appellation docg cote beaune nuits sauternes barsac menfi "
    "barossa alto adige trentino bolgheri maipo uco tupungato mendoza argentina gualtallary".split()
)
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
    normalize("Chateau Pichon Baron"): ("Chateau Pichon-Longueville au Baron de Pichon-Longueville",),
    normalize("G d'Estournel"): ("Chateau Cos d'Estournel Goulee G d'Estournel",),
    normalize("Armand Geoffroy Gevrey-Chambertin"): ("Domaine Harmand-Geoffroy Gevrey-Chambertin",),
    normalize("Comte Senard Bourgogne Cuvee Auguste"): ("Domaine Comte Senard Bourgogne Rouge Auguste",),
    normalize("Domaine Philippe Charlopin Charmes-Chambertin Grand Cru"): ("Domaine Philippe Charlopin-Parizot Charmes-Chambertin Grand Cru",),
    normalize("Philippe Charlopin Echezeaux Grand Cru"): ("Domaine Philippe Charlopin-Parizot Echezeaux Grand Cru",),
    normalize("Philippe Charlopin Bourgogne Cote d'Or Rouge"): ("Domaine Philippe Charlopin-Parizot Bourgogne Cote d'Or Rouge",),
    normalize("Philippe Charlopin Chablis"): ("Domaine Philippe Charlopin-Parizot Chablis",),
    normalize("Domaine J.A. Ferret Pouilly-Fuisse Clos des Prouges"): ("Domaine J.A. Ferret Pouilly-Fuisse Tete de Cru Clos des Prouges",),
    normalize("Domaine J.A. Ferret Pouilly-Fuisse Tete de Cru Clos de Jeanne"): ("Domaine J.A. Ferret Clos de Jeanne Tete de Cru",),
    normalize("Yann Durieux PV"): ("Yann Durieux Recrue des Sens PV",),
    normalize("Tua Rita Per Sempre Syrah"): ("Tua Rita Per Sempre Toscana IGT",),
    normalize("Vega Sicilia Valbuena 5º"): ("Vega Sicilia Tinto Valbuena 5",),
    normalize("Tenuta Sette Ponti Orma"): ("Tenuta Sette Ponti Podere Orma Orma Toscana IGT",),
    normalize("Vidal-Fleury Condrieu"): ("J. Vidal-Fleury Condrieu",),
    normalize("Michele Chiarlo Cipressi Nizza"): ("Michele Chiarlo Cipressi",),
    normalize("Fattoria Mantellassi Querciolaia Alicante Maremma Toscana"): ("Mantellassi Querciolaia Alicante Maremma Toscana",),
    normalize("Domane Gobelsburg Gruner Veltliner"): ("Schloss Gobelsburg Domaene Gobelsburg Gruner Veltliner",),
    normalize("Almaviva Epu"): ("Vina Almaviva Epu",),
    normalize("Almaviva"): ("Vina Almaviva",),
    normalize("Misty Cove Estate Marlborough Pinot Noir"): ("Misty Cove Estate Series Pinot Noir",),
    normalize("Attilio Ghisolfi Barolo Bussia Riserva Ciabot Minat"): ("Attilio Ghisolfi Ciabot Minat Riserva",),
    normalize("Giacomo Borgogno No Name Langhe Nebbiolo"): ("Giacomo Borgogno & Figli No Name",),
    normalize("Orpheus and the Raven Eternal Eight Chenin Blanc"): ("Orpheus & The Raven Eternal Eight",),
    normalize("Mollydooker Two Left Feet"): ("Mollydooker Two Left Feet Shiraz Cabernet Merlot",),
    normalize("Josh Cellars Cabernet Sauvignon"): ("Joseph Carr Josh Cellars Cabernet Sauvignon",),
    normalize("Josh Cellars Legacy Red Blend"): ("Joseph Carr Josh Cellars Legacy Red",),
    normalize("Josh Cellars Chardonnay"): ("Joseph Carr Josh Cellars Chardonnay",),
    normalize("Marc Hebrart Brut Rose"): ("Hebrart Premier Cru Brut Rose",),
    normalize("Marc Hebrart Blanc de Blancs Brut"): ("Hebrart Blanc de Blancs Premier Cru Brut",),
    normalize("Marc Hebrart Selection Brut"): ("Hebrart Selection Premier Cru Brut",),
    normalize("Tohu Whenua Awa Chardonnay"): ("Tohu Whenua Awa Single Vineyard Chardonnay",),
    normalize("Codorniu Gran Plus Ultra Chardonnay"): ("Codorniu Gran Plus Ultra Reserva Brut Nature Chardonnay Cava",),
    normalize("Tomas Cusine Llebre Tempranillo"): ("Tomas Cusine Llebre",),
    normalize("Columbia Crest H3 Cabernet Sauvignon"): ("H3 Cabernet Sauvignon",),
    normalize("Banfi Sciandor Brachetto"): ("Banfi Sciandor",),
    normalize("Dark Horse Buttery Chardonnay"): ("The Original Dark Horse Buttery Chardonnay",),
    normalize("Concha y Toro Terrunyo Carmenere"): ("Concha y Toro Terrunyo Block 27 Peumo Vineyard Carmenere",),
    normalize("Saldo Red Blend"): ("The Prisoner Wine Co. Saldo Red Blend",),
    normalize("Saldo Zinfandel"): ("The Prisoner Wine Co. Saldo Zinfandel",),
    normalize("Zonin Amarone della Valpolicella"): ("Famiglia Zonin dal 1821 Amarone della Valpolicella DOCG",),
    normalize("Zonin Valpolicella Ripasso"): ("Famiglia Zonin dal 1821 Valpolicella Superiore Ripasso",),
    normalize("Petrolo Torrione"): ("Petrolo Torrione Valdarno di Sopra",),
    normalize("San Pedro 1865 Selected Vineyards Syrah"): ("Vina San Pedro 1865 Single Vineyard Selected Vineyards Syrah",),
    normalize("La Petite Perriere Blanc"): ("Saget La Petite Perriere Sauvignon Blanc",),
    normalize("Underwood Oregon Pinot Noir"): ("Union Wine Co. Underwood Pinot Noir",),
    normalize("Domaine Long-Depaquit Chablis"): ("Albert Bichot Domaine Long-Depaquit Chablis",),
    normalize("Fontodi Vigna del Sorbo Chianti Classico Gran Selezione"): ("Fontodi Vigna del Sorbo",),
    normalize("Domaines Schlumberger Spiegel Grand Cru Pinot Gris"): ("Domaines Schlumberger Pinot Gris Spiegel",),
    normalize("Domaines Schlumberger Kessler Grand Cru Gewurztraminer"): ("Domaines Schlumberger Gewurztraminer Kessler",),
    normalize("Domaines Schlumberger Saering Grand Cru Riesling"): ("Domaines Schlumberger Riesling Saering",),
    normalize("Torres Gran Coronas"): ("Torres Gran Coronas Cabernet Sauvignon Reserva",),
    normalize("Truffle Hunter Leda Barbaresco"): ("Luca Bosio Vineyards Leda Truffle Hunter Barbaresco",),
    normalize("Don Melchor Cabernet Sauvignon"): ("Concha y Toro Don Melchor Cabernet Sauvignon",),
    normalize("Two Hands Coach House Block Shiraz"): ("Two Hands Wines Coach House Block Single Vineyard Shiraz",),
    normalize("Stella Aurea"): ("Vina Quebrada de Macul Stella Aurea",),
    normalize("Unshackled Red Blend"): ("The Prisoner Wine Co. Unshackled Red Blend",),
    normalize("Unshackled Cabernet Sauvignon"): ("The Prisoner Wine Co. Unshackled Cabernet Sauvignon",),
    normalize("Tabali Transversal"): ("Tabali Transversal Reserva Especial",),
    normalize("Los Camachos Single Vineyard Pinot Noir"): ("Vina Marchigue Los Camachos Pinot Noir",),
    normalize("Camille Saves Prestige Grand Cru Brut"): ("Camille Saves Cuvee Prestige Grand Cru Brut",),
    normalize("Andre Clouet Silver Brut Nature"): ("Andre Clouet Silver Grand Cru Blanc de Noirs Brut Nature",),
    normalize("Freixenet Vintage Reserva"): ("Freixenet Vintage Brut Reserva Cava",),
    normalize("Clos de los Siete"): ("Clos de los Siete by Michel Rolland",),
    normalize("Robert Mondavi Private Selection Cabernet Sauvignon"): ("Robert Mondavi Winery Vint Private Selection Cabernet Sauvignon",),
    normalize("Cloudline Oregon Pinot Noir"): ("Cloudline Cellars Pinot Noir",),
    normalize("Biondi Santi Brunello di Montalcino Riserva"): ("Biondi Santi Tenuta Greppo Riserva",),
    normalize("Bruno Giacosa Barbaresco Rabaja"): ("Falletto di Bruno Giacosa Rabaja",),
    normalize("Laurent Perrier Grand Siecle Iteration No 27"): ("Grand Siecle par Laurent-Perrier No 27",),
    normalize("Sassetti Livio Dieci Brunello di Montalcino"): ("Sassetti Livio Pertimali Dieci",),
    normalize("Tenuta Sette Ponti Vigna dell'Impero"): ("Tenuta Sette Ponti Vigna dell'Impero 1935 Valdarno di Sopra",),
    normalize("Tabali Talinay Pai Pinot Noir"): ("Tabali Talina Pai Pinot Noir",),
    normalize("Faustino Icon Edition"): ("Bodegas Faustino Icon Edition Reserva Especial",),
    normalize("Ruffino Riserva Ducale Oro Chianti Classico Gran Selezione"): ("Ruffino Riserva Ducale Oro-Gold",),
}
# Observed product paths for truncated titles or labels that omit a crucial qualifier.
# A different product path must not inherit a quote just because its snippet uses the same title.
EXACT_SOURCE_SLUGS = {
    normalize("Camille Saves Le Mont des Tours Blanc de Blancs Extra Brut Grand Cru Champagne"): "camille+saves+cuvee+le+mont+de+tour+blancdeblanc+grand+cru+champagne+france",
    normalize("The Big Red Monster Cabernet Sauvignon Paso Robles"): "big+red+the+monster+cab+sauv+paso+robles+st+luis+obispo+county+central+coast+california+usa",
    normalize("Vincent Girardin Bourgogne Chardonnay Cuvee Saint-Vincent"): "vincent+girardin+chard+cuvee+st+bourgogne+blanc+burgundy+france",
    normalize("Frescobaldi Castelgiocondo Brunello di Montalcino"): "marchesi+frescobaldi+castel+giocondo+docg+brunello+di+montalcino+tuscany+italy",
    normalize("San Pedro 1865 Prelude"): "vina+st+pedro+1865+prelusion+maipo+valley+chile",
    normalize("Michele Chiarlo Palas Moscato"): "michel+chiarlo+paillas+docg+muscat+d+asti+piemonte+italy",
    normalize("Annabella Napa Valley Cabernet Sauvignon"): "michael+pozzan+annabella+cab+sauv+napa+valley+county+north+coast+california+usa",
    normalize("Annabella Russian River Pinot Noir"): "michael+pozzan+annabella+pinot+noir+russian+river+valley+sonoma+county+north+coast+california+usa",
    normalize("Mouton Cadet Reserve Graves Rouge"): "baron+philip+de+rothschild+mouton+cadet+rsrv+rouge+graves+bordeaux+france",
    normalize("Mouton Cadet Reserve Graves Blanc"): "baron+philip+de+rothschild+mouton+cadet+rsrv+blanc+graves+bordeaux+france",
    normalize("Dos Copas Cabernet Sauvignon"): "two+copa+cab+sauv+curico+valley+chile",
    normalize("Domaine Leroy Vosne-Romanee Aux Genevrieres"): "leroy+les+genevrieres+vosne+romanee+cote+de+nuit+burgundy+france",
    normalize("Maison Leroy Meursault Les Perrieres"): "leroy+les+perrier+meursault+premier+cru+cote+de+beaune+burgundy+france",
    normalize("Pierre Damoy Chapelle-Chambertin Grand Cru"): "dom+pierre+damoy+grand+cru+chapelle+chambertin+gevrey+cote+de+nuit+burgundy+france",
    normalize("Henri Rebourseau Charmes-Chambertin Grand Cru"): "dom+henri+rebourseau+grand+cru+charmes+chambertin+gevrey+cote+de+nuit+burgundy+france",
    normalize("Henri Rebourseau Clos de Vougeot Grand Cru"): "dom+henri+rebourseau+grand+cru+cote+nuit+clos+de+vougeot+burgundy+france",
    normalize("Philippe Charlopin Charmes-Chambertin Grand Cru"): "dom+philip+charlopin+parizot+grand+cru+charmes+chambertin+gevrey+cote+de+nuit+burgundy+france",
    normalize("Philippe Charlopin Echezeaux Grand Cru"): "dom+philip+charlopin+parizot+grand+cru+echezeaux+flagey+cote+de+nuit+burgundy+france",
    normalize("Catena Zapata Adrianna Vineyard Mundus Bacillus Terrae Malbec"): "catena+zapata+adriana+mundus+bacillus+terrae+malbec+gualtallary+tupungato+uco+valley+mendoza",
    normalize("Catena Zapata Adrianna Vineyard River Stones Malbec"): "catena+zapata+adriana+river+stone+malbec+gualtallary+tupungato+uco+valley+mendoza+argentina",
    normalize("Cantina Terlano Vorberg Pinot Bianco Riserva"): "cantina+terlan+kellerei+vorberg+rsrv+pinot+bianco+alto+adige+trentino+sud+tyrol+italy",
    normalize("Cantina Terlano Nova Domus Riserva"): "cantina+terlan+kellerei+nuova+domus+rsrv+terlaner+alto+adige+sud+tyrol+trentino+italy",
    normalize("Cantina Terlano Monticol Pinot Noir Riserva"): "cantina+terlan+kellerei+montcol+pinot+nero+alto+adige+rsrv+trentino+italy",
    normalize("Cantina Terlano Winkl Sauvignon Blanc"): "cantina+terlan+kellerei+winkel+sauv+alto+adige+sud+tyrol+trentino+italy",
    normalize("Chateau Coutet Barsac"): "coutet+barsac+sauternes+bordeaux+france",
}
SEARCH_NAME_CORRECTIONS = {key: IDENTITY_ALIASES[key][0] for key in (
    normalize("Francois Martenot Chablis Montflory"), normalize("Domaine Berthenet Bourgogne Chardonnay Pur Sang"),
    normalize("Henri de Boursaulx Chablis"), normalize("Miteruno Mayacaba"), normalize("Alibranzia Barolo Poiana"),
    normalize("Alibranzia Barbaresco"), normalize("Erickson & Caradine Napa Valley Cabernet Sauvignon"),
    normalize("Erickson & Caradine Napa Valley Chardonnay"), normalize("Harmos Pole"))}


def canonical_name(name: str) -> str:
    return SEARCH_NAME_CORRECTIONS.get(normalize(name), name)
SLUG_WORDS = {"cab": "cabernet", "sauv": "sauvignon", "chard": "chardonnay", "dom": "domaine", "vyd": "vineyard",
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
    exact_slug = EXACT_SOURCE_SLUGS.get(normalize(name))
    if (exact_slug and parts[1] != exact_slug) or (not exact_slug and not identity_matches(expected, slug)):
        return None
    return {"name": name, "vintage": vintage or year, "currency": "USD", "url": clean_source(url),
            "checked_at": checked_at, "seller_market": source_market(url), "price_month": None,
            "citation_note": "AI 인용 페이지의 빈티지 미표기" if vintage and year is None else ""}


def identity_matches(expected: str, actual: str, full_product: str = "") -> bool:
    def words(value: str) -> frozenset[str]:
        value = re.sub(r"\bg[.\s]*d[.\s]+vajra\b", "gd vajra", value, flags=re.I)
        value = re.sub(r"\bno\.?\s*(\d+)\b", r"no \1", value, flags=re.I)
        value = re.sub(r"\bterlano\s*-\s*kellerei terlan\b", "terlano", value, flags=re.I)
        value = re.sub(r"\bpinot nero\b", "pinot noir", value, flags=re.I)
        value = re.sub(r"\bpinot bianco\b", "pinot blanc", value, flags=re.I)
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


def search_url(name: str, vintage: str | None, snippet_search: bool = False) -> str:
    if snippet_search and re.search(r"[가-힣]", name):
        parts = (vintage, name, "Wine-Searcher")
    elif snippet_search:
        parts = ("site:wine-searcher.com", f'"{vintage}"' if vintage else None, name, '"Avg Price"')
    else:
        parts = (vintage, name, SUFFIX)
    query = " ".join(part for part in parts if part)
    return "https://www.google.com/search?q=" + quote(query)


def clean_source(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()


def source_market(url: str) -> str:
    parts = urlsplit(url).path.strip("/").split("/")
    return unquote_plus(parts[3]).title() if len(parts) > 3 and parts[3] != "-" else ""


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
        match = TEXT.search(line)
        heading = next((i for i in range(index - 1, max(-1, index - 24), -1)
                        if 'heading "' in lines[i] and '[level=3]' in lines[i]), None)
        if heading is None or not match:
            continue
        # The URL must belong to this result's heading, not a preceding result.
        link_line = next((context for context in reversed(lines[max(0, heading - 3):heading])
                          if '- /url:' in context), "")
        source_match = WS_LINK.search(link_line)
        if not source_match:
            continue
        source = source_match[0]
        title = re.sub(r'^.*?heading "|" \[level=3\].*$', "", lines[heading])
        title = re.sub(r"^Best local price for\s+", "", title, flags=re.I)
        title = re.split(r"\s+-\s+stores near|\s+-\s+Wine-Searcher", title, maxsplit=1, flags=re.I)[0]
        average = re.sub(r"(?<![\w$])\$(?=\s*\d)", "US$", match[1].strip('"').replace(";", " "))
        amount = SNIPPET_AVERAGE.search(average)
        value = decimal_of(amount["amount"]) if amount else None
        if value is None or not 0 < value < 100_000_000:
            continue
        product = ""
        if "find the best " not in average[:amount.start()].lower():
            product = next((fragment[1].strip('"') for context in reversed(lines[heading + 1:index])
                            if "find the best " in context.lower() and (fragment := TEXT.search(context))), "")
        description = f"{product} {average}".strip()
        prefix = (product + " " + average[:amount.start()]).strip()
        full_match = SNIPPET_PRODUCT.search(prefix)
        full_product = full_match[1] if full_match else ""
        title_year = re.match(r"^((?:18|19|20)\d{2}|NV)\s+", title, re.I)
        product_year = re.match(r"^((?:18|19|20)\d{2}|NV)\s+", full_product, re.I)
        full_product = re.sub(r"^(?:(?:18|19|20)\d{2}|NV)\s+", "", full_product, flags=re.I)
        parts = urlsplit(source).path.strip("/").split("/")
        code = parts[2].upper() if len(parts) > 2 else "1"
        if YEAR.fullmatch(code) or code == "NV":
            year = code
        elif code in {"0", "1"} and (NV.search(title) or NV.search(prefix)):
            year = "NV"
        elif code == "1":
            year = None
        else:
            continue
        if vintage is not None and year != vintage:
            continue
        if any(found and found[1].upper() != year for found in (title_year, product_year)):
            continue
        result = parse_quote({"link": source, "description": description, "title": title}, checked_at)
        actual = result.name if result else re.sub(r"^(?:(?:18|19|20)\d{2}|NV)\s+", "", title, flags=re.I)
        truncated = actual.rstrip().endswith(("...", "…"))
        exact_slug = EXACT_SOURCE_SLUGS.get(normalize(name))
        if exact_slug and parts[1] != exact_slug:
            continue
        if not exact_slug and not identity_matches(name, actual, full_product) and not (truncated and cited_product(source, name, vintage, checked_at)):
            continue
        if result and result.vintage == year:
            quotes.append(asdict(result))
            continue
        quotes.append({"name": actual or name, "vintage": year, "price": amount["amount"].replace(",", ""),
                       "currency": SYMBOLS.get(amount["currency"].upper(), amount["currency"].upper()),
                       "scope": "worldwide" if amount["scope"] else "unspecified", "seller_market": source_market(source),
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
    ordinary = max((q for q in matches if q["scope"] == "worldwide" or not source_market(q["url"])),
                   key=lambda q: (q["scope"] == "worldwide", q["vintage"] == vintage, not q["seller_market"]), default=None)
    if ai and ai["scope"] != "ai_worldwide" and source_market(ai["url"]):
        ai = None
    selected = ai or ({**ordinary, "source": "google_snippet"} if ordinary else None)
    if selected:
        selected["google_url"] = url
        selected["query_vintage"] = vintage
    return {"name": name, "vintage": vintage, "checked_at": checked_at, "google_url": url, "quote": selected}


def collect(plan: Path, output: Path, limit: int | None, delay: float, executable: Path | None,
            snippet_search: bool = False, retry_unpriced: bool = False) -> int:
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
                    if not retry_unpriced or item.get("quote"):
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
                    page.goto(search_url(name, vintage, snippet_search), wait_until="domcontentloaded", timeout=20000)
                    try:
                        heading = r"^(?:검색결과|Search results|Web results)$" if snippet_search else r"^AI (?:Overview|개요)$"
                        page.get_by_role("heading", name=re.compile(heading, re.I)).first.wait_for(timeout=3000 if snippet_search else 6500)
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
    parser.add_argument("--snippet-search", action="store_true", help="Wine-Searcher 일반 결과의 Avg Price 문구를 지정해 검색")
    parser.add_argument("--retry-unpriced", action="store_true", help="저장된 항목 중 가격이 없는 조회를 다시 검색")
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or not 1 <= args.delay <= 60:
        parser.error("--limit은 양수, --delay는 1~60초여야 합니다.")
    raise SystemExit(collect(args.query_plan, args.output, args.limit, args.delay, args.chromium_path,
                             args.snippet_search, args.retry_unpriced))


if __name__ == "__main__":
    main()
