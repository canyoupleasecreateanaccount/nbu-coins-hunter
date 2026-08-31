"""Unit tests for scripts/check_lots.py.

These tests never hit the network: parsing is tested against local HTML
fixtures, and Telegram calls are outside the scope of the pure functions
under test here.
"""

from pathlib import Path

import check_lots

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_catalog_page_extracts_all_products():
    products = check_lots.parse_catalog_page(load_fixture_html("sample_catalog_page.html"))

    assert set(products.keys()) == {"1126", "1183"}


def test_parse_catalog_page_extracts_expected_fields():
    products = check_lots.parse_catalog_page(load_fixture_html("sample_catalog_page.html"))

    quoted = products["1183"]
    assert quoted["title"] == '"Архістратиг Михаїл" (з)'
    assert quoted["price"] == "71   982 грн"
    assert quoted["url"] == "https://coins.bank.gov.ua/-arhistratig-mihajil-z-/p-1183.html"


def test_parse_catalog_page_returns_empty_dict_for_unrelated_html():
    assert check_lots.parse_catalog_page("<html><body>nothing here</body></html>") == {}


def test_find_next_page_url_returns_none_when_catalog_fits_one_page():
    html = load_fixture_html("sample_catalog_page.html")

    assert check_lots.find_next_page_url(html) is None


def test_find_next_page_url_follows_pagination_when_present():
    html = load_fixture_html("sample_catalog_page_paginated.html")

    assert check_lots.find_next_page_url(html) == "https://coins.bank.gov.ua/catalog.html?page=2"


def test_select_new_lots_excludes_already_seen():
    products = check_lots.parse_catalog_page(load_fixture_html("sample_catalog_page.html"))
    previous_ids = {"1126"}

    result = check_lots.select_new_lots(products, previous_ids)

    assert [p["id"] for p in result] == ["1183"]


def test_select_new_lots_is_sorted_oldest_id_first():
    products = check_lots.parse_catalog_page(load_fixture_html("sample_catalog_page.html"))

    result = check_lots.select_new_lots(products, set())

    assert [p["id"] for p in result] == ["1126", "1183"]


def test_select_new_lots_returns_nothing_when_all_previously_seen():
    products = check_lots.parse_catalog_page(load_fixture_html("sample_catalog_page.html"))

    assert check_lots.select_new_lots(products, set(products.keys())) == []


def test_save_and_load_state_round_trip(tmp_path):
    state_path = tmp_path / "seen_lots.json"
    state = {"available_ids": ["885", "1016"]}

    check_lots.save_state(state, path=state_path)
    loaded = check_lots.load_state(path=state_path)

    assert loaded == state


def test_load_state_returns_empty_state_when_file_missing(tmp_path):
    state_path = tmp_path / "does_not_exist.json"

    assert check_lots.load_state(path=state_path) == {"available_ids": []}


def test_fetch_products_follows_pagination_until_exhausted(monkeypatch):
    page1 = load_fixture_html("sample_catalog_page_paginated.html")
    page2 = load_fixture_html("sample_catalog_page.html")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

    class FakeClient:
        def __init__(self):
            self.requested = []

        def get(self, url, timeout=30):
            self.requested.append(url)
            return FakeResponse(page1 if len(self.requested) == 1 else page2)

    client = FakeClient()
    products = check_lots.fetch_products(client, start_url="https://coins.bank.gov.ua/catalog.html")

    assert client.requested == [
        "https://coins.bank.gov.ua/catalog.html",
        "https://coins.bank.gov.ua/catalog.html?page=2",
    ]
    assert set(products.keys()) == {"885", "1126", "1183"}


def test_fetch_products_makes_a_single_request_when_catalog_fits_one_page(monkeypatch):
    html = load_fixture_html("sample_catalog_page.html")

    class FakeResponse:
        text = html

    class FakeClient:
        def __init__(self):
            self.requested = []

        def get(self, url, timeout=30):
            self.requested.append(url)
            return FakeResponse()

    client = FakeClient()
    products = check_lots.fetch_products(client, start_url="https://coins.bank.gov.ua/catalog.html")

    assert client.requested == ["https://coins.bank.gov.ua/catalog.html"]
    assert set(products.keys()) == {"1126", "1183"}
