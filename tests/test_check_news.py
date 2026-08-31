"""Unit tests for scripts/check_news.py.

These tests never hit the network: parsing is tested against a local HTML
fixture, and Telegram calls are outside the scope of the pure functions
under test here.
"""

from pathlib import Path

import check_news

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "sample_news_page.html"


def load_fixture_html() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


def test_parse_articles_page_extracts_all_articles():
    articles = check_news.parse_articles_page(load_fixture_html())

    assert set(articles.keys()) == {"789", "784", "933", "931"}


def test_parse_articles_page_extracts_expected_fields():
    articles = check_news.parse_articles_page(load_fixture_html())

    coin_article = articles["931"]
    assert coin_article["title"] == 'Пам\'ятна монета `Українська бавовна. Морський дрон "Sea Baby"`'
    assert coin_article["date"] == "29 Липня 2026"
    assert coin_article["url"] == (
        "https://coins.bank.gov.ua/pam-atna-moneta-ukrains-ka-bavovna-mors-kii-dron-sea-baby-/a-931.html"
    )


def test_parse_articles_page_returns_empty_dict_for_unrelated_html():
    assert check_news.parse_articles_page("<html><body>nothing here</body></html>") == {}


def test_matches_keywords_is_case_insensitive_substring_match():
    assert check_news.matches_keywords("Нова пам'ятна МОНЕТА", ["монет"])
    assert not check_news.matches_keywords("Оновлення сайту", ["монет"])


def test_matches_keywords_matches_any_of_several_keywords():
    keywords = ["монет", "банкнот"]
    assert check_news.matches_keywords("Нова сувенірна банкнота", keywords)
    assert check_news.matches_keywords("Нова пам'ятна монета", keywords)
    assert not check_news.matches_keywords("Загальна новина", keywords)


def test_select_new_matching_articles_excludes_already_seen():
    articles = check_news.parse_articles_page(load_fixture_html())
    seen_ids = {"789", "784", "931"}

    result = check_news.select_new_matching_articles(articles, seen_ids, ["монет"])

    assert [a["id"] for a in result] == ["933"]


def test_select_new_matching_articles_excludes_non_matching_titles():
    articles = check_news.parse_articles_page(load_fixture_html())
    seen_ids: set[str] = set()

    result = check_news.select_new_matching_articles(articles, seen_ids, ["монет"])

    assert {a["id"] for a in result} == {"933", "931"}
    assert "789" not in {a["id"] for a in result}
    assert "784" not in {a["id"] for a in result}


def test_select_new_matching_articles_is_sorted_oldest_id_first():
    articles = check_news.parse_articles_page(load_fixture_html())

    result = check_news.select_new_matching_articles(articles, set(), ["монет"])

    assert [a["id"] for a in result] == ["931", "933"]


def test_get_keywords_defaults_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("KEYWORDS", raising=False)

    assert check_news.get_keywords() == check_news.DEFAULT_KEYWORDS


def test_get_keywords_parses_comma_separated_env_var(monkeypatch):
    monkeypatch.setenv("KEYWORDS", " Монет , Банкнот ,")

    assert check_news.get_keywords() == ["монет", "банкнот"]


def test_save_and_load_state_round_trip(tmp_path):
    state_path = tmp_path / "seen_news.json"
    state = {"seen_ids": ["1", "2", "3"]}

    check_news.save_state(state, path=state_path)
    loaded = check_news.load_state(path=state_path)

    assert loaded == state


def test_load_state_returns_empty_state_when_file_missing(tmp_path):
    state_path = tmp_path / "does_not_exist.json"

    assert check_news.load_state(path=state_path) == {"seen_ids": []}


def test_fetch_articles_merges_pages_and_uses_the_client(monkeypatch):
    html = load_fixture_html()

    class FakeResponse:
        text = html

    class FakeClient:
        def __init__(self):
            self.requested = []

        def get(self, url, timeout=30):
            self.requested.append(url)
            return FakeResponse()

    client = FakeClient()
    articles = check_news.fetch_articles(client, ["u1", "u2"])

    assert client.requested == ["u1", "u2"]
    assert set(articles.keys()) == {"789", "784", "933", "931"}
