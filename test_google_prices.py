"""Regressions for real Google snippets with split highlighted text."""

import unittest

from google_prices import cited_product, observation
from market_catalog import query_plan


NAME = "Two Hands Wines Aphrodite Cabernet Sauvignon"
SOURCE = "https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/2021"
CHECKED_AT = "2026-11-11T00:55:32.148Z"
SNAPSHOT = r'''
- heading "검색결과" [level=1]
- main:
  - heading "Web results" [level=2]
  - link "2021 Two Hands Wines Aphrodite Cabernet Sauvignon Wine-Searcher https://www.wine-searcher.com › ...":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/2021
    - heading "2021 Two Hands Wines Aphrodite Cabernet Sauvignon" [level=3]
    - generic: Wine-Searcher
    - generic: https://www.wine-searcher.com › ...
  - generic: ·
  - link "이 페이지 번역하기":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/2021
  - button "검색결과에 관한 정보":
  - text: Find the best local price for
  - emphasis: "2021"
  - text: Two Hands Wines
  - emphasis: Aphrodite
  - text: Cabernet Sauvignon, Barossa Valley, Australia.
  - emphasis: Avg Price
  - text: (ex-tax) $128 / 750ml.
  - link "Two Hands Wines Aphrodite Cabernet Sauvignon Wine-Searcher https://www.wine-searcher.com › ...":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/1/usa-mi
    - heading "Two Hands Wines Aphrodite Cabernet Sauvignon" [level=3]
    - generic: Wine-Searcher
    - generic: https://www.wine-searcher.com › ...
  - generic: ·
  - link "이 페이지 번역하기":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/1/usa-mi
  - button "검색결과에 관한 정보":
  - text: Find the best local price for Two Hands Wines
  - emphasis: Aphrodite
  - text: Cabernet Sauvignon, Barossa Valley, Australia.
  - emphasis: Avg Price
  - text: ... All
  - emphasis: "2021"
  - text: 2020 2019 2018 2017 2016 2015 ...
  - img "별 5개 중 4.5개 사용자 리뷰 (47)개":
    - text: 92%
    - text: (47)
  - link "Two Hands Wines Aphrodite Cabernet Sauvignon Wine-Searcher https://www.wine-searcher.com › ...":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/1/usa-nj
    - heading "Two Hands Wines Aphrodite Cabernet Sauvignon" [level=3]
    - generic: Wine-Searcher
    - generic: https://www.wine-searcher.com › ...
  - generic: ·
  - link "이 페이지 번역하기":
    - /url: https://www.wine-searcher.com/find/two+hand+aphrodite+cab+sauv+barossa+valley+south+australia/1/usa-nj
  - button "검색결과에 관한 정보":
  - text: Find the best local price for Two Hands Wines
  - emphasis: Aphrodite
  - text: Cabernet Sauvignon, Barossa Valley, Australia.
  - emphasis: Avg Price
  - text: (ex-tax) $168 / 750ml.
  - link "Best local price for 2021 Duck Walk Vineyards Aphrodite Late ... Wine-Searcher https://www.wine-searcher.com › ...":
    - /url: https://www.wine-searcher.com/find/duck+walk+aphrodite+late+harvest+gewurz+long+island+new+york+usa/2021
    - heading "Best local price for 2021 Duck Walk Vineyards Aphrodite Late ..." [level=3]
    - generic: Wine-Searcher
    - generic: https://www.wine-searcher.com › ...
  - generic: ·
  - link "이 페이지 번역하기":
    - /url: https://www.wine-searcher.com/find/duck+walk+aphrodite+late+harvest+gewurz+long+island+new+york+usa/2021
  - button "검색결과에 관한 정보":
  - text: Find the best local price for
  - emphasis: "2021"
  - text: Duck Walk Vineyards
  - emphasis: Aphrodite
  - text: Late Harvest Gewurztraminer, Long Island, USA.
  - emphasis: Avg Price
  - text: (ex-tax) $40 / 750ml.
'''


