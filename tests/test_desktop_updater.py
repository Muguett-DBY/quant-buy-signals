from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path
import stat
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from desktop import updater


pytestmark = pytest.mark.desktop


def _manifest_payload(**updates):
    payload = {
        "schema_version": 1,
        "product": "DS_DCF",
        "version": "11.1.1",
        "published_at": "2026-07-17T00:00:00+00:00",
        "package_url": "https://downloads.example.test/DS_DCF-v11.1.1.zip",
        "sha256": "a" * 64,
        "size": 123,
    }
    payload.update(updates)
    return payload


def _parsed_manifest(**updates):
    return updater.parse_update_manifest(
        _manifest_payload(**updates),
        now=datetime(2026, 7, 17, 1, tzinfo=timezone.utc),
    )


def _watermark_manifest(*, version="11.2.0", published_at="2026-07-16T00:00:00+00:00", **updates):
    return updater.parse_update_manifest(
        _manifest_payload(version=version, published_at=published_at, **updates),
        now=datetime(2026, 7, 17, 1, tzinfo=timezone.utc),
    )


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        content_length: int | None = None,
        status_code: int = 200,
        headers: dict | None = None,
    ):
        self.body = body
        self.url = url
        self.headers = dict(headers or {})
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.status_code = status_code
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[start : start + chunk_size]

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class _SequenceSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _hold_cross_process_watermark_lock(path, ready, release):
    with updater._cross_process_watermark_lock(Path(path), timeout_seconds=10.0):
        ready.set()
        if not release.wait(15.0):
            raise RuntimeError("test did not release the cross-process watermark lock")


def _der_integer(value: int) -> bytes:
    encoded = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if encoded[0] & 0x80:
        encoded = b"\0" + encoded
    return b"\x02" + bytes((len(encoded),)) + encoded


def _test_manifest_signature(payload, *, private_key=1, nonce=2):
    digest = hashlib.sha256(updater._canonical_manifest_bytes(payload)).digest()
    point = updater._p256_scalar_multiply(nonce, updater._P256_GENERATOR)
    assert point is not None
    r = point[0] % updater._P256_ORDER
    s = (pow(nonce, -1, updater._P256_ORDER) * (int.from_bytes(digest, "big") + r * private_key)) % (
        updater._P256_ORDER
    )
    body = _der_integer(r) + _der_integer(s)
    return b"\x30" + bytes((len(body),)) + body


def _write_release_zip(path, *, version="11.1.1", mutations=None):
    root = f"DS_DCF-v{version}"
    entries = {
        f"{root}/DS_DCF.exe": b"MZ-test-executable",
        f"{root}/release-manifest.json": json.dumps(
            {
                "schema_version": 1,
                "product": "DS_DCF",
                "version": version,
                "entrypoint": "DS_DCF.exe",
            },
            separators=(",", ":"),
        ).encode(),
        f"{root}/build-provenance.json": json.dumps(
            {
                "schema_version": 1,
                "product": "DS_DCF",
                "version": version,
                "git_sha": "b" * 40,
            },
            separators=(",", ":"),
        ).encode(),
        f"{root}/_internal/app.py": b"print('app')\n",
    }
    if mutations:
        entries.update(mutations)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            if isinstance(content, zipfile.ZipInfo):
                archive.writestr(content, b"target")
            else:
                archive.writestr(name, content)
    raw = path.read_bytes()
    return updater.UpdateManifest(
        version=version,
        published_at="2026-07-17T00:00:00+00:00",
        package_url=f"https://downloads.example.test/DS_DCF-v{version}.zip",
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )


def test_update_manifest_is_strict_https_dated_and_canonical():
    manifest = _parsed_manifest()
    assert manifest.version == "11.1.1"
    assert manifest.package_url.startswith("https://")

    with pytest.raises(updater.UpdateError, match="HTTPS"):
        _parsed_manifest(package_url="http://example.test/package.zip")
    with pytest.raises(updater.UpdateError, match="semantic"):
        _parsed_manifest(version="10.0")
    with pytest.raises(updater.UpdateError, match="future"):
        updater.parse_update_manifest(
            _manifest_payload(published_at="2027-01-01T00:00:00+00:00"),
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
        )
    with pytest.raises(updater.UpdateError, match="shape"):
        updater.parse_update_manifest(
            {**_manifest_payload(), "unexpected": True},
            now=datetime(2026, 7, 17, 1, tzinfo=timezone.utc),
        )


