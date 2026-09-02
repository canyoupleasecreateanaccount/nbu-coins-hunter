"""Check the NBU coin sitemap for articles that go live before the news listing shows them.

The public news listing (https://coins.bank.gov.ua/novini/t-14.html, handled
by check_news.py) can lag behind: an article's own page - and its entry in
https://coins.bank.gov.ua/sitemap.xml - sometimes exists before the article
is inserted into that listing. This script polls the site's dedicated
article sitemap, and for any article ID that is new since the last run *and*
not present in a fresh fetch of the news listing, fetches the article page
itself to read its real title, and - if that title matches the same keyword
filter used by check_news.py - sends a distinct "spotted in the sitemap,
not on the site yet" Telegram notification.

Like check_news.py, it is meant to be run repeatedly (e.g. every 2 hours
from a GitHub Actions workflow) rather than kept running as a long-lived
process. It keeps its own state file so it works independently of
check_news.py's run history.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from check_news import PAGE_URLS, fetch_articles, get_keywords, matches_keywords
from http_client import SiteClient
from telegram_notify import format_message, send_telegram

SITEMAP_INDEX_URL = "https://coins.bank.gov.ua/sitemap.xml"

#: Used only if the sitemap index no longer contains an entry whose URL
#: obviously refers to articles (see find_articles_sitemap_url).
FALLBACK_ARTICLES_SITEMAP_URL = "https://coins.bank.gov.ua/sitemaparticles.xml"

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_sitemap.json"

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def find_articles_sitemap_url(index_xml: str) -> str:
    """Return the <loc> of the sub-sitemap that looks like it lists articles.

    Falls back to FALLBACK_ARTICLES_SITEMAP_URL if no <loc> in the index
    obviously matches, so a naming change upstream does not silently stop
    this check from working.
    """
    root = ET.fromstring(index_xml)
    locs = [el.text.strip() for el in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS) if el.text]
    for loc in locs:
        if "article" in loc.lower():
            return loc
    return FALLBACK_ARTICLES_SITEMAP_URL


def parse_sitemap_articles(xml_text: str) -> dict[str, dict]:
    """Parse a sitemap <urlset> into a mapping of article ID -> {id, url, lastmod}.

    Only <url> entries whose <loc> ends in the site's article pattern
    (.../a-<digits>.html) are kept; anything else in the sitemap is ignored.
    """
    root = ET.fromstring(xml_text)
    articles: dict[str, dict] = {}
    for url_el in root.findall("sm:url", SITEMAP_NS):
        loc_el = url_el.find("sm:loc", SITEMAP_NS)
        if loc_el is None or not loc_el.text:
            continue
        loc = loc_el.text.strip()
        match = re.search(r"a-(\d+)\.html$", loc)
        if not match:
            continue
        lastmod_el = url_el.find("sm:lastmod", SITEMAP_NS)
        lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else ""
        article_id = match.group(1)
        articles[article_id] = {"id": article_id, "url": loc, "lastmod": lastmod}
    return articles


def fetch_sitemap_articles(client: SiteClient) -> dict[str, dict]:
    """Download the sitemap index, follow it to the articles sitemap, and parse it."""
    index_resp = client.get(SITEMAP_INDEX_URL)
    articles_url = find_articles_sitemap_url(index_resp.text)

    articles_resp = client.get(articles_url)
    return parse_sitemap_articles(articles_resp.text)


def extract_article_title(html: str, fallback: str) -> str:
    """Pull the real headline out of an article page.

    The page markup has two <h1> tags: an always-empty "category_heading"
    placeholder first, then "pageHeading" with the actual title - so the
    first non-empty <h1> is preferred over blindly taking the first <h1>.
    Falls back to the <title> tag, then to ``fallback`` (e.g. the URL).
    """
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all("h1"):
        text = heading.get_text(strip=True)
        if text:
            return text
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return fallback


def fetch_article_title(client: SiteClient, url: str) -> str:
    """Fetch an individual article page and return its real headline."""
    resp = client.get(url)
    return extract_article_title(resp.text, fallback=url)


def load_state(path: Path = STATE_FILE) -> dict:
    """Load the seen-sitemap-articles state file, or return an empty state if missing."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen_ids": []}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    """Write the state dict to ``path`` as pretty-printed, UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    """Entry point: fetch the sitemap, notify about early coin articles, persist state."""
    keywords = get_keywords()
    client = SiteClient()

    sitemap_articles = fetch_sitemap_articles(client)
    if not sitemap_articles:
        print("No articles parsed from the sitemap - its structure may have changed.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    is_first_run = len(seen_ids) == 0

    if is_first_run:
        print(f"First run: baselining {len(sitemap_articles)} sitemap articles without notifying.")
    else:
        new_ids = sorted((aid for aid in sitemap_articles if aid not in seen_ids), key=int)
        if not new_ids:
            # Nothing new in the sitemap, so there is no reason to fetch the
            # news listing at all - this is the common case, and skipping it
            # keeps a routine run down to two requests instead of seven.
            print("No new sitemap articles.")
        else:
            # A fresh, independent fetch of the news listing - not shared
            # state with check_news.py - so this script works standalone.
            news_articles = fetch_articles(client, PAGE_URLS)

            for article_id in new_ids:
                if article_id in news_articles:
                    # Already on the news listing too - check_news.py will
                    # notify about it (or already has), so this is not "early".
                    continue
                article = sitemap_articles[article_id]
                title = fetch_article_title(client, article["url"])
                if not matches_keywords(title, keywords):
                    continue
                text = format_message(
                    "З'явилась стаття в sitemap НБУ, очікуємо на публікацію в новинах на сайті",
                    {
                        "Назва": title,
                        "Дата зміни": article["lastmod"],
                        "Посилання": article["url"],
                    },
                )
                try:
                    send_telegram(text)
                except Exception:
                    # Record this one as seen before re-raising, so the retry
                    # on the next run does not notify about it twice.
                    seen_ids.add(article_id)
                    state["seen_ids"] = sorted(seen_ids, key=int)
                    save_state(state)
                    raise
                seen_ids.add(article_id)
                print(f"Notified (sitemap-early): {title}")

    seen_ids.update(sitemap_articles.keys())
    state["seen_ids"] = sorted(seen_ids, key=int)
    save_state(state)


if __name__ == "__main__":
    main()