class GoogleSnippetTests(unittest.TestCase):
    def read_quote(self, name=NAME, vintage="2021", snapshot=SNAPSHOT):
        return observation(snapshot, name, vintage, "https://www.google.com/search?q=aphrodite", CHECKED_AT)["quote"]

    def test_split_average_returns_exact_2021_price(self):
        quote = self.read_quote()
        self.assertEqual((quote["price"], quote["currency"], quote["vintage"]), ("128", "USD", "2021"))
        self.assertEqual((quote["source"], quote["url"]), ("google_snippet", SOURCE))
        self.assertEqual(quote["scope"], "unspecified")

    def test_another_vintage_is_not_substituted(self):
        self.assertIsNone(self.read_quote(vintage="2020"))

    def test_another_two_hands_cuvee_is_not_substituted(self):
        self.assertIsNone(self.read_quote(name="Two Hands Wines Bella's Garden Shiraz"))

    def test_generic_result_listing_2021_is_not_a_2021_price(self):
        lines = SNAPSHOT.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith('  - link "Two Hands Wines'))
        self.assertIsNone(self.read_quote(snapshot="\n".join(lines[start:])))

    def test_other_producer_price_stays_with_its_result(self):
        quote = self.read_quote(name="Duck Walk Vineyards Aphrodite Late Harvest Gewurztraminer")
        self.assertEqual(quote["price"], "40")
        self.assertIn("duck+walk+", quote["url"])

    def test_barossa_citation_matches_without_losing_vintage_guard(self):
        self.assertIsNotNone(cited_product(SOURCE, NAME, "2021", CHECKED_AT))
        self.assertIsNone(cited_product(SOURCE.replace("/2021", "/2017"), NAME, "2021", CHECKED_AT))
        self.assertIsNone(cited_product(SOURCE.replace("aphrodite", "bellas+garden"), NAME, "2021", CHECKED_AT))

    def test_conflicting_year_in_result_description_is_rejected(self):
        self.assertIsNone(self.read_quote(snapshot=SNAPSHOT.replace('- emphasis: "2021"', '- emphasis: "2019"', 1)))

    def test_country_filtered_price_requires_explicit_worldwide_average(self):
        local = SNAPSHOT.replace(SOURCE, SOURCE + "/usa")
        self.assertIsNone(self.read_quote(snapshot=local))
        worldwide = local.replace("- emphasis: Avg Price", "- emphasis: Global Avg Price")
        self.assertEqual(self.read_quote(snapshot=worldwide)["scope"], "worldwide")

    def test_known_cuvee_path_does_not_accept_another_1865_wine(self):
        snippet = '''
- main:
  - link "Vina San Pedro 1865 Prelusion":
    - /url: https://www.wine-searcher.com/find/vina+st+pedro+1865+single+select+cab+sauv+maipo+valley+chile
    - heading "Vina San Pedro 1865 Prelusion" [level=3]
  - text: Avg Price (ex-tax) $21 / 750ml.
'''
        self.assertIsNone(self.read_quote(name="San Pedro 1865 Prelude", vintage=None, snapshot=snippet))
        exact = snippet.replace("single+select+cab+sauv", "prelusion").replace("$21", "$72")
        self.assertEqual(self.read_quote(name="San Pedro 1865 Prelude", vintage=None, snapshot=exact)["price"], "72")

    def test_long_pichon_baron_label_does_not_include_second_wine(self):
        second_wine = '''
  - link "2017 Les Tourelles de Pichon Baron":
    - /url: https://www.wine-searcher.com/find/les+tourelles+de+pichon+baron+pauillac+medoc+bordeaux+france/2017
    - heading "2017 Les Tourelles de Pichon Baron" [level=3]
  - text: Avg Price (ex-tax) $65 / 750ml.
'''
        grand_vin = '''
  - link "2017 Chateau Pichon-Longueville au Baron de ...":
    - /url: https://www.wine-searcher.com/find/pichon+longueville+au+baron+de+pauillac+medoc+bordeaux+france/2017
    - heading "2017 Chateau Pichon-Longueville au Baron de ..." [level=3]
  - text: Find the best local price for
  - emphasis: 2017 Chateau Pichon-Longueville
  - text: au Baron de Pichon-Longueville, Pauillac, France.
  - emphasis: Avg Price
  - text: (ex-tax) $194 / 750ml.
'''
        self.assertIsNone(self.read_quote(name="Chateau Pichon Baron", vintage="2017", snapshot=second_wine))
        quote = self.read_quote(name="Chateau Pichon Baron", vintage="2017", snapshot=second_wine + grand_vin)
        self.assertEqual(quote["price"], "194")


class RetryPlanTests(unittest.TestCase):
    def test_displayed_vintage_with_legacy_query_metadata_is_not_retried(self):
        row = {"name": "Almaviva 19/23", "search_name": "Almaviva", "grades": ["A", "B"],
               "vintage": None, "vintage_options": ["2019", "2023"],
               "search_prices": [{"vintage": "2019", "query_vintage": None}]}
        self.assertEqual(query_plan([row], unpriced_only=True), [{"name": "Almaviva", "vintage": "2023"}])


if __name__ == "__main__":
    unittest.main()