def test_update_manifest_watermark_rejects_replay_and_same_version_substitution(tmp_path):
    watermark_path = tmp_path / "state" / "update-manifest-watermark.json"
    accepted = _watermark_manifest(version="11.4.0")

    first = updater.verify_update_manifest_watermark(
        accepted,
        watermark_path,
        current_version="11.2.0",
        commit=True,
    )
    repeated = updater.verify_update_manifest_watermark(
        accepted,
        watermark_path,
        current_version="11.2.0",
        commit=True,
    )

    assert first == repeated
    assert watermark_path.is_file()
    with pytest.raises(updater.UpdateError, match="highest verified"):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(version="11.3.0"),
            watermark_path,
            current_version="11.2.0",
        )
    with pytest.raises(updater.UpdateError, match="different signed manifest"):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(version="11.4.0", sha256="b" * 64),
            watermark_path,
            current_version="11.2.0",
        )
    with pytest.raises(updater.UpdateError, match="different signed manifest"):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(version="11.4.0", published_at="2026-07-16T00:00:01+00:00"),
            watermark_path,
            current_version="11.2.0",
        )
    with pytest.raises(updater.UpdateError, match="publication time moved backwards"):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(version="11.5.0", published_at="2026-07-15T00:00:00+00:00"),
            watermark_path,
            current_version="11.2.0",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}",
        b'{"schema_version":1',
        b'{"schema_version":1,"schema_version":1}',
    ],
)
def test_update_manifest_watermark_fails_closed_on_empty_partial_or_malformed_state(tmp_path, raw):
    watermark_path = tmp_path / "update-manifest-watermark.json"
    watermark_path.write_bytes(raw)

    with pytest.raises(updater.UpdateError):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(),
            watermark_path,
            current_version="11.2.0",
        )


def test_update_manifest_watermark_fails_closed_on_non_file_state(tmp_path):
    watermark_path = tmp_path / "update-manifest-watermark.json"
    watermark_path.mkdir()

    with pytest.raises(updater.UpdateError, match="regular file"):
        updater.verify_update_manifest_watermark(
            _watermark_manifest(),
            watermark_path,
            current_version="11.2.0",
        )


def test_windows_named_mutex_timeout_closes_handle_without_releasing_unowned_mutex(monkeypatch):
    calls = []

    def create_mutex(_security, _owner, name):
        calls.append(("create", name))
        return 123

    def wait_for_single_object(handle, timeout_ms):
        calls.append(("wait", handle, timeout_ms))
        return updater._WINDOWS_WAIT_TIMEOUT

    def release_mutex(handle):
        calls.append(("release", handle))
        return True

    def close_handle(handle):
        calls.append(("close", handle))
        return True

    monkeypatch.setattr(
        updater,
        "_windows_mutex_api",
        lambda: (create_mutex, wait_for_single_object, release_mutex, close_handle, lambda: 0),
    )

    with pytest.raises(updater.UpdateError, match="timed out"):
        with updater._windows_named_mutex("Local\\test", timeout_seconds=0.025):
            pytest.fail("timed-out mutex must not enter the protected section")

    assert calls == [
        ("create", "Local\\test"),
        ("wait", 123, 25),
        ("close", 123),
    ]


@pytest.mark.parametrize("wait_result", [updater._WINDOWS_WAIT_OBJECT_0, updater._WINDOWS_WAIT_ABANDONED])
def test_windows_named_mutex_releases_and_closes_after_protected_exception(monkeypatch, wait_result):
    calls = []

    monkeypatch.setattr(
        updater,
        "_windows_mutex_api",
        lambda: (
            lambda _security, _owner, _name: 456,
            lambda handle, timeout_ms: calls.append(("wait", handle, timeout_ms)) or wait_result,
            lambda handle: calls.append(("release", handle)) or True,
            lambda handle: calls.append(("close", handle)) or True,
            lambda: 0,
        ),
    )

    with pytest.raises(RuntimeError, match="protected failure"):
        with updater._windows_named_mutex("Local\\test", timeout_seconds=0.1):
            raise RuntimeError("protected failure")

    assert calls == [("wait", 456, 100), ("release", 456), ("close", 456)]


