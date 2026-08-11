from datetime import datetime, timezone

import pytest
import requests

from data.provider_http import RequestRateLimiter, is_transient_request_error, request_error_kind, retry_delay_seconds


class _Response:
    def __init__(self, status=429, headers=None):
        self.status_code = status
        self.headers = dict(headers or {})


def test_retry_policy_distinguishes_transient_transport_and_http_failures():
    assert is_transient_request_error(requests.ReadTimeout("slow"))
    assert is_transient_request_error(requests.HTTPError("busy"), _Response(429))
    assert is_transient_request_error(requests.HTTPError("upstream"), _Response(503))
    assert not is_transient_request_error(requests.HTTPError("missing"), _Response(404))
    assert not is_transient_request_error(requests.exceptions.SSLError("certificate"))
    assert not is_transient_request_error(ValueError("schema"))
    assert request_error_kind(requests.HTTPError("busy"), _Response(429)) == "http_429"
    assert request_error_kind(requests.ReadTimeout("slow")) == "timeout"


def test_retry_delay_honours_delta_and_http_date_with_a_bound():
    def fixed_now():
        return datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)

    assert retry_delay_seconds(_Response(headers={"Retry-After": "9"}), attempt=0, base_seconds=2) == 9
    assert (
        retry_delay_seconds(
            _Response(headers={"Retry-After": "Tue, 11 Aug 2026 00:00:12 GMT"}),
            attempt=0,
            base_seconds=2,
            now=fixed_now,
        )
        == 12
    )
    assert retry_delay_seconds(_Response(headers={"Retry-After": "999"}), attempt=0, base_seconds=2) == 60
    assert retry_delay_seconds(_Response(headers={"Retry-After": "invalid"}), attempt=1, base_seconds=2) == 4


def test_rate_limiter_rejects_invalid_intervals():
    with pytest.raises(ValueError):
        RequestRateLimiter(-1)
