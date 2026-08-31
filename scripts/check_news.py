"""Check the NBU coin shop news feed for newly published coin-related articles.

This script polls https://coins.bank.gov.ua/novini/t-14.html, compares the
articles it finds against a small JSON state file of previously seen article
IDs, and sends a Telegram message for every new article whose title matches a
configurable keyword filter (coin-related news, by default).

It is designed to be run repeatedly (e.g. every 15 minutes from a GitHub
Actions workflow) rather than kept running as a long-lived process.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

from http_client import SiteClient
from telegram_notify import format_message, send_telegram

BASE_URL = "https://coins.bank.gov.ua/novini/t-14.html"

#: Fetch five listing pages (20 articles) instead of just one. The site
#: does not list articles in strict chronological order (a pinned article
#: always occupies the first slot, and article IDs are only roughly, not
#: strictly, descending - e.g. article 915 is listed after 916), so a
#: generous margin avoids missing an article that lands slightly further
#: back than expected.
PAGE_URLS = [BASE_URL] + [f"{BASE_URL}?page={page}" for page in range(2, 6)]

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_news.json"

#: Case-insensitive substrings a title must contain (any of them) to be
#: treated as coin-related news. Overridable via the KEYWORDS environment
#: variable, e.g. "монет,банкнот".
#:
#: Matching is by substring, so a stem is enough and listing declensions
#: adds nothing: "монет" already matches "монета", "монети", "монету" and
#: so on. Only add an entry for a genuinely different word.
DEFAULT_KEYWORDS = ["монет"]

def get_keywords() -> list[str]:
    """Read the keyword filter from the KEYWORDS env var, or fall back to the default."""
    raw = os.environ.get("KEYWORDS")
    if not raw:
        return list(DEFAULT_KEYWORDS)
    return [kw.strip().lower() for kw in raw.split(",") if kw.strip()]


def parse_articles_page(html: str) -> dict[str, dict]:
    """Parse one news-listing page into a mapping of article ID -> article info.

    Each article dict has the keys ``id``, ``title``, ``url`` and ``date``.
    Articles whose link does not match the expected ``a-<id>.html`` pattern
    are skipped (defensive against unrelated links on the page).
    """
    articles: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")
    for block in soup.select(".article_listing .text-articles-listing"):
        link = block.find("a")
        if not link or not link.get("href"):
            continue
        href = link["href"]
        match = re.search(r"a-(\d+)\.html", href)
        if not match:
            continue
        article_id = match.group(1)
        title = link.get_text(strip=True)
        date_tag = block.find("small")
        date = date_tag.get_text(strip=True) if date_tag else ""
        full_url = href if href.startswith("http") else f"https://coins.bank.gov.ua/{href}"
        articles[article_id] = {
            "id": article_id,
            "title": title,
            "url": full_url,
            "date": date,
        }
    return articles


def fetch_articles(client: SiteClient, urls: Iterable[str] = PAGE_URLS) -> dict[str, dict]:
    """Download and parse every page in ``urls`` via ``client``, merging the results.

    Raises ``requests.HTTPError`` if a page still fails after the client's
    retries, so a run blocked by the site's bot shield fails loudly rather
    than quietly reporting "no news".
    """
    articles: dict[str, dict] = {}
    for url in urls:
        resp = client.get(url)
        articles.update(parse_articles_page(resp.text))
    return articles


def load_state(path: Path = STATE_FILE) -> dict:
    """Load the seen-articles state file, or return an empty state if it does not exist."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"seen_ids": []}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    """Write the state dict to ``path`` as pretty-printed, UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def matches_keywords(title: str, keywords: Iterable[str]) -> bool:
    """Return True if ``title`` contains any of ``keywords`` (case-insensitive)."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in keywords)


def select_new_matching_articles(
    articles: dict[str, dict], seen_ids: set[str], keywords: Iterable[str]
) -> list[dict]:
    """Return articles that are not in ``seen_ids`` and match ``keywords``, oldest first."""
    keywords = list(keywords)
    new_articles = [a for aid, a in articles.items() if aid not in seen_ids]
    matching = [a for a in new_articles if matches_keywords(a["title"], keywords)]
    return sorted(matching, key=lambda a: int(a["id"]))


def main() -> None:
    """Entry point: fetch news, notify about new coin articles, and persist the new state."""
    keywords = get_keywords()
    client = SiteClient()
    articles = fetch_articles(client)
    if not articles:
        print("No articles parsed - page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    is_first_run = len(seen_ids) == 0

    if is_first_run:
        print(f"First run: baselining {len(articles)} articles without notifying.")
    else:
        new_ids = [aid for aid in articles if aid not in seen_ids]
        coin_articles = select_new_matching_articles(articles, seen_ids, keywords)
        for article in coin_articles:
            text = format_message(
                "Нова монета на сайті НБУ",
                {
                    "Назва": article["title"],
                    "Дата": article["date"],
                    "Посилання": article["url"],
                },
            )
            try:
                send_telegram(text)
            except Exception:
                # Record the articles already delivered before re-raising, so
                # the retry on the next run does not send them a second time.
                # Everything not yet notified stays unseen and is retried.
                seen_ids.add(article["id"])
                state["seen_ids"] = sorted(seen_ids, key=int)
                save_state(state)
                raise
            seen_ids.add(article["id"])
            print(f"Notified: {article['title']}")
        if new_ids and not coin_articles:
            print(f"{len(new_ids)} new article(s), none matched keywords {keywords}.")
        elif not new_ids:
            print("No new articles.")

    seen_ids.update(articles.keys())
    state["seen_ids"] = sorted(seen_ids, key=int)
    save_state(state)


if __name__ == "__main__":
    main()
