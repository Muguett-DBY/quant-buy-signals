"""Fail-closed HTTPS updater for side-by-side Windows desktop releases."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import errno
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from desktop.version import __version__


PRODUCT_ID = "DS_DCF"
DESKTOP_LIBRARY_NAME = "6BUYING_POINT"
UPDATE_MANIFEST_SCHEMA_VERSION = 1
RELEASE_MANIFEST_SCHEMA_VERSION = 1
BUILD_PROVENANCE_SCHEMA_VERSION = 1
MANIFEST_MAX_BYTES = 64 * 1024
MANIFEST_SIGNATURE_MAX_BYTES = 512
UPDATE_WATERMARK_MAX_BYTES = 4 * 1024
UPDATE_WATERMARK_MUTEX_TIMEOUT_SECONDS = 30.0
PACKAGE_MAX_BYTES = 2 * 1024 * 1024 * 1024
ZIP_MAX_ENTRIES = 20_000
ZIP_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 1_000.0
UPDATE_TIMEOUT = (10, 60)
UPDATE_MAX_REDIRECTS = 5

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_AMBIGUOUS_NUMERIC_HOST = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)(?:\.(?:0[xX][0-9a-fA-F]+|[0-9]+)){0,3}$")
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
# This is the P-256 public key that signs Windows update manifests.  Only the
# corresponding private key, held outside the repository, can authorize a new
# package hash.  The updater intentionally has no configurable key override.
UPDATE_SIGNING_PUBLIC_KEY_SPKI_BASE64 = (
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbzdnA3j6aObU/Z0HlTTC+PziXVm4h"
    "Z/pqSQrUWeC2mm/INuge2qyT67vWxpTC7yPDzFdHOBenDnQ8lMEilPKDw=="
)
UPDATE_SIGNING_PUBLIC_KEY = (
    0x6F37670378FA68E6D4FD9D079534C2F8FCE25D59B8859FE9A9242B516782DA69,
    0xBF20DBA07B6AB24FAEEF5B1A530BBC8F0F315D1CE05E9C39D0F253048A53CA0F,
)
_P256_FIELD = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_FIELD - 3
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GENERATOR = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_UPDATE_WATERMARK_LOCK = threading.Lock()
_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_WAIT_ABANDONED = 0x00000080
_WINDOWS_WAIT_TIMEOUT = 0x00000102
_WINDOWS_WAIT_FAILED = 0xFFFFFFFF


class UpdateError(RuntimeError):
    """An update manifest, package, or install operation failed validation."""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    published_at: str
    package_url: str
    sha256: str
    size: int
    manifest_sha256: str = ""


@dataclass(frozen=True)
class UpdateManifestWatermark:
    version: str
    published_at: str
    manifest_sha256: str


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    manifest: UpdateManifest
    update_available: bool


@dataclass(frozen=True)
class InstalledUpdate:
    version: str
    version_dir: Path
    install_dir: Path
    executable: Path
    package: Path
    shortcut: Path | None


def default_version_library_root() -> Path:
    """Return the user-visible, side-by-side version library."""

    override = str(os.environ.get("DS_DCF_VERSION_LIBRARY_ROOT") or "").strip()
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer)
            if result != 0 or not buffer.value:
                raise OSError(f"SHGetFolderPathW failed: {result}")
            root = Path(buffer.value) / DESKTOP_LIBRARY_NAME
        except (AttributeError, OSError):
            root = Path.home() / "Desktop" / DESKTOP_LIBRARY_NAME
    else:
        root = Path.home() / "Desktop" / DESKTOP_LIBRARY_NAME
    return root.resolve()


def _sha256_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise UpdateError(f"file integrity check failed: {type(exc).__name__}") from exc
    return size, digest.hexdigest()


def _semver_tuple(value: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(str(value).strip())
    if match is None:
        raise UpdateError("version must be a canonical three-part semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _validate_https_url(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"{field} must be a non-empty HTTPS URL")
    text = value.strip()
    if (
        text != value
        or "#" in text
        or "\\" in text
        or any(ord(character) < 33 or ord(character) == 127 for character in text)
    ):
        raise UpdateError(f"{field} contains unsafe URL characters")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UpdateError(f"{field} is not a valid HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "\\" in parsed.netloc
    ):
        raise UpdateError(f"{field} must be an HTTPS URL without credentials or fragments")
    _ = port
    if any(ord(character) < 33 or ord(character) > 126 for character in hostname) or "%" in hostname:
        raise UpdateError(f"{field} contains an unsafe hostname")
    normalized_host = hostname.rstrip(".").casefold()
    if not normalized_host or normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise UpdateError(f"{field} must not target localhost")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        if _AMBIGUOUS_NUMERIC_HOST.fullmatch(normalized_host):
            raise UpdateError(f"{field} contains an ambiguous numeric IP address") from None
    else:
        if not address.is_global or address.is_multicast:
            raise UpdateError(f"{field} must not target a private, reserved, or local IP address")
    return text


def _redirect_identity(url: str, *, field: str) -> str:
    parsed = urlsplit(_validate_https_url(url, field=field))
    hostname = str(parsed.hostname).rstrip(".").casefold()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == name.casefold():
            return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def _open_https_response(
    url: str,
    *,
    label: str,
    session: Any,
    timeout: tuple[int, int],
) -> tuple[Any, str]:
    """Open one HTTPS response while validating every redirect before use."""

    current_url = _validate_https_url(url, field=f"{label} URL")
    seen = {_redirect_identity(current_url, field=f"{label} URL")}
    redirect_count = 0
    while True:
        try:
            response = session.get(current_url, timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException as exc:
            raise UpdateError(f"{label} request failed: {type(exc).__name__}") from exc
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            response.close()
            raise UpdateError(f"{label} response has an invalid HTTP status")
        if status_code not in _REDIRECT_STATUS_CODES:
            if 300 <= status_code < 400:
                response.close()
                raise UpdateError(f"{label} returned an unsupported redirect status: {status_code}")
            return response, current_url
        try:
            location = _response_header(response, "Location")
            if location is None:
                raise UpdateError(f"{label} redirect is missing a valid Location header")
            target_url = _validate_https_url(urljoin(current_url, location), field=f"{label} redirect URL")
            identity = _redirect_identity(target_url, field=f"{label} redirect URL")
            if identity in seen:
                raise UpdateError(f"{label} redirect loop was detected")
            if redirect_count >= UPDATE_MAX_REDIRECTS:
                raise UpdateError(f"{label} exceeded the redirect limit")
        finally:
            response.close()
        seen.add(identity)
        redirect_count += 1
        current_url = target_url


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError(f"JSON contains a duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise UpdateError(f"JSON contains a non-finite number: {value}")


def _decode_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{label} is not UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"{label} is not valid JSON") from exc


def _canonical_manifest_bytes(payload: object) -> bytes:
    """Return the single JSON representation covered by the release signature."""

    if not isinstance(payload, Mapping):
        raise UpdateError("update manifest is not a JSON object")
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UpdateError("update manifest cannot be canonicalized") from exc


def _read_der_length(raw: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(raw):
        raise UpdateError("update manifest signature is not valid DER")
    first = raw[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    length_octets = first & 0x7F
    if length_octets == 0 or length_octets > 2 or offset + length_octets > len(raw):
        raise UpdateError("update manifest signature is not valid DER")
    encoded = raw[offset : offset + length_octets]
    if encoded[0] == 0:
        raise UpdateError("update manifest signature is not canonical DER")
    length = int.from_bytes(encoded, "big")
    if length < 0x80:
        raise UpdateError("update manifest signature is not canonical DER")
    return length, offset + length_octets


def _read_der_integer(raw: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(raw) or raw[offset] != 0x02:
        raise UpdateError("update manifest signature is not valid DER")
    length, offset = _read_der_length(raw, offset + 1)
    end = offset + length
    if length == 0 or end > len(raw):
        raise UpdateError("update manifest signature is not valid DER")
    encoded = raw[offset:end]
    if encoded[0] & 0x80:
        raise UpdateError("update manifest signature contains a negative integer")
    if len(encoded) > 1 and encoded[0] == 0 and not encoded[1] & 0x80:
        raise UpdateError("update manifest signature is not canonical DER")
    return int.from_bytes(encoded, "big"), end


def _decode_ecdsa_signature(signature: bytes) -> tuple[int, int]:
    if not isinstance(signature, bytes) or not 8 <= len(signature) <= MANIFEST_SIGNATURE_MAX_BYTES:
        raise UpdateError("update manifest signature size is invalid")
    if signature[0] != 0x30:
        raise UpdateError("update manifest signature is not valid DER")
    sequence_length, offset = _read_der_length(signature, 1)
    if offset + sequence_length != len(signature):
        raise UpdateError("update manifest signature is not valid DER")
    r, offset = _read_der_integer(signature, offset)
    s, offset = _read_der_integer(signature, offset)
    if offset != len(signature):
        raise UpdateError("update manifest signature is not valid DER")
    if not (1 <= r < _P256_ORDER and 1 <= s < _P256_ORDER):
        raise UpdateError("update manifest signature scalar is outside the P-256 range")
    return r, s


def _p256_point_add(
    left: tuple[int, int] | None,
    right: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2:
        if (y1 + y2) % _P256_FIELD == 0:
            return None
        slope = ((3 * x1 * x1 + _P256_A) * pow(2 * y1, -1, _P256_FIELD)) % _P256_FIELD
    else:
        slope = ((y2 - y1) * pow((x2 - x1) % _P256_FIELD, -1, _P256_FIELD)) % _P256_FIELD
    x3 = (slope * slope - x1 - x2) % _P256_FIELD
    return x3, (slope * (x1 - x3) - y1) % _P256_FIELD


def _p256_scalar_multiply(scalar: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    value = scalar
    while value:
        if value & 1:
            result = _p256_point_add(result, addend)
        addend = _p256_point_add(addend, addend)
        value >>= 1
    return result


def _verify_manifest_signature(payload: object, signature: bytes) -> None:
    """Verify one detached ECDSA-P256/SHA-256 signature with the pinned key."""

    public_x, public_y = UPDATE_SIGNING_PUBLIC_KEY
    if (
        not 0 <= public_x < _P256_FIELD
        or not 0 <= public_y < _P256_FIELD
        or (public_y * public_y - (public_x**3 + _P256_A * public_x + _P256_B)) % _P256_FIELD != 0
    ):
        raise UpdateError("embedded update signing public key is invalid")
    r, s = _decode_ecdsa_signature(signature)
    digest = hashlib.sha256(_canonical_manifest_bytes(payload)).digest()
    inverse = pow(s, -1, _P256_ORDER)
    left = _p256_scalar_multiply((int.from_bytes(digest, "big") * inverse) % _P256_ORDER, _P256_GENERATOR)
    right = _p256_scalar_multiply((r * inverse) % _P256_ORDER, UPDATE_SIGNING_PUBLIC_KEY)
    point = _p256_point_add(left, right)
    if point is None or point[0] % _P256_ORDER != r:
        raise UpdateError("update manifest signature does not match the pinned release key")


def _manifest_signature_url(manifest_url: str) -> str:
    parsed = urlsplit(_validate_https_url(manifest_url, field="manifest_url"))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path + ".sig", parsed.query, ""))


def _bounded_response_bytes(response: Any, *, limit: int) -> bytes:
    declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if declared not in (None, ""):
        if not str(declared).isdigit() or int(declared) > limit:
            raise UpdateError("HTTP response exceeds the declared byte limit")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise UpdateError("HTTP response yielded non-byte content")
        total += len(chunk)
        if total > limit:
            raise UpdateError("HTTP response exceeds the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_update_manifest(payload: object, *, now: datetime | None = None) -> UpdateManifest:
    expected = {
        "schema_version",
        "product",
        "version",
        "published_at",
        "package_url",
        "sha256",
        "size",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("update manifest has an invalid shape")
    if payload.get("schema_version") != UPDATE_MANIFEST_SCHEMA_VERSION or payload.get("product") != PRODUCT_ID:
        raise UpdateError("update manifest product or schema does not match this application")
    version = str(payload.get("version") or "").strip()
    _semver_tuple(version)
    published_at = payload.get("published_at")
    if not isinstance(published_at, str):
        raise UpdateError("update manifest published_at must be an ISO timestamp")
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateError("update manifest published_at is invalid") from exc
    if published.tzinfo is None:
        raise UpdateError("update manifest published_at must include a timezone")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise ValueError("now must include a timezone")
    if published > reference.astimezone(timezone.utc).replace(microsecond=0) + timedelta(minutes=5):
        raise UpdateError("update manifest publication time is in the future")
    package_url = _validate_https_url(payload.get("package_url"), field="package_url")
    sha256 = str(payload.get("sha256") or "").strip()
    if _SHA256.fullmatch(sha256) is None:
        raise UpdateError("update manifest sha256 must be lowercase hexadecimal")
    size = payload.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= PACKAGE_MAX_BYTES:
        raise UpdateError("update manifest size is outside the allowed range")
    manifest_sha256 = hashlib.sha256(_canonical_manifest_bytes(payload)).hexdigest()
    return UpdateManifest(version, published_at, package_url, sha256, size, manifest_sha256)


def _published_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise UpdateError(f"{label} publication time is invalid") from exc
    if parsed.tzinfo is None:
        raise UpdateError(f"{label} publication time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _watermark_from_manifest(manifest: UpdateManifest) -> UpdateManifestWatermark:
    _semver_tuple(manifest.version)
    _published_timestamp(manifest.published_at, label="update manifest")
    if _SHA256.fullmatch(manifest.manifest_sha256) is None:
        raise UpdateError("update manifest has no verified canonical identity")
    return UpdateManifestWatermark(
        version=manifest.version,
        published_at=manifest.published_at,
        manifest_sha256=manifest.manifest_sha256,
    )


def _update_watermark_path(value: str | os.PathLike[str], *, create_parent: bool) -> Path:
    requested = Path(value).expanduser()
    if not requested.name or requested.name in {".", ".."}:
        raise UpdateError("update watermark path is invalid")
    requested_parent = requested.parent
    parent_is_junction = getattr(requested_parent, "is_junction", lambda: False)
    if requested_parent.is_symlink() or parent_is_junction():
        raise UpdateError("update watermark parent is a symbolic link or junction")
    try:
        if create_parent:
            requested_parent.mkdir(parents=True, exist_ok=True)
        parent = requested_parent.resolve(strict=True)
    except OSError as exc:
        raise UpdateError(f"update watermark directory is unavailable: {type(exc).__name__}") from exc
    path = parent / requested.name
    path_is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or path_is_junction():
        raise UpdateError("update watermark is a symbolic link or junction")
    if path.exists() and not path.is_file():
        raise UpdateError("update watermark is not a regular file")
    return path


def _watermark_mutex_name(path: Path) -> str:
    identity = os.path.normcase(str(path)).encode("utf-8", errors="surrogatepass")
    return f"Local\\DS_DCF_UpdateWatermark_{hashlib.sha256(identity).hexdigest()}"


def _windows_mutex_api() -> tuple[Any, Any, Any, Any, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    release_mutex = kernel32.ReleaseMutex
    release_mutex.argtypes = (wintypes.HANDLE,)
    release_mutex.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    return create_mutex, wait_for_single_object, release_mutex, close_handle, ctypes.get_last_error


def _mutex_timeout_milliseconds(timeout_seconds: float) -> int:
    if isinstance(timeout_seconds, bool):
        raise UpdateError("update watermark mutex timeout is invalid")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise UpdateError("update watermark mutex timeout is invalid") from exc
    if not 0.0 <= timeout <= 300.0:
        raise UpdateError("update watermark mutex timeout is invalid")
    return min(0xFFFFFFFE, max(0, int(timeout * 1000)))


@contextmanager
def _windows_named_mutex(name: str, *, timeout_seconds: float):
    try:
        create_mutex, wait_for_single_object, release_mutex, close_handle, get_last_error = _windows_mutex_api()
        handle = create_mutex(None, False, name)
    except (AttributeError, OSError) as exc:
        raise UpdateError(f"update watermark mutex setup failed: {type(exc).__name__}") from exc
    if not handle:
        raise UpdateError(f"update watermark mutex could not be created: Windows error {get_last_error()}")
    acquired = False
    operation_failed = False
    try:
        try:
            result = int(wait_for_single_object(handle, _mutex_timeout_milliseconds(timeout_seconds)))
        except (OSError, TypeError, ValueError) as exc:
            raise UpdateError(f"update watermark mutex wait failed: {type(exc).__name__}") from exc
        if result in {_WINDOWS_WAIT_OBJECT_0, _WINDOWS_WAIT_ABANDONED}:
            acquired = True
        elif result == _WINDOWS_WAIT_TIMEOUT:
            raise UpdateError("timed out waiting for the update watermark mutex")
        elif result == _WINDOWS_WAIT_FAILED:
            raise UpdateError(f"update watermark mutex wait failed: Windows error {get_last_error()}")
        else:
            raise UpdateError(f"update watermark mutex returned an unexpected wait result: {result}")
        yield
    except BaseException:
        operation_failed = True
        raise
    finally:
        cleanup_errors = []
        try:
            if acquired and not release_mutex(handle):
                cleanup_errors.append(f"release failed with Windows error {get_last_error()}")
        except OSError as exc:
            cleanup_errors.append(f"release failed: {type(exc).__name__}")
        try:
            if not close_handle(handle):
                cleanup_errors.append(f"handle close failed with Windows error {get_last_error()}")
        except OSError as exc:
            cleanup_errors.append(f"handle close failed: {type(exc).__name__}")
        if cleanup_errors and not operation_failed:
            raise UpdateError("update watermark mutex cleanup failed: " + "; ".join(cleanup_errors))


@contextmanager
def _posix_file_mutex(path: Path, *, timeout_seconds: float):
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - all supported POSIX builds provide fcntl
        raise UpdateError("cross-process update watermark locking is unavailable") from exc

    timeout_ms = _mutex_timeout_milliseconds(timeout_seconds)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    else:  # pragma: no cover - supported POSIX platforms expose O_NOFOLLOW
        raise UpdateError("safe cross-process update watermark locking is unavailable")
    descriptor: int | None = None
    acquired = False
    operation_failed = False
    try:
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UpdateError("update watermark lock is not a regular file")
            os.fchmod(descriptor, 0o600)
        except UpdateError:
            raise
        except OSError as exc:
            raise UpdateError(f"update watermark lock could not be opened safely: {type(exc).__name__}") from exc
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise UpdateError(f"update watermark lock failed: {type(exc).__name__}") from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpdateError("timed out waiting for the update watermark lock") from exc
                time.sleep(min(0.05, remaining))
        yield
    except BaseException:
        operation_failed = True
        raise
    finally:
        cleanup_errors = []
        if descriptor is not None:
            if acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError as exc:
                    cleanup_errors.append(f"unlock failed: {type(exc).__name__}")
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_errors.append(f"descriptor close failed: {type(exc).__name__}")
        if cleanup_errors and not operation_failed:
            raise UpdateError("update watermark lock cleanup failed: " + "; ".join(cleanup_errors))


@contextmanager
def _cross_process_watermark_lock(path: Path, *, timeout_seconds: float):
    if os.name == "nt":
        with _windows_named_mutex(_watermark_mutex_name(path), timeout_seconds=timeout_seconds):
            yield
        return
    if os.name == "posix":
        with _posix_file_mutex(path, timeout_seconds=timeout_seconds):
            yield
        return
    raise UpdateError("cross-process update watermark locking is unsupported on this platform")


def _watermark_payload(watermark: UpdateManifestWatermark) -> dict[str, object]:
    _semver_tuple(watermark.version)
    _published_timestamp(watermark.published_at, label="update watermark")
    if _SHA256.fullmatch(watermark.manifest_sha256) is None:
        raise UpdateError("update watermark manifest identity is invalid")
    return {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "version": watermark.version,
        "published_at": watermark.published_at,
        "manifest_sha256": watermark.manifest_sha256,
    }


def _canonical_watermark_bytes(watermark: UpdateManifestWatermark) -> bytes:
    return (
        json.dumps(
            _watermark_payload(watermark),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _read_update_watermark(path: Path) -> UpdateManifestWatermark | None:
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"update watermark could not be read: {type(exc).__name__}") from exc
    if not raw or len(raw) > UPDATE_WATERMARK_MAX_BYTES:
        raise UpdateError("update watermark is empty or exceeds the byte limit")
    payload = _decode_json(raw, label="update watermark")
    expected = {"schema_version", "product", "version", "published_at", "manifest_sha256"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("update watermark has an invalid shape")
    if payload.get("schema_version") != 1 or payload.get("product") != PRODUCT_ID:
        raise UpdateError("update watermark product or schema is invalid")
    watermark = UpdateManifestWatermark(
        version=str(payload.get("version") or ""),
        published_at=str(payload.get("published_at") or ""),
        manifest_sha256=str(payload.get("manifest_sha256") or ""),
    )
    canonical = _canonical_watermark_bytes(watermark)
    if raw != canonical:
        raise UpdateError("update watermark is not canonical or is only partially written")
    return watermark


def _write_update_watermark(path: Path, watermark: UpdateManifestWatermark) -> None:
    raw = _canonical_watermark_bytes(watermark)
    if len(raw) > UPDATE_WATERMARK_MAX_BYTES:  # pragma: no cover - bounded fields make this defensive
        raise UpdateError("update watermark exceeds the byte limit")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise UpdateError(f"update watermark could not be stored: {type(exc).__name__}") from exc
    finally:
        _remove_owned_temporary(temporary)


def _select_update_watermark(
    stored: UpdateManifestWatermark | None,
    candidate: UpdateManifestWatermark,
    *,
    current_version: str,
) -> UpdateManifestWatermark:
    current = _semver_tuple(current_version)
    candidate_version = _semver_tuple(candidate.version)
    if candidate_version < current:
        raise UpdateError("signed update manifest is older than the installed application")
    if stored is None:
        return candidate
    stored_version = _semver_tuple(stored.version)
    if candidate_version < stored_version:
        raise UpdateError("signed update manifest is older than the highest verified update")
    if candidate_version == stored_version:
        if candidate.published_at != stored.published_at or candidate.manifest_sha256 != stored.manifest_sha256:
            raise UpdateError("the same update version has a different signed manifest or publication time")
        return stored
    if _published_timestamp(candidate.published_at, label="update manifest") < _published_timestamp(
        stored.published_at,
        label="update watermark",
    ):
        raise UpdateError("signed update manifest publication time moved backwards")
    return candidate


def verify_update_manifest_watermark(
    manifest: UpdateManifest,
    watermark_path: str | os.PathLike[str],
    *,
    current_version: str = __version__,
    commit: bool = False,
) -> UpdateManifestWatermark:
    """Reject replayed signed manifests and optionally advance the durable watermark."""

    candidate = _watermark_from_manifest(manifest)
    with _UPDATE_WATERMARK_LOCK:
        path = _update_watermark_path(watermark_path, create_parent=True)
        with _cross_process_watermark_lock(
            path,
            timeout_seconds=UPDATE_WATERMARK_MUTEX_TIMEOUT_SECONDS,
        ):
            stored = _read_update_watermark(path)
            selected = _select_update_watermark(stored, candidate, current_version=current_version)
            if commit and selected is candidate:
                _write_update_watermark(path, candidate)
            return selected


def _fetch_https_bytes(
    url: str,
    *,
    label: str,
    limit: int,
    session: Any,
    timeout: tuple[int, int],
) -> bytes:
    try:
        response, _requested_url = _open_https_response(
            url,
            label=label,
            session=session,
            timeout=timeout,
        )
        try:
            response.raise_for_status()
            _validate_https_url(str(response.url), field=f"final {label} URL")
            return _bounded_response_bytes(response, limit=limit)
        finally:
            response.close()
    except UpdateError:
        raise
    except requests.RequestException as exc:
        raise UpdateError(f"{label} request failed: {type(exc).__name__}") from exc


def fetch_update_manifest(
    manifest_url: str,
    *,
    session: Any = requests,
    timeout: tuple[int, int] = UPDATE_TIMEOUT,
) -> UpdateManifest:
    url = _validate_https_url(manifest_url, field="manifest_url")
    raw = _fetch_https_bytes(
        url,
        label="update manifest",
        limit=MANIFEST_MAX_BYTES,
        session=session,
        timeout=timeout,
    )
    payload = _decode_json(raw, label="update manifest")
    signature = _fetch_https_bytes(
        _manifest_signature_url(url),
        label="update manifest signature",
        limit=MANIFEST_SIGNATURE_MAX_BYTES,
        session=session,
        timeout=timeout,
    )
    _verify_manifest_signature(payload, signature)
    return parse_update_manifest(payload)


def check_for_update(
    manifest_url: str,
    *,
    current_version: str = __version__,
    session: Any = requests,
    watermark_path: str | os.PathLike[str] | None = None,
) -> UpdateCheckResult:
    current = _semver_tuple(current_version)
    manifest = fetch_update_manifest(manifest_url, session=session)
    if watermark_path is not None:
        verify_update_manifest_watermark(
            manifest,
            watermark_path,
            current_version=current_version,
            commit=_semver_tuple(manifest.version) == current,
        )
    available = _semver_tuple(manifest.version) > current
    return UpdateCheckResult(current_version=current_version, manifest=manifest, update_available=available)


def _remove_owned_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def download_update_package(
    manifest: UpdateManifest,
    destination: str | os.PathLike[str],
    *,
    session: Any = requests,
    timeout: tuple[int, int] = UPDATE_TIMEOUT,
    watermark_path: str | os.PathLike[str] | None = None,
    current_version: str = __version__,
) -> Path:
    requested_destination = Path(destination).expanduser()
    if not requested_destination.name or requested_destination.name in {".", ".."}:
        raise UpdateError("update package destination is invalid")
    destination_parent = requested_destination.parent.resolve()
    destination_path = destination_parent / requested_destination.name
    if destination_path.is_symlink() or getattr(destination_path, "is_junction", lambda: False)():
        raise UpdateError("update package destination is a symbolic link or junction")
    destination_parent.mkdir(parents=True, exist_ok=True)
    resolved_watermark_path: Path | None = None
    if watermark_path is not None:
        resolved_watermark_path = _update_watermark_path(watermark_path, create_parent=True)
        if os.path.normcase(str(resolved_watermark_path)) == os.path.normcase(str(destination_path)):
            raise UpdateError("update watermark and package destination must be different files")
        verify_update_manifest_watermark(
            manifest,
            resolved_watermark_path,
            current_version=current_version,
            commit=False,
        )
    temporary: Path | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        response, _requested_package_url = _open_https_response(
            manifest.package_url,
            label="update package",
            session=session,
            timeout=timeout,
        )
        try:
            response.raise_for_status()
            _validate_https_url(str(response.url), field="final package URL")
            declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if declared not in (None, "") and (not str(declared).isdigit() or int(declared) != manifest.size):
                raise UpdateError("update package Content-Length does not match the update manifest")
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination_path.name}.",
                suffix=".part",
                dir=destination_path.parent,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        raise UpdateError("update package yielded non-byte content")
                    total += len(chunk)
                    if total > manifest.size or total > PACKAGE_MAX_BYTES:
                        raise UpdateError("update package exceeds the manifest size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        if total != manifest.size:
            raise UpdateError("update package byte count does not match the update manifest")
        if digest.hexdigest() != manifest.sha256:
            raise UpdateError("update package SHA-256 does not match the update manifest")
        if temporary is None:
            raise UpdateError("update package temporary file was not created")
        if resolved_watermark_path is not None:
            # Advance only after the exact signed package has been completely
            # downloaded and hash-verified.  A mere update check or a failed
            # download therefore cannot pin an unavailable future version.
            verify_update_manifest_watermark(
                manifest,
                resolved_watermark_path,
                current_version=current_version,
                commit=True,
            )
        os.replace(temporary, destination_path)
        temporary = None
        return destination_path
    except UpdateError:
        _remove_owned_temporary(temporary)
        raise
    except requests.RequestException as exc:
        _remove_owned_temporary(temporary)
        raise UpdateError(f"update package request failed: {type(exc).__name__}") from exc
    except OSError as exc:
        _remove_owned_temporary(temporary)
        raise UpdateError(f"update package could not be stored: {type(exc).__name__}") from exc


def _validate_zip_component(component: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or component.endswith((" ", "."))
        or any(character in component for character in '<>:"|?*')
        or any(ord(character) < 32 for character in component)
    ):
        raise UpdateError("update ZIP contains an unsafe path component")
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise UpdateError("update ZIP contains a reserved Windows path")


def _validated_zip_entries(archive: zipfile.ZipFile, *, version: str) -> tuple[str, list[zipfile.ZipInfo]]:
    entries = archive.infolist()
    if not entries or len(entries) > ZIP_MAX_ENTRIES:
        raise UpdateError("update ZIP entry count is outside the allowed range")
    expected_root = f"DS_DCF-v{version}"
    seen: set[str] = set()
    total_uncompressed = 0
    for info in entries:
        name = info.filename
        if not isinstance(name, str) or "\\" in name or name.startswith("/") or len(name) > 240:
            raise UpdateError("update ZIP contains an unsafe path")
        path = PurePosixPath(name)
        parts = path.parts
        if not parts or parts[0] != expected_root:
            raise UpdateError("update ZIP must contain exactly one versioned product root")
        for component in parts:
            _validate_zip_component(component)
        key = "/".join(parts).casefold().rstrip("/")
        if key in seen:
            raise UpdateError("update ZIP contains duplicate or case-colliding paths")
        seen.add(key)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise UpdateError("update ZIP contains a symbolic link")
        if info.flag_bits & 0x1:
            raise UpdateError("update ZIP contains an encrypted entry")
        file_type = stat.S_IFMT(mode)
        if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UpdateError("update ZIP contains a non-regular filesystem entry")
        if file_type and stat.S_ISDIR(mode) != info.is_dir():
            raise UpdateError("update ZIP entry type disagrees with its path syntax")
        if len(parts) == 1 and not info.is_dir():
            raise UpdateError("update ZIP product root must be a directory")
        if info.is_dir() and (info.file_size != 0 or info.compress_size != 0):
            raise UpdateError("update ZIP directory entry contains data")
        if info.file_size < 0 or info.compress_size < 0:
            raise UpdateError("update ZIP contains an invalid entry size")
        total_uncompressed += info.file_size
        if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
            raise UpdateError("update ZIP uncompressed size exceeds the limit")
        if info.file_size and info.file_size / max(info.compress_size, 1) > ZIP_MAX_COMPRESSION_RATIO:
            raise UpdateError("update ZIP contains an excessive compression ratio")
    required = {
        f"{expected_root}/DS_DCF.exe",
        f"{expected_root}/release-manifest.json",
        f"{expected_root}/build-provenance.json",
    }
    if not required.issubset({info.filename for info in entries}):
        raise UpdateError("update ZIP is missing its executable or internal release metadata")
    return expected_root, entries


def _validate_internal_release_manifest(raw: bytes, *, version: str) -> None:
    payload = _decode_json(raw, label="internal release manifest")
    expected = {"schema_version", "product", "version", "entrypoint"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("internal release manifest has an invalid shape")
    if payload != {
        "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_ID,
        "version": version,
        "entrypoint": "DS_DCF.exe",
    }:
        raise UpdateError("internal release manifest does not match the requested version")


def _validate_build_provenance(raw: bytes, *, version: str) -> str:
    payload = _decode_json(raw, label="internal build provenance")
    expected = {"schema_version", "product", "version", "git_sha"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise UpdateError("internal build provenance has an invalid shape")
    git_sha = str(payload.get("git_sha") or "").strip()
    if (
        payload
        != {
            "schema_version": BUILD_PROVENANCE_SCHEMA_VERSION,
            "product": PRODUCT_ID,
            "version": version,
            "git_sha": git_sha,
        }
        or _GIT_SHA.fullmatch(git_sha) is None
    ):
        raise UpdateError("internal build provenance does not identify an exact Git commit")
    return git_sha


def _validate_archive_metadata(archive: zipfile.ZipFile, *, package_root: str, version: str) -> str:
    try:
        _validate_internal_release_manifest(
            archive.read(f"{package_root}/release-manifest.json"),
            version=version,
        )
        return _validate_build_provenance(
            archive.read(f"{package_root}/build-provenance.json"),
            version=version,
        )
    except UpdateError:
        raise
    except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UpdateError("update ZIP internal release metadata could not be read") from exc


def _relative_archive_files(entries: list[zipfile.ZipInfo], package_root: str) -> dict[str, zipfile.ZipInfo]:
    result: dict[str, zipfile.ZipInfo] = {}
    prefix = package_root + "/"
    for info in entries:
        if info.is_dir():
            continue
        relative = info.filename.removeprefix(prefix)
        if not relative or relative == info.filename:
            raise UpdateError("update ZIP file is outside the product root")
        result[relative.casefold()] = info
    return result


def _verify_install_against_archive(
    install_dir: Path,
    stored_package: Path,
    archive: zipfile.ZipFile,
    package_root: str,
    entries: list[zipfile.ZipInfo],
    manifest: UpdateManifest,
) -> Path:
    """Verify an install byte-for-byte against the already SHA-256-bound ZIP."""

    install_is_junction = getattr(install_dir, "is_junction", lambda: False)
    package_is_junction = getattr(stored_package, "is_junction", lambda: False)
    if install_dir.is_symlink() or install_is_junction():
        raise UpdateError("installed application directory is a symbolic link or junction")
    if stored_package.is_symlink() or package_is_junction():
        raise UpdateError("stored portable package is a symbolic link or junction")
    if not install_dir.is_dir():
        raise UpdateError("installed application directory is missing")
    version_root = install_dir.parent.resolve()
    resolved_install = install_dir.resolve()
    resolved_package = stored_package.resolve()
    if resolved_install.parent != version_root or resolved_package.parent != version_root:
        raise UpdateError("installed application or package escapes its version directory")
    expected = _relative_archive_files(entries, package_root)
    actual: dict[str, Path] = {}
    try:
        for path in install_dir.rglob("*"):
            is_junction = getattr(path, "is_junction", lambda: False)
            if path.is_symlink() or is_junction():
                raise UpdateError("installed application contains a symbolic link or junction")
            resolved = path.resolve()
            if resolved_install not in resolved.parents and resolved != resolved_install:
                raise UpdateError("installed application path escapes its version directory")
            if path.is_file():
                relative = path.relative_to(install_dir).as_posix()
                key = relative.casefold()
                if key in actual:
                    raise UpdateError("installed application contains case-colliding files")
                actual[key] = path
    except OSError as exc:
        raise UpdateError(f"installed application enumeration failed: {type(exc).__name__}") from exc
    if set(actual) != set(expected):
        missing = len(set(expected) - set(actual))
        extra = len(set(actual) - set(expected))
        raise UpdateError(f"installed application file set differs from package (missing={missing}, extra={extra})")

    for key, info in expected.items():
        actual_size, actual_digest = _sha256_file(actual[key])
        if actual_size != info.file_size:
            raise UpdateError(f"installed file size differs from package: {info.filename}")
        expected_digest = hashlib.sha256()
        try:
            with archive.open(info, "r") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    expected_digest.update(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise UpdateError(f"update ZIP entry could not be verified: {info.filename}") from exc
        if actual_digest != expected_digest.hexdigest():
            raise UpdateError(f"installed file SHA-256 differs from package: {info.filename}")

    package_size, package_digest = _sha256_file(stored_package)
    if package_size != manifest.size or package_digest != manifest.sha256:
        raise UpdateError("stored portable package differs from the update manifest")
    executable = install_dir / "DS_DCF.exe"
    internal = install_dir / "release-manifest.json"
    if not executable.is_file() or not internal.is_file():
        raise UpdateError("installed update is missing its executable or release manifest")
    try:
        _validate_internal_release_manifest(internal.read_bytes(), version=manifest.version)
    except OSError as exc:
        raise UpdateError(f"installed release manifest could not be read: {type(exc).__name__}") from exc
    return executable


def verify_update_package(
    package_path: str | os.PathLike[str],
    manifest: UpdateManifest,
) -> Path:
    """Verify a local update ZIP without changing the installed version library.

    The bootstrap installer uses this same gate before its first installation,
    so a release ZIP cannot have a weaker trust path than an in-app update.
    """

    package = Path(package_path).expanduser().resolve()
    if not package.is_file():
        raise UpdateError("update package does not exist")
    package_size, package_digest = _sha256_file(package)
    if package_size != manifest.size:
        raise UpdateError("stored update package size does not match the manifest")
    if package_digest != manifest.sha256:
        raise UpdateError("stored update package SHA-256 does not match the manifest")
    try:
        with zipfile.ZipFile(package, "r") as archive:
            package_root, entries = _validated_zip_entries(archive, version=manifest.version)
            _validate_archive_metadata(archive, package_root=package_root, version=manifest.version)
    except UpdateError:
        raise
    except zipfile.BadZipFile as exc:
        raise UpdateError("update package is not a valid ZIP archive") from exc
    except OSError as exc:
        raise UpdateError(f"update package could not be read: {type(exc).__name__}") from exc
    return package


def install_update_package(
    package_path: str | os.PathLike[str],
    manifest: UpdateManifest,
    *,
    versions_root: str | os.PathLike[str] | None = None,
    create_shortcut: bool = True,
    current_version: str = __version__,
) -> InstalledUpdate:
    if _semver_tuple(manifest.version) <= _semver_tuple(current_version):
        raise UpdateError("refusing to install the same version or a downgrade")
    package = verify_update_package(package_path, manifest)

    root = Path(versions_root).expanduser().resolve() if versions_root is not None else default_version_library_root()
    target_version = (root / manifest.version).resolve()
    if target_version.parent != root:
        raise UpdateError("update target escapes the version store")
    target_install = target_version / "app"
    portable_name = f"DS_DCF-v{manifest.version}-windows-x64-portable.zip"
    target_package = target_version / portable_name
    staging_parent: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package, "r") as archive:
            package_root, entries = _validated_zip_entries(archive, version=manifest.version)
            _validate_archive_metadata(archive, package_root=package_root, version=manifest.version)
            if target_version.exists():
                executable = _verify_install_against_archive(
                    target_install,
                    target_package,
                    archive,
                    package_root,
                    entries,
                    manifest,
                )
                shortcut = create_desktop_shortcut(executable) if create_shortcut else None
                return InstalledUpdate(
                    manifest.version,
                    target_version,
                    target_install,
                    executable,
                    target_package,
                    shortcut,
                )

            staging_parent = Path(tempfile.mkdtemp(prefix=f".{manifest.version}-", dir=root)).resolve()
            staging_version = (staging_parent / manifest.version).resolve()
            staging_install = staging_version / "app"
            staging_package = staging_version / portable_name
            staging_install.mkdir(parents=True, exist_ok=False)
            for info in entries:
                relative = PurePosixPath(info.filename)
                destination = staging_install.joinpath(*relative.parts[1:]).resolve()
                if staging_install not in destination.parents and destination != staging_install:
                    raise UpdateError("update ZIP extraction escaped the staging directory")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, open(destination, "xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            shutil.copyfile(package, staging_package)
            _verify_install_against_archive(
                staging_install,
                staging_package,
                archive,
                package_root,
                entries,
                manifest,
            )
            os.replace(staging_version, target_version)
    except UpdateError:
        raise
    except zipfile.BadZipFile as exc:
        raise UpdateError("update package is not a valid ZIP archive") from exc
    except (OSError, RuntimeError, shutil.Error) as exc:
        raise UpdateError(f"update installation failed: {type(exc).__name__}") from exc
    finally:
        if staging_parent is not None:
            shutil.rmtree(staging_parent, ignore_errors=True)

    executable = target_install / "DS_DCF.exe"
    shortcut = create_desktop_shortcut(executable) if create_shortcut else None
    return InstalledUpdate(
        manifest.version,
        target_version,
        target_install,
        executable,
        target_package,
        shortcut,
    )


def _windows_powershell_executable() -> Path:
    if os.name != "nt":
        raise UpdateError("PowerShell shortcut creation is available only on Windows")
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError) as exc:
        raise UpdateError("Windows system directory lookup failed") from exc
    if length <= 0 or length >= len(buffer) or not buffer.value:
        raise UpdateError("Windows system directory lookup failed")
    executable = (Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe").resolve()
    if not executable.is_file():
        raise UpdateError("the system PowerShell executable is unavailable")
    return executable


def create_desktop_shortcut(executable: str | os.PathLike[str]) -> Path | None:
    """Create the stable shortcut inside the user's version library."""
    if os.name != "nt":
        return None
    target = Path(executable).resolve()
    if not target.is_file() or target.name.casefold() != "ds_dcf.exe":
        raise UpdateError("desktop shortcut target is not a DS_DCF executable")
    script = """
$desktop = [Environment]::GetFolderPath('Desktop')
$library = Join-Path $desktop '6BUYING_POINT'
[System.IO.Directory]::CreateDirectory($library) | Out-Null
$shortcutPath = Join-Path $library 'DS_DCF.lnk'
$target = [Environment]::GetEnvironmentVariable('DS_DCF_SHORTCUT_TARGET')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = Split-Path -Parent $target
$shortcut.Description = 'DS_DCF valuation and seven-framework diagnostics'
$shortcut.Save()
Write-Output $shortcutPath
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = dict(os.environ)
    environment["DS_DCF_SHORTCUT_TARGET"] = str(target)
    powershell = _windows_powershell_executable()
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"desktop shortcut creation failed: {type(exc).__name__}") from exc
    shortcut_text = completed.stdout.strip().splitlines()
    if not shortcut_text:
        raise UpdateError("desktop shortcut creation returned no path")
    shortcut = Path(shortcut_text[-1]).resolve()
    if (
        shortcut.name.casefold() != "ds_dcf.lnk"
        or shortcut.parent.name != DESKTOP_LIBRARY_NAME
        or not shortcut.is_file()
    ):
        raise UpdateError("desktop shortcut was not created at the expected path")
    return shortcut


def _read_update_config(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"update_config.json could not be read: {type(exc).__name__}") from exc
    if len(raw) > MANIFEST_MAX_BYTES:
        raise UpdateError("update_config.json exceeds the byte limit")
    payload = _decode_json(raw, label="update_config.json")
    if not isinstance(payload, Mapping) or set(payload) != {"manifest_url"}:
        raise UpdateError("update_config.json has an invalid shape")
    value = payload.get("manifest_url")
    return None if value is None else _validate_https_url(value, field="manifest_url")


def load_update_manifest_url(
    app_root: str | os.PathLike[str],
    *,
    library_root: str | os.PathLike[str] | None = None,
) -> str | None:
    environment = str(os.environ.get("DS_DCF_UPDATE_MANIFEST_URL") or "").strip()
    if environment:
        return _validate_https_url(environment, field="DS_DCF_UPDATE_MANIFEST_URL")
    external_root = (
        Path(library_root).expanduser().resolve() if library_root is not None else default_version_library_root()
    )
    external = external_root / "update_config.json"
    bundled = Path(app_root).resolve() / "update_config.json"
    if external.is_file():
        return _read_update_config(external)
    if bundled.is_file():
        return _read_update_config(bundled)
    return None


__all__ = [
    "DESKTOP_LIBRARY_NAME",
    "InstalledUpdate",
    "PRODUCT_ID",
    "UpdateCheckResult",
    "UpdateError",
    "UpdateManifest",
    "UpdateManifestWatermark",
    "check_for_update",
    "create_desktop_shortcut",
    "download_update_package",
    "default_version_library_root",
    "fetch_update_manifest",
    "install_update_package",
    "load_update_manifest_url",
    "parse_update_manifest",
    "verify_update_manifest_watermark",
    "verify_update_package",
]
