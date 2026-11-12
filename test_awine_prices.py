import unittest

from awine_prices import best_offer, bundle_components, compare, compare_bundle, identity, read_product, row_name
from build_dashboard import bottle_size


def product(korean, english, *, vintage=None, size=None, price=65_000):
    return {
        "name": korean, "english": english, "vintage": vintage, "volume_ml": size,
        "type": "샴페인", "pack": False, "descriptors": "Champagne",
        "url": "https://awine.kr/wine/example/", "price_krw": price, "stock": "cart",
        "stock_note": "", "has_case": False, "checked_at": "2026-11-11T00:00:00+00:00",
    }


class AwineTests(unittest.TestCase):
    def test_uses_product_title_for_vintage(self):
        page = """<main><section>
          <h1 id="kor_title">샤또 무통 로칠드 2023</h1>
          <h2 id="eng_title">Chateau Mouton Rothschild</h2>
          <p id="price">1,250,000원 <button>장바구니 담기</button></p>
        </section></main>"""
        actual = read_product(page.encode(), "https://awine.kr/wine/mouton-1991/")
        self.assertEqual(actual["vintage"], "2023")
        self.assertEqual(actual["price_krw"], 1_250_000)

    def test_english_title_cannot_match_a_different_korean_product(self):
        row = {"name": "모엣샹동 임페리얼", "type": "sparkling", "search_name": "Moet Chandon Imperial Brut"}
        unrelated = product("샴페인 보주아 브뤼", "Moet Chandon Imperial Brut", vintage="NV")
        self.assertFalse(identity(row, unrelated, row_name(row)))

    def test_magnum_requires_same_bottle_size(self):
        row = {"name": "모엣샹동 임페리얼 매그넘", "event_krw": 95_800, "card_krw": 67_060}
        standard = product("모엣 샹동 임페리얼 브뤼", "Moet Chandon Imperial Brut")
        magnum = product("모엣 샹동 임페리얼 브뤼 매그넘", "Moet Chandon Imperial Brut", size=1500)
        self.assertIsNone(best_offer(row, [standard], None))
        self.assertEqual(best_offer(row, [magnum], None)["volume_ml"], 1500)
        self.assertEqual(bottle_size("1,500ml"), 1500)

    def test_compares_general_and_card_prices(self):
        row = {"name": "모엣샹동 임페리얼", "type": "sparkling", "search_name": "Moet Chandon Imperial Brut",
               "event_krw": 95_800, "card_krw": 67_060}
        same_wine = product("모엣 샹동 임페리얼 브뤼", "Moet Chandon Imperial Brut", size=750)
        result = compare(row, [same_wine["url"]], {same_wine["url"]: same_wine})["offers"][0]
        self.assertEqual((result["event_delta"], result["card_delta"]), (-30_800, -2_060))
        self.assertIn("750mL 가정", result["notes"])

    def test_bundle_multiplies_quantities_and_marks_nearby_vintage(self):
        row = {"id": "A!1", "name": "샴페인 세트", "event_krw": 180_000, "card_krw": 150_000}
        name = "모엣 샹동 임페리얼 브뤼"
        bundle = {"bottles": 3, "volume_ml": 750, "components": [
            {"name": name, "vintage": "2012", "quantity": 2},
            {"name": name, "vintage": "2015", "quantity": 1},
        ]}
        parts = bundle_components(row, bundle)
        same_wine = product(name, "Moet Chandon Imperial Brut", vintage="2013", size=750)
        url = same_wine["url"]
        result = compare_bundle(row, bundle, parts, {part["id"]: [url] for part in parts}, {url: same_wine})
        self.assertTrue(result["bundle"]["reference"])
        self.assertEqual(result["offers"][0]["price_krw"], 195_000)
        self.assertEqual(result["offers"][0]["card_delta"], 45_000)

    def test_bundle_keeps_verified_components_when_total_is_missing(self):
        row = {"id": "A!1", "name": "샴페인 세트"}
        same_wine = product("모엣 샹동 임페리얼 브뤼", "Moet Chandon Imperial Brut", vintage="2012", size=750)
        bundle = {"bottles": 2, "volume_ml": 750, "components": [
            {"name": same_wine["name"], "vintage": "2012", "quantity": 1},
            {"name": same_wine["name"], "vintage": "2019", "quantity": 1},
        ]}
        parts = bundle_components(row, bundle)
        url = same_wine["url"]
        result = compare_bundle(row, bundle, parts, {part["id"]: [url] for part in parts}, {url: same_wine})
        self.assertEqual(result["status"], "bundle_incomplete")
        self.assertNotIn("offers", result)
        first, second = result["bundle"]["components"]
        self.assertEqual(first["offer"]["price_krw"], 65_000)
        self.assertEqual(second["status"], "incompatible")
