"""Shared HTTP client for talking to coins.bank.gov.ua.

The site sits behind BunnyCDN with Bunny Shield bot protection, which
matters a lot for how requests must be made:

* Requests without a browser-like User-Agent get HTTP 403 outright.
* A burst of independent, cookie-less requests gets rate-limited: in
  testing, roughly the ninth rapid request started returning HTTP 429,
  followed by 403 challenge pages. One run of this project makes a dozen
  requests, so this is not a theoretical concern.
* The shield sets a `bunny_shield_id_*` cookie. A real browser keeps that
  cookie across page loads; independent cookie-less requests look much
  more like a bot.
* No `Retry-After` header is sent, so backoff timings are our own choice.
* Traffic from cloud/hosting IP ranges (which is exactly what GitHub-hosted
  Actions runners are - Azure datacenter addresses - and most free public
  proxies are too) gets refused outright with a plain HTTP 403, or
  sometimes a *JavaScript* challenge instead: HTTP 403 with a
  `CDN-Challenge: true` header and a page titled "Establishing a secure
  connection ...". No amount of retrying, headers or session cookies gets
  a scripted HTTP client past either - a plain block cannot be solved at
  all from the same IP, and the JS challenge requires actually running the
  page's JavaScript, which is what `SiteClient._solve_challenge` uses a
  headless browser for (see below).

So every request in a run goes through one shared `requests.Session`
(persisting cookies), with a small delay between requests and retries
with exponential backoff on the responses the shield produces. If every
retry still gets refused with HTTP 403, the client falls back to:

1. Routing the request through a free public proxy instead (see
   `_find_working_proxy`) - most candidates are dead or blocked the same
   way, but trying up to a couple hundred (pooled from several public
   lists, several in flight at once, Russian-hosted ones filtered out)
   usually turns up one that is not, which sidesteps the IP-based block
   entirely. Free proxies are flaky, so an adopted one that later stops
   working mid-run is dropped and replaced from the same pool (see
   `get()`) rather than failing the run outright.
2. Only if no proxy is left working and the block was specifically the JS
   challenge (not a plain block, which a browser cannot do anything about
   either): solving it once with a headless browser (Playwright/Chromium)
   and copying the resulting cookies into the same session, so every
   actual page fetch - including non-HTML ones like sitemap.xml - still
   goes through plain, fast `requests` calls rather than a full browser.
"""

from __future__ import annotations

import concurrent.futures
import random
import time

import requests

#: Real browser/OS combinations. One is chosen per run (not per request):
#: a real browser does not change its User-Agent midway through a session,
#: and the shield would notice if ours did.
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

#: Status codes worth retrying: 429 (rate limited) and 403 (shield
#: challenge) are the ones this site actually returns under load; 5xx
#: covers ordinary transient server trouble.
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}

MAX_ATTEMPTS = 4

#: Seconds to wait before the first retry; doubled on each further attempt
#: (2s, 4s, 8s) plus a little jitter.
INITIAL_BACKOFF_SECONDS = 2.0

#: Polite pause between consecutive requests within one run, to stay well
#: under the burst threshold that triggers the shield.
MIN_REQUEST_INTERVAL_SECONDS = 1.5

#: How long to give the headless browser to load a page and let Bunny
#: Shield's challenge script finish running, in milliseconds.
CHALLENGE_PAGE_LOAD_TIMEOUT_MS = 30_000
CHALLENGE_SETTLE_TIMEOUT_MS = 8_000

#: Community-maintained lists of free public HTTP proxies, refreshed
#: regularly. Most entries are dead or already blocked by the same kind of
#: shield within hours of being listed - pooling several sources widens
#: the net; this is still a best-effort source, not a guaranteed one.
FREE_PROXY_LIST_URLS = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
]

#: Geonode's list tags each entry with a country, which is what makes the
#: EXCLUDED_PROXY_COUNTRIES filter below possible without a separate
#: geolocation lookup per candidate.
GEONODE_PROXY_LIST_URL = "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http"

#: A dedicated per-country list, used only to filter *other* sources
#: (above) that do not tag entries with a country themselves.
RUSSIAN_PROXY_LIST_URL = "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/countries/RU/proxies.txt"

#: This project has no business routing Ukrainian coin-shop traffic
#: through Russian infrastructure - excluded regardless of source. This is
#: a best-effort filter (by geolocated country, checked against two
#: sources), not a guarantee: a proxy neither source happens to know is
#: Russian will not be caught.
EXCLUDED_PROXY_COUNTRIES = {"RU"}

