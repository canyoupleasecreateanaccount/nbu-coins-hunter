"""Check the NBU coin shop catalog for items that are actually up for sale.

This script is different in kind from check_news.py and check_sitemap.py:
those two watch for *announcements* that a coin exists, this one watches
the shop itself (https://coins.bank.gov.ua/catalog.html) for items that can
actually be bought right now. NBU typically publishes a news article about
a coin days or weeks before it goes on sale, and the catalog is usually
very sparse (often under ten items site-wide, since popular releases sell
out within minutes) - so a new listing here is the real, time-sensitive
signal collectors are chasing, not just an announcement.

catalog.html aggregates every sellable category on the site (coins,
banknotes, souvenirs, medals - anything with a price and an "add to cart"
button), so this script does not apply the KEYWORDS title filter that
check_news.py and check_sitemap.py use: unlike news headlines, product
names on this site rarely contain a word like "монета" at all (e.g. a coin
is simply named after its theme, such as `"Архістратиг Михаїл" (з)`), so
that filter would silently hide real coin listings instead of narrowing
them down.

Unlike the other two scripts' state files, seen_lots.json is not an
ever-growing set of IDs: it is a snapshot of what the catalog looked like
on the previous run. A product ID that drops out of the catalog (sold out)
and later reappears (restocked) is treated as new again - which is exactly
what should happen, since a restock is another real chance to buy it.

Like check_news.py and check_sitemap.py, it is designed to be run
repeatedly (e.g. every 15 minutes from a GitHub Actions workflow) rather
than kept running as a long-lived process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from http_client import SiteClient
from telegram_notify import format_message, send_telegram

BASE_URL = "https://coins.bank.gov.ua/catalog.html"

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_lots.json"

#: Safety cap on how many catalog pages fetch_products will follow, in case
#: the site's pagination ever loops back on itself. The catalog normally
#: fits on a single page - the whole shop rarely holds more than a
#: handful of items at once - so this is a defensive limit, not a target.
MAX_PAGES = 20


def parse_catalog_page(html: str) -> dict[str, dict]:
    """Parse one catalog-listing page into a mapping of product ID -> product info.

    Each product dict has the keys ``id``, ``title``, ``url`` and ``price``.
    Blocks without a recognizable ``p-<id>.html`` product link are skipped
    (defensive against unrelated cards on the page).
    """
    products: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")
    for block in soup.select(".product__name"):
        link = block.find("a", class_="model_product")
        if not link or not link.get("href"):
            continue
        href = link["href"]
        match = re.search(r"p-(\d+)\.html", href)
        if not match:
            continue
        product_id = match.group(1)
        title = link.get_text(strip=True)
        price_tag = block.find("span", class_="new_price")
        price = price_tag.get_text(strip=True) if price_tag else ""
        full_url = href if href.startswith("http") else f"https://coins.bank.gov.ua{href}"
        products[product_id] = {
            "id": product_id,
            "title": title,
            "url": full_url,
            "price": price,
        }
    return products


def find_next_page_url(html: str) -> str | None:
    """Return the URL of the next catalog page, or None if this is the last page."""
    soup = BeautifulSoup(html, "html.parser")
    next_link = soup.select_one(".pagination_block a.next_page")
    if next_link and next_link.get("href"):
        return next_link["href"]
    return None


def fetch_products(client: SiteClient, start_url: str = BASE_URL) -> dict[str, dict]:
    """Download and parse the whole catalog via ``client``, following pagination.

    Follows the "next page" link for as long as the site provides one (up
    to MAX_PAGES), instead of always requesting a fixed number of pages -
    a routine run costs a single request, since the catalog normally fits
    on one page.

    Raises ``requests.HTTPError`` if a page still fails after the client's
    retries, so a run blocked by the site's bot shield fails loudly rather
    than quietly reporting "nothing in stock".
    """
    products: dict[str, dict] = {}
    url: str | None = start_url
    for _ in range(MAX_PAGES):
        if url is None:
            break
        resp = client.get(url)
        products.update(parse_catalog_page(resp.text))
        url = find_next_page_url(resp.text)
    return products


def load_state(path: Path = STATE_FILE) -> dict:
    """Load the previous run's catalog snapshot, or an empty one if it does not exist.

    The returned dict's ``baselined`` key - not just file existence - is
    what main() uses to decide whether this is the first real run: the
    state file itself ships committed to the repo (like the other two
    scripts' state files), pre-seeded with an empty snapshot and no
    ``baselined`` key, so checking mere file existence would never detect
    a first run at all, for this repo or any fork of it.
    """
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"available_ids": [], "baselined": False}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    """Write the state dict to ``path`` as pretty-printed, UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def select_new_lots(products: dict[str, dict], previous_ids: set[str]) -> list[dict]:
    """Return products in ``products`` that are not in ``previous_ids``, oldest ID first."""
    new_products = [p for pid, p in products.items() if pid not in previous_ids]
    return sorted(new_products, key=lambda p: int(p["id"]))


def main() -> None:
    """Entry point: fetch the catalog, notify about newly listed lots, and persist the snapshot."""
    client = SiteClient()
    products = fetch_products(client)

    # Unlike check_news.py/check_sitemap.py, an empty snapshot here is a
    # legitimate steady state (nothing currently for sale), not just the
    # first run's starting point - so "first run" is tracked with its own
    # explicit flag instead of reusing "no IDs seen yet".
    state = load_state()
    is_first_run = not state.get("baselined", False)
    previous_ids = set(state.get("available_ids", []))
    state["baselined"] = True

    if is_first_run:
        print(f"First run: baselining {len(products)} catalog item(s) without notifying.")
    else:
        new_lots = select_new_lots(products, previous_ids)
        for lot in new_lots:
            text = format_message(
                "Новий лот у продажу на сайті НБУ",
                {
                    "Назва": lot["title"],
                    "Ціна": lot["price"],
                    "Посилання": lot["url"],
                },
            )
            try:
                send_telegram(text)
            except Exception:
                # Record the lots already delivered before re-raising, so
                # the retry on the next run does not send them a second
                # time. Everything not yet notified stays out of this
                # saved snapshot and is retried next run.
                previous_ids.add(lot["id"])
                state["available_ids"] = sorted(previous_ids, key=int)
                save_state(state)
                raise
            previous_ids.add(lot["id"])
            print(f"Notified: {lot['title']}")
        if not new_lots:
            print("No new catalog items.")

    state["available_ids"] = sorted(products.keys(), key=int)
    save_state(state)


if __name__ == "__main__":
    main()