def test_watermark_lock_serializes_processes_times_out_and_recovers_after_exception(tmp_path):
    watermark_path = (tmp_path / "state" / "update-manifest-watermark.json").resolve()
    watermark_path.parent.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_cross_process_watermark_lock,
        args=(str(watermark_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10.0), f"lock-holder process exited early with code {process.exitcode}"
        with pytest.raises(updater.UpdateError, match="timed out"):
            with updater._cross_process_watermark_lock(watermark_path, timeout_seconds=0.05):
                pytest.fail("a second process must not enter the protected section")
    finally:
        release.set()
        process.join(10.0)
        if process.is_alive():
            process.terminate()
            process.join(5.0)
    assert process.exitcode == 0

    with pytest.raises(RuntimeError, match="protected failure"):
        with updater._cross_process_watermark_lock(watermark_path, timeout_seconds=1.0):
            raise RuntimeError("protected failure")
    with updater._cross_process_watermark_lock(watermark_path, timeout_seconds=1.0):
        pass


def test_update_check_records_only_the_installed_version_baseline(tmp_path, monkeypatch):
    watermark_path = tmp_path / "state" / "update-manifest-watermark.json"
    future = _watermark_manifest(version="11.4.0")
    monkeypatch.setattr(updater, "fetch_update_manifest", lambda *args, **kwargs: future)

    result = updater.check_for_update(
        "https://example.test/update-manifest.json",
        current_version="11.2.0",
        watermark_path=watermark_path,
    )

    assert result.update_available is True
    assert not watermark_path.exists()

    current = _watermark_manifest(version="11.2.0")
    monkeypatch.setattr(updater, "fetch_update_manifest", lambda *args, **kwargs: current)
    result = updater.check_for_update(
        "https://example.test/update-manifest.json",
        current_version="11.2.0",
        watermark_path=watermark_path,
    )
    assert result.update_available is False
    assert watermark_path.is_file()

    changed_current = _watermark_manifest(version="11.2.0", sha256="c" * 64)
    monkeypatch.setattr(updater, "fetch_update_manifest", lambda *args, **kwargs: changed_current)
    with pytest.raises(updater.UpdateError, match="different signed manifest"):
        updater.check_for_update(
            "https://example.test/update-manifest.json",
            current_version="11.2.0",
            watermark_path=watermark_path,
        )


def test_local_package_verification_is_side_effect_free_and_reuses_update_gates(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)

    verified = updater.verify_update_package(package, manifest)

    assert verified == package.resolve()
    assert list(tmp_path.iterdir()) == [package]


@pytest.mark.parametrize(
    "provenance, message",
    [
        (b"{}", "invalid shape"),
        (
            b'{"schema_version":1,"product":"DS_DCF","version":"11.1.1","git_sha":"not-a-commit"}',
            "exact Git commit",
        ),
        (
            b'{"schema_version":1,"product":"DS_DCF","version":"99.0.0","git_sha":"' + b"b" * 40 + b'"}',
            "exact Git commit",
        ),
    ],
)
def test_update_package_requires_exact_git_build_provenance(tmp_path, provenance, message):
    package = tmp_path / "release.zip"
    root = "DS_DCF-v11.1.1"
    manifest = _write_release_zip(
        package,
        mutations={f"{root}/build-provenance.json": provenance},
    )

    with pytest.raises(updater.UpdateError, match=message):
        updater.verify_update_package(package, manifest)


def test_manifest_fetch_rejects_insecure_redirect_and_duplicate_json_key():
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"product":"DS_DCF","version":"11.1.1",'
        b'"published_at":"2026-07-17T00:00:00+00:00","package_url":"https://example.test/p.zip",'
        b'"sha256":"' + b"a" * 64 + b'","size":123}'
    )
    redirect = _Response(
        b"",
        url="https://example.test/manifest.json",
        status_code=302,
        headers={"Location": "http://redirect.example.test/manifest.json"},
    )
    insecure = _Session(redirect)
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.fetch_update_manifest("https://example.test/manifest.json", session=insecure)
    assert [call[0] for call in insecure.calls] == ["https://example.test/manifest.json"]
    assert insecure.calls[0][1]["allow_redirects"] is False
    assert redirect.closed

    duplicate_session = _Session(_Response(duplicate, url="https://example.test/manifest.json"))
    with pytest.raises(updater.UpdateError, match="duplicate key"):
        updater.fetch_update_manifest("https://example.test/manifest.json", session=duplicate_session)