#: How many untested candidates to try per batch. In testing, only about
#: 1-3% of pooled candidates both work at all *and* get past this site's
#: shield, so finding one reliably needs a wide net. A client works
#: through the pool in batches of this size (see SiteClient._next_proxy_batch)
#: rather than all at once, since a proxy that tests fine can still go bad
#: partway through a run - the leftover, not-yet-tried candidates are what
#: makes recovering from that possible without refetching the lists.
MAX_PROXY_CANDIDATES = 200
PROXY_TEST_TIMEOUT_SECONDS = 8
PROXY_TEST_CONCURRENCY = 15

#: How many fresh batches of untested proxies to work through - in the
#: initial search and again each time an adopted proxy later goes bad -
#: before giving up on the proxy route entirely for a given request.
MAX_PROXY_BATCHES = 3

#: Once routed through an already-adopted proxy, a lower timeout and
#: attempt count than the direct-request defaults: a genuinely working
#: proxy responds quickly (it just proved that during selection), and
#: backing off to retry the *same* dead proxy rarely helps the way it
#: does for a real rate limit - abandoning it for a different one (see
#: SiteClient.get) is more productive than waiting it out.
PROXY_REQUEST_TIMEOUT_SECONDS = 10
MAX_PROXY_REQUEST_ATTEMPTS = 2


def build_headers(user_agent: str) -> dict[str, str]:
    """Return browser-like request headers built around ``user_agent``."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def is_bunny_shield_challenge(resp: requests.Response) -> bool:
    """Return True if ``resp`` is Bunny Shield's JS challenge page, not a plain block."""
    return resp.headers.get("CDN-Challenge") == "true"


def parse_plain_proxy_list(text: str) -> set[str]:
    """Parse a newline-separated proxy list into a set of bare "ip:port" strings.

    Some sources prefix entries with a scheme, e.g. "socks5://1.2.3.4:1080"
    - only the "ip:port" part is comparable across sources, so that prefix
    (if any) is stripped.
    """
    return {line.strip().rsplit("://", 1)[-1] for line in text.splitlines() if line.strip()}


