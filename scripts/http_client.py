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
  Actions runners are - Azure datacenter addresses) can get a *JavaScript*
  challenge instead of a plain block: HTTP 403 with a `CDN-Challenge: true`
  header and a page titled "Establishing a secure connection ...". No
  amount of retrying, headers or session cookies gets a scripted HTTP
  client past that - it requires actually running the page's JavaScript,
  which is what `SiteClient._solve_challenge` uses a headless browser for
  (see below).

So every request in a run goes through one shared `requests.Session`
(persisting cookies), with a small delay between requests and retries
with exponential backoff on the responses the shield produces. If the
final retry is specifically the JS challenge, the client falls back to
solving it once with a headless browser (Playwright/Chromium) and copies
the resulting cookies into the same session, so every actual page fetch -
including non-HTML ones like sitemap.xml - still goes through plain,
fast `requests` calls rather than a full browser.
"""

from __future__ import annotations

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


class BunnyShieldChallenge(requests.HTTPError):
    """Raised when every retry still gets Bunny Shield's JS challenge page.

    A subclass of ``requests.HTTPError`` so existing callers that only
    catch that (or plain ``Exception``) keep working unchanged.
    """


class SiteClient:
    """A polite, retrying HTTP client that reuses one session for a whole run.

    Create one per run and pass it around; it keeps the shield's cookies,
    spaces requests out, retries the failures the shield produces, and
    falls back to a headless browser if it runs into a JS challenge that
    plain retries cannot solve.
    """

    def __init__(self, user_agent: str | None = None, sleep=time.sleep):
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.session = requests.Session()
        self.session.headers.update(build_headers(self.user_agent))
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._challenge_solved = False

    def _wait_between_requests(self) -> None:
        """Sleep just enough that consecutive requests stay politely spaced."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def _get_with_retries(self, url: str, timeout: int) -> requests.Response:
        """GET ``url``, retrying with exponential backoff on shield/transient errors.

        Raises BunnyShieldChallenge if the last attempt is specifically
        Bunny Shield's JS challenge page (a signal worth reacting to
        differently from a plain, unsolvable block), or the plain
        ``requests.HTTPError``/connection error for anything else.
        """
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_between_requests()
            is_last_attempt = attempt == MAX_ATTEMPTS
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
                    if is_bunny_shield_challenge(resp):
                        raise BunnyShieldChallenge(
                            f"Bunny Shield JS challenge (ErrorCode={resp.headers.get('ErrorCode')}) for {url}",
                            response=resp,
                        )
                    resp.raise_for_status()
                    return resp
                reason = f"HTTP {resp.status_code}"

            print(f"  {url} -> {reason}, retry {attempt}/{MAX_ATTEMPTS - 1} in {backoff:.1f}s")
            self._sleep(backoff + random.uniform(0, 1))
            backoff *= 2

        raise RuntimeError(f"Failed to fetch {url}")

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
        """GET ``url``, retrying on shield/transient errors and solving a JS challenge if hit.

        Raises the final ``requests.HTTPError`` (or connection error) if
        every attempt - including the post-challenge retry - still fails,
        so a persistently blocked run fails loudly rather than silently
        returning nothing to parse.
        """
        try:
            return self._get_with_retries(url, timeout)
        except BunnyShieldChallenge:
            print(f"  {url} -> solving Bunny Shield's JS challenge with a headless browser...")
            self._solve_challenge(url)
        return self._get_with_retries(url, timeout)