@pytest.mark.parametrize(
    "target",
    [
        "http://public.example.test/manifest.json",
        "https://user@public.example.test/manifest.json",
        "https://public.example.test/manifest.json#fragment",
        "https://localhost/manifest.json",
        "https://LOCALHOST./manifest.json",
        "https://service.localhost/manifest.json",
        "https://127.0.0.1/manifest.json",
        "https://10.0.0.1/manifest.json",
        "https://169.254.1.1/manifest.json",
        "https://192.0.2.1/manifest.json",
        "https://[::1]/manifest.json",
        "https://[fe80::1]/manifest.json",
        "https://127.1/manifest.json",
        "https://2130706433/manifest.json",
        "https://0x7f000001/manifest.json",
        "https://0177.0.0.1/manifest.json",
    ],
)
def test_https_redirect_rejects_unsafe_target_before_network_request(target):
    source = "https://public.example.test/manifest.json"
    redirect = _Response(b"", url=source, status_code=302, headers={"Location": target})
    session = _Session(redirect)

    with pytest.raises(updater.UpdateError):
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=session,
            timeout=(1, 1),
        )

    assert [call[0] for call in session.calls] == [source]
    assert redirect.closed


@pytest.mark.parametrize("location", [None, "", "   ", 123])
def test_https_redirect_requires_one_nonempty_location_header(location):
    source = "https://public.example.test/manifest.json"
    headers = {} if location is None else {"Location": location}
    redirect = _Response(b"", url=source, status_code=302, headers=headers)

    with pytest.raises(updater.UpdateError, match="Location"):
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=_Session(redirect),
            timeout=(1, 1),
        )
    assert redirect.closed


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
def test_https_fetch_follows_each_supported_redirect_status(status_code):
    source = "https://public.example.test/manifest.json"
    target = "https://cdn.example.test/manifest.json"
    redirect = _Response(b"", url=source, status_code=status_code, headers={"Location": target})
    final = _Response(b"verified", url=target)
    session = _SequenceSession(redirect, final)

    assert (
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=session,
            timeout=(1, 1),
        )
        == b"verified"
    )
    assert [call[0] for call in session.calls] == [source, target]
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert redirect.closed and final.closed


@pytest.mark.parametrize("status_code", [300, 304, 305, 306])
def test_https_fetch_rejects_unsupported_redirect_status(status_code):
    source = "https://public.example.test/manifest.json"
    response = _Response(
        b"",
        url=source,
        status_code=status_code,
        headers={"Location": "https://cdn.example.test/manifest.json"},
    )

    with pytest.raises(updater.UpdateError, match="unsupported redirect"):
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=_Session(response),
            timeout=(1, 1),
        )
    assert response.closed


def test_https_redirect_rejects_loop_and_limit_without_requesting_the_next_target(monkeypatch):
    source = "https://public.example.test/manifest.json"
    second = "https://cdn.example.test/manifest.json"
    first_loop_response = _Response(b"", url=source, status_code=301, headers={"Location": second})
    second_loop_response = _Response(b"", url=second, status_code=307, headers={"Location": source})
    loop_session = _SequenceSession(first_loop_response, second_loop_response)
    with pytest.raises(updater.UpdateError, match="loop"):
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=loop_session,
            timeout=(1, 1),
        )
    assert [call[0] for call in loop_session.calls] == [source, second]
    assert first_loop_response.closed and second_loop_response.closed

    monkeypatch.setattr(updater, "UPDATE_MAX_REDIRECTS", 2)
    first = _Response(b"", url=source, status_code=302, headers={"Location": "/one"})
    second_response = _Response(
        b"",
        url="https://public.example.test/one",
        status_code=302,
        headers={"Location": "/two"},
    )
    third = _Response(
        b"",
        url="https://public.example.test/two",
        status_code=302,
        headers={"Location": "/never-requested"},
    )
    limited_session = _SequenceSession(first, second_response, third)
    with pytest.raises(updater.UpdateError, match="redirect limit"):
        updater._fetch_https_bytes(
            source,
            label="update manifest",
            limit=1024,
            session=limited_session,
            timeout=(1, 1),
        )
    assert [call[0] for call in limited_session.calls] == [
        source,
        "https://public.example.test/one",
        "https://public.example.test/two",
    ]
    assert first.closed and second_response.closed and third.closed