def fetch_excluded_proxy_ips() -> set[str]:
    """Return IPs known to be in EXCLUDED_PROXY_COUNTRIES, per the dedicated per-country list."""
    try:
        resp = requests.get(RUSSIAN_PROXY_LIST_URL, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return set()
    return {entry.split(":", 1)[0] for entry in parse_plain_proxy_list(resp.text)}


def fetch_geonode_candidates(excluded_ips: set[str]) -> set[str]:
    """Return "ip:port" candidates from Geonode's list, skipping excluded countries/IPs."""
    try:
        resp = requests.get(GEONODE_PROXY_LIST_URL, timeout=15)
        resp.raise_for_status()
        entries = resp.json().get("data", [])
    except (requests.RequestException, ValueError):
        return set()

    candidates = set()
    for entry in entries:
        ip, port, country = entry.get("ip"), entry.get("port"), entry.get("country")
        if not ip or not port:
            continue
        if country in EXCLUDED_PROXY_COUNTRIES or ip in excluded_ips:
            continue
        candidates.add(f"{ip}:{port}")
    return candidates


def fetch_candidate_proxies() -> list[str]:
    """Return every "ip:port" candidate pooled from several free proxy list sources, shuffled.

    Not limited to any particular count: a SiteClient works through the
    result in batches (see ``SiteClient._next_proxy_batch``), so the whole
    pool stays available to recover from a proxy that goes bad partway
    through a run, not just the first attempt.

    See EXCLUDED_PROXY_COUNTRIES for why some candidates never make it in.
    A source that fails to load (network hiccup, format change, ...) is
    simply skipped rather than failing the whole run - the point of
    pooling several is that no single one is relied on.
    """
    excluded_ips = fetch_excluded_proxy_ips()
    candidates = fetch_geonode_candidates(excluded_ips)

    for url in FREE_PROXY_LIST_URLS:
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        for entry in parse_plain_proxy_list(resp.text):
            if entry.split(":", 1)[0] not in excluded_ips:
                candidates.add(entry)

    candidates = list(candidates)
    random.shuffle(candidates)
    return candidates


class BunnyShieldBlocked(requests.HTTPError):
    """Raised when every direct retry still gets refused by Bunny Shield with HTTP 403.

    ``is_js_challenge`` distinguishes a solvable JS challenge
    (``CDN-Challenge`` header present) from a plain, harder block with no
    challenge offered at all - only a proxy (which changes the IP the
    block is keyed on) can do anything about the latter.

    A subclass of ``requests.HTTPError`` so existing callers that only
    catch that (or plain ``Exception``) keep working unchanged.
    """

    def __init__(self, message: str, response: requests.Response, is_js_challenge: bool):
        super().__init__(message, response=response)
        self.is_js_challenge = is_js_challenge


class SiteClient:
    """A polite, retrying HTTP client that reuses one session for a whole run.

    Create one per run and pass it around; it keeps the shield's cookies,
    spaces requests out, retries the failures the shield produces, and
    falls back to a free proxy or (for a JS challenge specifically) a
    headless browser if direct requests keep getting refused.
    """

    def __init__(self, user_agent: str | None = None, sleep=time.sleep):
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.session = requests.Session()
        self.session.headers.update(build_headers(self.user_agent))
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._challenge_solved = False
        self._proxy_pool: list[str] | None = None
        self._tried_proxies: set[str] = set()

    def _wait_between_requests(self) -> None:
        """Sleep just enough that consecutive requests stay politely spaced."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _get_with_retries(self, url: str, timeout: int, max_attempts: int = MAX_ATTEMPTS) -> requests.Response:
        """GET ``url``, retrying with exponential backoff on shield/transient errors.

        Raises BunnyShieldBlocked if the last attempt is still refused with
        HTTP 403 (a signal worth reacting to with the fallbacks below,
        rather than failing outright), or the plain
        ``requests.HTTPError``/connection error for anything else.
        """
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(1, max_attempts + 1):
            self._wait_between_requests()
            is_last_attempt = attempt == max_attempts
            reason: str | None = None

            try:
                resp = self.session.get(url, timeout=timeout)
                self._last_request_at = time.monotonic()
            except requests.RequestException as exc:
                # Connection reset, DNS hiccup, timeout - always transient.
                self._last_request_at = time.monotonic()
                if is_last_attempt:
                    raise
                reason = type(exc).__name__
            else:
                if resp.status_code not in RETRY_STATUS_CODES:
                    # Either a success, or a genuine error (404, ...) that
                    # retrying cannot fix - raise_for_status decides which.
                    resp.raise_for_status()
                    return resp
                if is_last_attempt:
                    if resp.status_code == 403:
                        is_challenge = is_bunny_shield_challenge(resp)
                        kind = "JS challenge" if is_challenge else "block"
                        raise BunnyShieldBlocked(
                            f"Bunny Shield {kind} for {url}", response=resp, is_js_challenge=is_challenge
                        )
                    resp.raise_for_status()
                    return resp
                reason = f"HTTP {resp.status_code}"

            print(f"  {url} -> {reason}, retry {attempt}/{max_attempts - 1} in {backoff:.1f}s")
            self._sleep(backoff + random.uniform(0, 1))
            backoff *= 2

        raise RuntimeError(f"Failed to fetch {url}")

    def _next_proxy_batch(self) -> list[str]:
        """Return up to MAX_PROXY_CANDIDATES not-yet-tried proxies from the shared pool.

        Fetches the full pool once per client (cached in ``_proxy_pool``)
        and remembers every candidate ever handed out here (``_tried_proxies``),
        successful or not, so later calls - e.g. because a previously
        adopted proxy went bad on a later request - make progress through
        fresh candidates instead of re-testing dead ones. Returns an empty
        list once the whole pool has been exhausted.
        """
        if self._proxy_pool is None:
            try:
                self._proxy_pool = fetch_candidate_proxies()
            except requests.RequestException:
                self._proxy_pool = []

        untested = [candidate for candidate in self._proxy_pool if candidate not in self._tried_proxies]
        batch = untested[:MAX_PROXY_CANDIDATES]
        self._tried_proxies.update(batch)
        return batch

    def _find_working_proxy(self, url: str) -> requests.Response | None:
        """Try untested free public proxies concurrently until one can fetch ``url``, and adopt it.

        Tests candidates against the real ``url`` (not a throwaway probe),
        so the first one that works has already fetched what we needed -
        its response is returned directly. Leaves this client's session
        using that same proxy for the rest of the run (until ``get()``
        notices it stopped working and calls this again). Returns None if
        none of the batch worked, which is the common case.
        """
        candidates = self._next_proxy_batch()
        if not candidates:
            return None

        def try_candidate(candidate: str) -> tuple[str, requests.Response] | None:
            proxy_url = f"http://{candidate}"
            try:
                resp = requests.get(
                    url,
                    headers=dict(self.session.headers),
                    proxies={"http": proxy_url, "https": proxy_url},
                    timeout=PROXY_TEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException:
                return None
            return (proxy_url, resp) if resp.status_code == 200 else None

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=PROXY_TEST_CONCURRENCY)
        found: tuple[str, requests.Response] | None = None
        try:
            futures = [executor.submit(try_candidate, candidate) for candidate in candidates]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    found = result
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if found is None:
            return None
        proxy_url, resp = found
        print(f"  {url} -> found a working free proxy ({proxy_url})")
        self.session.proxies = {"http": proxy_url, "https": proxy_url}
        return resp

    def _solve_challenge(self, url: str) -> None:
        """Load ``url`` in a headless browser once to pass Bunny Shield's JS challenge.

        Copies the cookies the browser ends up with into this client's
        plain ``requests.Session``, so every subsequent ``get()`` call -
        including for non-HTML URLs like sitemap.xml, which a browser
        would otherwise wrap in its own XML viewer - goes back to fast,
        ordinary HTTP requests instead of needing a browser each time.

        A no-op after the first successful call, since one solved
        challenge's cookies are good for the rest of this run.
        """
        if self._challenge_solved:
            return

        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                context = browser.new_context(user_agent=self.user_agent)
                page = context.new_page()
                page.goto(url, wait_until="load", timeout=CHALLENGE_PAGE_LOAD_TIMEOUT_MS)
                # The challenge page runs a script and redirects to the real
                # page once it passes; goto() only waits for the challenge
                # page's own load, so give the redirect a moment to happen.
                page.wait_for_timeout(CHALLENGE_SETTLE_TIMEOUT_MS)
                cookies = context.cookies()
            finally:
                browser.close()

        for cookie in cookies:
            self.session.cookies.set(
                cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/")
            )
        self._challenge_solved = True

    def get(self, url: str, timeout: int = 30) -> requests.Response:
        """GET ``url``, retrying on shield/transient errors and falling back if every retry is blocked.

        If this client already adopted a working proxy for an earlier
        call, that is tried first (a lower timeout/attempt budget than a
        direct request, since a genuinely working proxy responds quickly,
        and a dead one is more productive to abandon than to wait out).
        Free proxies are flaky, so one that worked before can still stop
        working mid-run - when that happens, it is dropped and a
        replacement is searched for, same as if there had been no proxy
        at all.

        On a persistent HTTP 403 with no (or no more) working proxy
        available, falls back to a headless browser - but only for a JS
        challenge specifically, since a browser cannot do anything a
        proxy-less plain block either.

        Raises the final ``requests.HTTPError`` (or connection error) if
        every attempt still fails, so a persistently blocked run fails
        loudly rather than silently returning nothing to parse.
        """
        is_challenge: bool | None
        if self.session.proxies:
            try:
                return self._get_with_retries(
                    url, PROXY_REQUEST_TIMEOUT_SECONDS, max_attempts=MAX_PROXY_REQUEST_ATTEMPTS
                )
            except (BunnyShieldBlocked, requests.RequestException) as exc:
                print(f"  {url} -> the adopted proxy stopped working ({type(exc).__name__}), looking for another...")
                self.session.proxies = {}
            # Unknown here: the failure was proxy-side, not a fresh read of
            # whether coins.bank.gov.ua itself would even still block us
            # directly for this URL - resolved below if no proxy is found.
            is_challenge = None
        else:
            try:
                return self._get_with_retries(url, timeout)
            except BunnyShieldBlocked as exc:
                print(f"  {url} -> {exc}, trying a free public proxy...")
                is_challenge = exc.is_js_challenge
                last_error = exc

        for _ in range(MAX_PROXY_BATCHES):
            proxy_resp = self._find_working_proxy(url)
            if proxy_resp is not None:
                return proxy_resp

        if is_challenge is None:
            try:
                return self._get_with_retries(url, timeout)
            except BunnyShieldBlocked as exc:
                is_challenge = exc.is_js_challenge
                last_error = exc

        if not is_challenge:
            raise last_error

        print(f"  {url} -> no working proxy found, solving Bunny Shield's JS challenge with a headless browser...")
        self._solve_challenge(url)
        return self._get_with_retries(url, timeout)
