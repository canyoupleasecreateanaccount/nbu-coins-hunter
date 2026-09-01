"""Unit tests for scripts/http_client.py.

The site sits behind bot protection that rate-limits bursts, so the retry
and pacing behaviour here is what keeps a run from failing spuriously.
These tests never sleep for real or hit the network: the client takes an
injectable sleep function, and its session is replaced with a fake.
"""

import pytest
import requests

import http_client


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.text = "<html></html>"
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Stands in for requests.Session, returning a scripted sequence of responses."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0
        self.headers = {}
        self.proxies = {}

    def get(self, url, timeout=None):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        if isinstance(status, tuple):
            status_code, headers = status
            return FakeResponse(status_code, headers=headers)
        return FakeResponse(status)


def make_client(statuses):
    """Build a SiteClient whose network and sleeping are both faked out."""
    slept = []
    client = http_client.SiteClient(user_agent="test-agent", sleep=slept.append)
    client.session = FakeSession(statuses)
    return client, slept


def test_build_headers_includes_the_given_user_agent():
    headers = http_client.build_headers("my-agent")

    assert headers["User-Agent"] == "my-agent"
    assert headers["Accept-Language"] == "uk-UA,uk;q=0.9,en;q=0.8"


def test_client_picks_a_user_agent_from_the_known_list():
    client = http_client.SiteClient(sleep=lambda _: None)

    assert client.user_agent in http_client.USER_AGENTS


def test_get_returns_immediately_on_success():
    client, slept = make_client([200])

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert client.session.calls == 1


def test_get_retries_on_rate_limit_then_succeeds():
    # 429 is exactly what the site returns when a run's requests come too fast.
    client, slept = make_client([429, 200])

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert client.session.calls == 2
    assert slept, "expected a backoff sleep between attempts"


def test_get_with_retries_raises_immediately_on_403_without_backing_off():
    # Unlike 429/5xx, a 403 from Bunny Shield has never once turned out to
    # be transient in testing - backing off and retrying it just wastes
    # ~15-30s before failing anyway, so it should bail on the very first
    # attempt instead of working through the usual retry cycle.
    client, slept = make_client([(403, {}), 200])

    with pytest.raises(http_client.BunnyShieldBlocked):
        client._get_with_retries("https://example.com", timeout=30)

    assert client.session.calls == 1
    assert slept == []


def test_get_gives_up_and_raises_after_max_attempts():
    client, _ = make_client([429] * http_client.MAX_ATTEMPTS)

    with pytest.raises(requests.HTTPError):
        client.get("https://example.com")

    assert client.session.calls == http_client.MAX_ATTEMPTS


def test_get_backoff_grows_between_attempts():
    client, slept = make_client([429, 429, 200])

    client.get("https://example.com")

    # Only the backoff sleeps exceed the pacing interval; each retry waits
    # longer than the one before (the base doubles), so a persistently
    # throttled run backs off instead of hammering the site.
    backoffs = [s for s in slept if s > http_client.MIN_REQUEST_INTERVAL_SECONDS]
    assert len(backoffs) == 2
    assert backoffs[1] > backoffs[0]


def test_get_retries_on_connection_error():
    client, _ = make_client([requests.ConnectionError("boom"), 200])

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert client.session.calls == 2


def test_parse_plain_proxy_list_strips_scheme_prefix_and_blank_lines():
    text = "1.2.3.4:8080\nsocks5://5.6.7.8:1080\n\n  \n"

    assert http_client.parse_plain_proxy_list(text) == {"1.2.3.4:8080", "5.6.7.8:1080"}


class FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_geonode_candidates_excludes_entries_tagged_with_an_excluded_country(monkeypatch):
    payload = {
        "data": [
            {"ip": "1.1.1.1", "port": "8080", "country": "US"},
            {"ip": "2.2.2.2", "port": "3128", "country": "RU"},
            {"ip": "3.3.3.3", "port": "80", "country": "DE"},
        ]
    }
    monkeypatch.setattr(http_client.requests, "get", lambda *a, **kw: FakeJsonResponse(payload))

    candidates = http_client.fetch_geonode_candidates(excluded_ips=set())

    assert candidates == {"1.1.1.1:8080", "3.3.3.3:80"}


def test_fetch_geonode_candidates_also_excludes_ips_from_the_dedicated_country_list(monkeypatch):
    # Geonode's own "country" tag is not the only signal used - an IP
    # named by the dedicated per-country list is excluded even if Geonode
    # itself tags it as something else (e.g. a proxy tunnelled abroad).
    payload = {"data": [{"ip": "9.9.9.9", "port": "80", "country": "DE"}]}
    monkeypatch.setattr(http_client.requests, "get", lambda *a, **kw: FakeJsonResponse(payload))

    candidates = http_client.fetch_geonode_candidates(excluded_ips={"9.9.9.9"})

    assert candidates == set()


CHALLENGE_RESPONSE = (403, {"CDN-Challenge": "true", "ErrorCode": "112"})
PLAIN_BLOCK_RESPONSE = (403, {})


