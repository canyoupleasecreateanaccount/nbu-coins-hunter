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

So every request in a run goes through one shared `requests.Session`
(persisting cookies), with a small delay between requests and retries
with exponential backoff on the responses the shield produces.
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


def build_headers(user_agent: str) -> dict[str, str]:
    """Return browser-like request headers built around ``user_agent``."""
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


class SiteClient:
    """A polite, retrying HTTP client that reuses one session for a whole run.

    Create one per run and pass it around; it keeps the shield's cookies,
    spaces requests out, and retries the failures the shield produces.
    """

    def __init__(self, user_agent: str | None = None, sleep=time.sleep):
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.session = requests.Session()
        self.session.headers.update(build_headers(self.user_agent))
        self._sleep = sleep
        self._last_request_at: float | None = None

    def _wait_between_requests(self) -> None:
        """Sleep just enough that consecutive requests stay politely spaced."""
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def get(self, url: str, timeout: int = 30) -> requests.Response:
        """GET ``url``, retrying with exponential backoff on shield/transient errors.

        Raises the final ``requests.HTTPError`` (or connection error) if
        every attempt fails, so a persistently blocked run fails loudly
        rather than silently returning nothing to parse.
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
                    resp.raise_for_status()
                    return resp
                reason = f"HTTP {resp.status_code}"

            print(f"  {url} -> {reason}, retry {attempt}/{MAX_ATTEMPTS - 1} in {backoff:.1f}s")
            self._sleep(backoff + random.uniform(0, 1))
            backoff *= 2

        raise RuntimeError(f"Failed to fetch {url}")
