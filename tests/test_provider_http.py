from datetime import datetime, timezone

import pytest
import requests

from data.provider_http import (
    RequestRateLimiter,
    is_transient_request_error,
    read_bounded_response_bytes,
    request_error_kind,
    retry_delay_seconds,
)


class _Response:
    def __init__(self, status=429, headers=None):
        self.status_code = status
        self.headers = dict(headers or {})

    def iter_content(self, *, chunk_size):
        del chunk_size
        yield from getattr(self, "chunks", [])


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


def test_bounded_response_reader_enforces_declared_and_streamed_size():
    response = _Response(headers={"Content-Length": "4"})
    response.chunks = [b"ab", b"", b"cd"]
    assert read_bounded_response_bytes(response, 4, chunk_size=2) == b"abcd"

    response.headers["Content-Length"] = "5"
    with pytest.raises(ValueError, match="declared byte limit"):
        read_bounded_response_bytes(response, 4)

    response.headers = {}
    response.chunks = [b"abc", b"de"]
    with pytest.raises(ValueError, match="byte limit"):
        read_bounded_response_bytes(response, 4)


def test_bounded_response_reader_rejects_invalid_content_length():
    response = _Response(headers={"Content-Length": "not-a-number"})
    with pytest.raises(ValueError, match="invalid Content-Length"):
        read_bounded_response_bytes(response, 4)