def test_https_redirect_allows_exactly_the_configured_limit(monkeypatch):
    monkeypatch.setattr(updater, "UPDATE_MAX_REDIRECTS", 2)
    source = "https://public.example.test/manifest.json"
    first = _Response(b"", url=source, status_code=302, headers={"Location": "/one"})
    second = _Response(
        b"",
        url="https://public.example.test/one",
        status_code=307,
        headers={"Location": "/two"},
    )
    final = _Response(b"verified", url="https://public.example.test/two")
    session = _SequenceSession(first, second, final)

    body = updater._fetch_https_bytes(
        source,
        label="update manifest",
        limit=1024,
        session=session,
        timeout=(1, 1),
    )

    assert body == b"verified"
    assert len(session.calls) == 3
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert first.closed and second.closed and final.closed


def test_manifest_signature_and_package_use_the_same_manual_redirect_policy(tmp_path, monkeypatch):
    payload = _manifest_payload(
        package_url="https://downloads.example.test/package.zip",
        sha256=hashlib.sha256(b"package").hexdigest(),
        size=len(b"package"),
    )
    raw = json.dumps(payload).encode()
    signature = _test_manifest_signature(payload)
    monkeypatch.setattr(updater, "UPDATE_SIGNING_PUBLIC_KEY", updater._P256_GENERATOR)
    manifest_session = _SequenceSession(
        _Response(
            b"",
            url="https://example.test/update-manifest.json",
            status_code=302,
            headers={"Location": "/release/update-manifest.json"},
        ),
        _Response(raw, url="https://example.test/release/update-manifest.json"),
        _Response(
            b"",
            url="https://example.test/update-manifest.json.sig",
            status_code=307,
            headers={"Location": "https://cdn.example.test/update-manifest.sig"},
        ),
        _Response(signature, url="https://cdn.example.test/update-manifest.sig"),
    )
    manifest = updater.fetch_update_manifest(
        "https://example.test/update-manifest.json",
        session=manifest_session,
    )
    assert all(call[1]["allow_redirects"] is False for call in manifest_session.calls)

    package_redirect = _Response(
        b"",
        url=manifest.package_url,
        status_code=308,
        headers={"Location": "https://cdn.example.test/package.zip"},
    )
    package_response = _Response(
        b"package",
        url="https://cdn.example.test/package.zip",
        content_length=len(b"package"),
    )
    package_session = _SequenceSession(package_redirect, package_response)
    downloaded = updater.download_update_package(
        manifest,
        tmp_path / "package.zip",
        session=package_session,
    )
    assert downloaded.read_bytes() == b"package"
    assert all(call[1]["allow_redirects"] is False for call in package_session.calls)
    assert package_redirect.closed and package_response.closed


def test_manifest_fetch_requires_a_valid_detached_signature_over_canonical_json(monkeypatch):
    payload = _manifest_payload()
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    signature = _test_manifest_signature(payload)
    monkeypatch.setattr(updater, "UPDATE_SIGNING_PUBLIC_KEY", updater._P256_GENERATOR)
    session = _SequenceSession(
        _Response(raw, url="https://example.test/update-manifest.json"),
        _Response(signature, url="https://example.test/update-manifest.json.sig"),
    )

    manifest = updater.fetch_update_manifest(
        "https://example.test/update-manifest.json",
        session=session,
    )

    assert manifest.version == "11.1.1"
    assert [call[0] for call in session.calls] == [
        "https://example.test/update-manifest.json",
        "https://example.test/update-manifest.json.sig",
    ]

    tampered = dict(payload, package_url="https://attacker.example.test/replaced.zip")
    tampered_session = _SequenceSession(
        _Response(json.dumps(tampered).encode(), url="https://example.test/update-manifest.json"),
        _Response(signature, url="https://example.test/update-manifest.json.sig"),
    )
    with pytest.raises(updater.UpdateError, match="does not match"):
        updater.fetch_update_manifest(
            "https://example.test/update-manifest.json",
            session=tampered_session,
        )


@pytest.mark.parametrize(
    "signature",
    [
        b"",
        b"not-der",
        b"\x30\x06\x02\x01\x00\x02\x01\x01",
        b"\x30\x07\x02\x02\x00\x01\x02\x01\x01",
    ],
)
def test_manifest_fetch_fails_closed_for_missing_or_noncanonical_signature(monkeypatch, signature):
    payload = _manifest_payload()
    monkeypatch.setattr(updater, "UPDATE_SIGNING_PUBLIC_KEY", updater._P256_GENERATOR)
    session = _SequenceSession(
        _Response(json.dumps(payload).encode(), url="https://example.test/update-manifest.json"),
        _Response(signature, url="https://example.test/update-manifest.json.sig"),
    )

    with pytest.raises(updater.UpdateError, match="signature"):
        updater.fetch_update_manifest("https://example.test/update-manifest.json", session=session)


