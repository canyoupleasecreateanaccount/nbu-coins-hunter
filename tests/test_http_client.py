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


def test_get_retries_on_shield_403_then_succeeds():
    client, slept = make_client([403, 403, 200])

    resp = client.get("https://example.com")

    assert resp.status_code == 200
    assert client.session.calls == 3


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


def test_get_logs_bunny_shield_js_challenge_on_final_failure(capsys):
    # A real Bunny Shield challenge page still comes back as HTTP 403, but
    # tagged with this header - worth calling out explicitly in the run log,
    # since no amount of retrying a plain HTTP client can solve it.
    challenge_response = (403, {"CDN-Challenge": "true", "ErrorCode": "112"})
    client, _ = make_client([challenge_response] * http_client.MAX_ATTEMPTS)

    with pytest.raises(requests.HTTPError):
        client.get("https://example.com")

    assert "Bunny Shield JS challenge" in capsys.readouterr().out


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
