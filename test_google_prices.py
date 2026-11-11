"""Regressions for real Google snippets with split highlighted text."""

import unittest

from google_prices import cited_product, observation


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


if __name__ == "__main__":
    unittest.main()