def test_embedded_update_public_key_is_a_valid_p256_point():
    x, y = updater.UPDATE_SIGNING_PUBLIC_KEY
    assert (y * y - (x**3 + updater._P256_A * x + updater._P256_B)) % updater._P256_FIELD == 0


def test_download_requires_exact_manifest_size_and_sha256(tmp_path):
    body = b"verified update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    response = _Response(body, url=manifest.package_url, content_length=len(body))
    path = updater.download_update_package(manifest, tmp_path / "package.zip", session=_Session(response))
    assert path.read_bytes() == body
    assert response.closed

    bad = updater.UpdateManifest(**{**manifest.__dict__, "sha256": "0" * 64})
    with pytest.raises(updater.UpdateError, match="SHA-256 does not match the update manifest"):
        updater.download_update_package(
            bad,
            tmp_path / "bad.zip",
            session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        )
    assert list(tmp_path.glob(".bad.zip.*.part")) == []


@pytest.mark.parametrize(
    "package_url",
    [
        "http://downloads.example.test/package.zip",
        "https://localhost/package.zip",
        "https://127.0.0.1/package.zip",
        "https://[::1]/package.zip",
    ],
)
def test_download_rejects_unsafe_direct_manifest_package_url_before_network(tmp_path, package_url):
    body = b"package"
    manifest = updater.UpdateManifest(
        version="11.2.0",
        published_at="2026-07-17T00:00:00+00:00",
        package_url=package_url,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    session = _Session(_Response(body, url=package_url, content_length=len(body)))

    with pytest.raises(updater.UpdateError):
        updater.download_update_package(manifest, tmp_path / "package.zip", session=session)

    assert session.calls == []
    assert not (tmp_path / "package.zip").exists()


def test_download_advances_watermark_only_after_complete_hash_verification(tmp_path):
    body = b"fully verified signed update package"
    watermark_path = tmp_path / "state" / "update-manifest-watermark.json"
    manifest = _watermark_manifest(
        version="11.3.0",
        package_url="https://downloads.example.test/DS_DCF-v11.3.0.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )

    downloaded = updater.download_update_package(
        manifest,
        tmp_path / "package.zip",
        session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        watermark_path=watermark_path,
        current_version="11.2.0",
    )

    assert downloaded.read_bytes() == body
    persisted = watermark_path.read_bytes()
    updater.verify_update_manifest_watermark(
        manifest,
        watermark_path,
        current_version="11.2.0",
    )

    bad = _watermark_manifest(
        version="11.4.0",
        published_at="2026-07-16T00:00:01+00:00",
        package_url="https://downloads.example.test/DS_DCF-v11.4.0.zip",
        sha256="0" * 64,
        size=len(body),
    )
    with pytest.raises(updater.UpdateError, match="SHA-256 does not match"):
        updater.download_update_package(
            bad,
            tmp_path / "bad-package.zip",
            session=_Session(_Response(body, url=bad.package_url, content_length=len(body))),
            watermark_path=watermark_path,
            current_version="11.2.0",
        )
    assert watermark_path.read_bytes() == persisted


def test_download_rejects_watermark_path_that_aliases_package_destination(tmp_path):
    body = b"verified signed update package"
    destination = tmp_path / "package.zip"
    manifest = _watermark_manifest(
        version="11.3.0",
        package_url="https://downloads.example.test/DS_DCF-v11.3.0.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    session = _Session(_Response(body, url=manifest.package_url, content_length=len(body)))

    with pytest.raises(updater.UpdateError, match="must be different files"):
        updater.download_update_package(
            manifest,
            destination,
            session=session,
            watermark_path=destination,
            current_version="11.2.0",
        )

    assert session.calls == []
    assert not destination.exists()


def test_download_uses_one_resolved_watermark_path_for_preflight_and_commit(tmp_path, monkeypatch):
    body = b"verified signed update package"
    manifest = _watermark_manifest(
        version="11.3.0",
        package_url="https://downloads.example.test/DS_DCF-v11.3.0.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    original_verify = updater.verify_update_manifest_watermark
    observed_paths = []

    def record_path(*args, **kwargs):
        observed_paths.append(Path(args[1]))
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(updater, "verify_update_manifest_watermark", record_path)
    monkeypatch.chdir(tmp_path)
    updater.download_update_package(
        manifest,
        tmp_path / "package.zip",
        session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        watermark_path="state/update-manifest-watermark.json",
        current_version="11.2.0",
    )

    expected_path = (tmp_path / "state" / "update-manifest-watermark.json").resolve()
    assert observed_paths == [expected_path, expected_path]


def test_download_never_overwrites_or_deletes_a_preexisting_partial_file(tmp_path):
    body = b"verified update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )
    partial = tmp_path / "package.zip.part"
    partial.write_bytes(b"belongs to another download")

    downloaded = updater.download_update_package(
        manifest,
        tmp_path / "package.zip",
        session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
    )

    assert partial.read_bytes() == b"belongs to another download"
    assert downloaded.read_bytes() == body


def test_temporary_cleanup_failure_does_not_mask_the_validation_error(tmp_path, monkeypatch):
    body = b"corrupt update bytes"
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/package.zip",
        sha256="0" * 64,
        size=len(body),
    )

    def fail_cleanup(self, *, missing_ok=False):
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(updater.UpdateError, match="SHA-256 does not match"):
        updater.download_update_package(
            manifest,
            tmp_path / "package.zip",
            session=_Session(_Response(body, url=manifest.package_url, content_length=len(body))),
        )


def test_valid_update_installs_side_by_side_without_overwriting_current_version(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)

    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
        current_version="11.1.0",
    )

    assert installed.version == "11.1.1"
    assert installed.executable.read_bytes() == b"MZ-test-executable"
    assert installed.version_dir == (tmp_path / "versions" / "11.1.1").resolve()
    assert installed.install_dir == (tmp_path / "versions" / "11.1.1" / "app").resolve()
    assert installed.package.name == "DS_DCF-v11.1.1-windows-x64-portable.zip"
    assert installed.package.read_bytes() == package.read_bytes()
    assert package.is_file()

    repeated = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
        current_version="11.1.0",
    )
    assert repeated == installed


@pytest.mark.parametrize("tamper", ["executable", "extra_file", "portable_package"])
def test_existing_version_is_replayed_byte_for_byte_before_idempotent_success(tmp_path, tamper):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)
    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
        current_version="11.1.0",
    )
    if tamper == "executable":
        installed.executable.write_bytes(b"MZ-tampered")
    elif tamper == "extra_file":
        (installed.install_dir / "unexpected.dll").write_bytes(b"extra")
    else:
        installed.package.write_bytes(b"tampered package")

    with pytest.raises(updater.UpdateError, match="differs|file set"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )


