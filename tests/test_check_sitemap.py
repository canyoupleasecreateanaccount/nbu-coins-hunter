"""Unit tests for scripts/check_sitemap.py.

These tests never hit the network: parsing and title extraction are
tested against local XML/HTML fixtures.
"""

from pathlib import Path

import check_sitemap

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_find_articles_sitemap_url_matches_articles_entry():
    index_xml = (FIXTURES / "sample_sitemap_index.xml").read_text(encoding="utf-8")

    assert check_sitemap.find_articles_sitemap_url(index_xml) == "https://coins.bank.gov.ua/sitemaparticles.xml"


def test_find_articles_sitemap_url_falls_back_when_nothing_matches():
    index_xml = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://coins.bank.gov.ua/sitemapproducts.xml</loc></sitemap>
    </sitemapindex>"""

    assert check_sitemap.find_articles_sitemap_url(index_xml) == check_sitemap.FALLBACK_ARTICLES_SITEMAP_URL


def test_parse_sitemap_articles_extracts_ids_urls_and_lastmod():
    xml_text = (FIXTURES / "sample_sitemap_articles.xml").read_text(encoding="utf-8")

    articles = check_sitemap.parse_sitemap_articles(xml_text)

    assert set(articles.keys()) == {"580", "933", "950"}
    assert articles["950"]["lastmod"] == "2026-08-30"
    assert articles["950"]["url"].endswith("a-950.html")


def test_parse_sitemap_articles_ignores_urls_without_article_id():
    xml_text = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://coins.bank.gov.ua/</loc></url>
    </urlset>"""

    assert check_sitemap.parse_sitemap_articles(xml_text) == {}


def test_extract_article_title_skips_empty_first_h1():
    html = (FIXTURES / "sample_article_page.html").read_text(encoding="utf-8")

    title = check_sitemap.extract_article_title(html, fallback="https://example.com")

    assert title == 'Пам\'ятна монета "Новинка, ще не на сайті"'


def test_extract_article_title_falls_back_to_title_tag_when_no_h1_text():
    html = "<html><head><title>Заголовок сторінки</title></head><body><h1></h1></body></html>"

    assert check_sitemap.extract_article_title(html, fallback="https://example.com") == "Заголовок сторінки"


def test_extract_article_title_falls_back_to_provided_default():
    html = "<html><body>no headings at all</body></html>"

    assert check_sitemap.extract_article_title(html, fallback="https://example.com/x") == "https://example.com/x"