def test_get_falls_back_to_a_proxy_and_succeeds_without_touching_the_browser():
    # A working free proxy sidesteps the block entirely - no need to ever
    # reach for the (much heavier) headless-browser fallback.
    client, _ = make_client([CHALLENGE_RESPONSE])
    client._find_working_proxy = lambda url: FakeResponse(200)
    client._solve_challenge = lambda url: pytest.fail("should not be called when a proxy already worked")

    resp = client.get("https://example.com")

    assert resp.status_code == 200


def test_get_falls_back_to_solving_the_challenge_when_no_proxy_works(capsys):
    # A real Bunny Shield challenge page still comes back as HTTP 403, but
    # tagged with this header. A plain HTTP client can never solve it by
    # retrying, so once no free proxy works either, get() should fall back
    # to _solve_challenge() - faked out here so the test never touches a
    # real browser - and then try the request again.
    client, _ = make_client([CHALLENGE_RESPONSE])
    client._find_working_proxy = lambda url: None
    solved_with = []
    client._solve_challenge = solved_with.append

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert solved_with == ["https://example.com"]
    # One call hits the challenge (immediately, no backoff), then one more
    # succeeds once the (faked) challenge-solving has "run".
    assert client.session.calls == 2
    assert "solving Bunny Shield's JS challenge" in capsys.readouterr().out


def test_get_raises_if_challenge_persists_after_every_fallback():
    # The challenge-solving fallback only gets one try per get() call - if
    # the site still challenges every request afterwards (e.g. the
    # headless browser also got blocked), this must not retry forever.
    client, _ = make_client([CHALLENGE_RESPONSE, CHALLENGE_RESPONSE])
    client._find_working_proxy = lambda url: None
    client._solve_challenge = lambda url: None

    with pytest.raises(http_client.BunnyShieldBlocked):
        client.get("https://example.com")

    # One call for the initial direct attempt, one more for the retry
    # after (faked) challenge-solving - both hit the challenge immediately.
    assert client.session.calls == 2


def test_next_proxy_batch_does_not_hand_out_the_same_candidate_twice(monkeypatch):
    client = http_client.SiteClient(user_agent="test-agent", sleep=lambda _: None)
    monkeypatch.setattr(http_client, "fetch_candidate_proxies", lambda: ["1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"])

    first_batch = client._next_proxy_batch()
    second_batch = client._next_proxy_batch()

    assert set(first_batch) == {"1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"}
    assert second_batch == []


def test_get_finds_a_replacement_when_the_adopted_proxy_stops_working(capsys):
    # Free proxies are flaky: one that worked for an earlier URL in this
    # same run can still die on a later one. get() must drop it and look
    # for a replacement instead of letting the connection error crash the
    # whole run.
    # A fresh direct attempt follows the dead proxy (see the "no proxy
    # left" test below) - scripted to fail here too, so the flow actually
    # reaches the proxy search this test means to exercise.
    statuses = [requests.ConnectionError("proxy died")] * http_client.MAX_PROXY_REQUEST_ATTEMPTS + [
        PLAIN_BLOCK_RESPONSE
    ]
    client, _ = make_client(statuses)
    client.session.proxies = {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"}
    searched_with = []

    def fake_find(url):
        searched_with.append(url)
        return FakeResponse(200)

    client._find_working_proxy = fake_find

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert searched_with == ["https://example.com"]
    assert "the adopted proxy stopped working" in capsys.readouterr().out


def test_get_falls_back_to_the_browser_after_the_adopted_proxy_dies_with_no_replacement():
    # Once the adopted proxy dies and no replacement is found, get() falls
    # through to a fresh direct attempt - the only way left to know
    # whether coins.bank.gov.ua would even still challenge this specific
    # URL directly, which decides whether a headless browser is worth it.
    statuses = [requests.ConnectionError("proxy died")] * http_client.MAX_PROXY_REQUEST_ATTEMPTS + [
        CHALLENGE_RESPONSE
    ]
    client, _ = make_client(statuses)
    client.session.proxies = {"http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"}
    client._find_working_proxy = lambda url: None
    solved_with = []
    client._solve_challenge = solved_with.append

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert solved_with == ["https://example.com"]


def test_get_does_not_try_the_browser_for_a_plain_block_with_no_challenge():
    # A plain 403 with no CDN-Challenge header is not a JS challenge - a
    # headless browser cannot do anything a plain request could not, so
    # it should never even be attempted; only the proxy fallback can help.
    client, _ = make_client([PLAIN_BLOCK_RESPONSE])
    client._find_working_proxy = lambda url: None
    client._solve_challenge = lambda url: pytest.fail("should not be called for a plain block")

    with pytest.raises(http_client.BunnyShieldBlocked) as exc_info:
        client.get("https://example.com")

    assert exc_info.value.is_js_challenge is False


def test_get_does_not_retry_on_404():
    # A genuine "not found" is not transient - retrying it just wastes requests.
    client, _ = make_client([404])

    with pytest.raises(requests.HTTPError):
        client.get("https://example.com")

    assert client.session.calls == 1


def test_consecutive_requests_are_spaced_apart():
    client, slept = make_client([200, 200])

    client.get("https://example.com/1")
    client.get("https://example.com/2")

    # The second request waits out the minimum interval before firing.
    assert any(0 < s <= http_client.MIN_REQUEST_INTERVAL_SECONDS for s in slept)