def test_existing_version_rejects_an_application_root_symlink(tmp_path):
    package = tmp_path / "release.zip"
    manifest = _write_release_zip(package)
    installed = updater.install_update_package(
        package,
        manifest,
        versions_root=tmp_path / "versions",
        create_shortcut=False,
        current_version="11.1.0",
    )
    outside = tmp_path / "outside-app"
    installed.install_dir.rename(outside)
    try:
        installed.install_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        outside.rename(installed.install_dir)
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(updater.UpdateError, match="symbolic link or junction"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"../escape.exe": b"bad"}, "one versioned product root"),
        ({"DS_DCF-v11.1.1/ds_dcf.EXE": b"collision"}, "case-colliding"),
        ({"DS_DCF-v11.1.1/CON.txt": b"reserved"}, "reserved Windows"),
        ({"DS_DCF-v11.1.1/invalid?.dll": b"bad"}, "unsafe path component"),
    ],
)
def test_update_zip_rejects_traversal_case_collisions_and_reserved_names(tmp_path, mutation, message):
    package = tmp_path / "malicious.zip"
    manifest = _write_release_zip(package, mutations=mutation)

    with pytest.raises(updater.UpdateError, match=message):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )
    assert not (tmp_path / "escape.exe").exists()


def test_update_zip_rejects_symbolic_links(tmp_path):
    package = tmp_path / "symlink.zip"
    root = "DS_DCF-v11.1.1"
    link = zipfile.ZipInfo(f"{root}/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    manifest = _write_release_zip(package, mutations={link.filename: link})

    with pytest.raises(updater.UpdateError, match="symbolic link"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )


@pytest.mark.parametrize(
    "name, mode, message",
    [
        ("DS_DCF-v11.1.1/mode-says-directory", stat.S_IFDIR | 0o755, "type disagrees"),
        ("DS_DCF-v11.1.1/mode-says-file/", stat.S_IFREG | 0o644, "type disagrees"),
        ("DS_DCF-v11.1.1", stat.S_IFREG | 0o644, "root must be a directory"),
    ],
)
def test_update_zip_rejects_external_mode_and_path_type_mismatches(tmp_path, name, mode, message):
    package = tmp_path / "mode-mismatch.zip"
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = mode << 16
    manifest = _write_release_zip(package, mutations={entry.filename: entry})

    with pytest.raises(updater.UpdateError, match=message):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )


def test_updater_refuses_same_version_and_downgrade_before_install(tmp_path):
    package = tmp_path / "old.zip"
    manifest = SimpleNamespace(version="10.0.0")
    with pytest.raises(updater.UpdateError, match="downgrade"):
        updater.install_update_package(package, manifest, create_shortcut=False)


def test_corrupt_zip_and_target_path_conflict_are_normalized_to_update_error(tmp_path):
    package = tmp_path / "corrupt.zip"
    package.write_bytes(b"not-a-zip")
    raw = package.read_bytes()
    manifest = updater.UpdateManifest(
        version="11.1.1",
        published_at="2026-07-17T00:00:00+00:00",
        package_url="https://downloads.example.test/corrupt.zip",
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
    )
    with pytest.raises(updater.UpdateError, match="valid ZIP"):
        updater.install_update_package(
            package,
            manifest,
            versions_root=tmp_path / "versions",
            create_shortcut=False,
            current_version="11.1.0",
        )

    valid = tmp_path / "valid.zip"
    valid_manifest = _write_release_zip(valid)
    root = tmp_path / "blocked"
    root.mkdir()
    (root / "11.1.1").write_bytes(b"path conflict")
    with pytest.raises(updater.UpdateError, match="directory is missing"):
        updater.install_update_package(
            valid,
            valid_manifest,
            versions_root=root,
            create_shortcut=False,
            current_version="11.1.0",
        )


def test_update_config_is_optional_but_never_accepts_plain_http(tmp_path, monkeypatch):
    monkeypatch.delenv("DS_DCF_UPDATE_MANIFEST_URL", raising=False)
    library = tmp_path / "library"
    assert updater.load_update_manifest_url(tmp_path, library_root=library) is None
    (tmp_path / "update_config.json").write_text('{"manifest_url":null}', encoding="utf-8")
    assert updater.load_update_manifest_url(tmp_path, library_root=library) is None
    (tmp_path / "update_config.json").write_text(
        '{"manifest_url":"http://example.test/manifest.json"}',
        encoding="utf-8",
    )
    with pytest.raises(updater.UpdateError, match="HTTPS"):
        updater.load_update_manifest_url(tmp_path, library_root=library)


def test_stable_library_update_config_overrides_bundled_config_and_environment_wins(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    library = tmp_path / "library"
    app_root.mkdir()
    library.mkdir()
    (app_root / "update_config.json").write_text(
        '{"manifest_url":"https://example.test/bundled.json"}', encoding="utf-8"
    )
    (library / "update_config.json").write_text('{"manifest_url":"https://example.test/stable.json"}', encoding="utf-8")
    assert updater.load_update_manifest_url(app_root, library_root=library).endswith("stable.json")

    monkeypatch.setenv("DS_DCF_UPDATE_MANIFEST_URL", "https://example.test/environment.json")
    assert updater.load_update_manifest_url(app_root, library_root=library).endswith("environment.json")


def test_shortcut_creation_uses_an_absolute_system_powershell_path(tmp_path, monkeypatch):
    target = tmp_path / "app" / "DS_DCF.exe"
    target.parent.mkdir()
    target.write_bytes(b"MZ")
    powershell = tmp_path / "Windows" / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"MZ")
    shortcut = tmp_path / "6BUYING_POINT" / "DS_DCF.lnk"
    shortcut.parent.mkdir()
    shortcut.write_bytes(b"lnk")
    calls = []

    monkeypatch.setattr(updater, "_windows_powershell_executable", lambda: powershell.resolve())

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=str(shortcut) + "\n")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    assert updater.create_desktop_shortcut(target) == shortcut.resolve()
    assert Path(calls[0][0][0]).is_absolute()
    assert Path(calls[0][0][0]) == powershell.resolve()
    assert calls[0][1]["env"]["DS_DCF_SHORTCUT_TARGET"] == str(target.resolve())
