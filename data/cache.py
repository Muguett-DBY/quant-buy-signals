"""Safe cache primitives used by the data layer.

``Cache`` is a small SQLite KV cache kept for backwards-compatible callers.
``SafeFileCache`` is the preferred cache for the complete quote/financial
snapshot: it is a single, checksummed, versioned JSON/GZIP file and never
executes data while loading.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import orjson

from config import CACHE_TTL_SECONDS


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "cache.db")
_TYPE_KEY = "__ds_dcf_cache_type__"
_SAFE_CACHE_FORMAT = "ds-dcf-safe-cache-orjson-v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_ARTIFACT_UNSET = object()
_TAGGED_JSON_KINDS = {"datetime", "date", "decimal", "bytes", "tuple", "set", "dataframe"}
_MAX_TYPE_PATHS = 1_000_000
_MAX_TYPE_PATH_DEPTH = 100


class CacheError(RuntimeError):
    """A cache operation failed; callers must not treat this as a cache miss."""


class SafeCacheError(RuntimeError):
    """A safe snapshot could not be written."""


class SafeCacheConflict(SafeCacheError):
    """A conditional safe-cache write observed a different active payload."""


def _encode_json_value(value: Any) -> Any:
    """Convert supported values to lossless, non-executable JSON structures."""
    if isinstance(value, pd.DataFrame):
        return {
            _TYPE_KEY: "dataframe",
            "columns": [_encode_json_value(column) for column in value.columns.tolist()],
            "records": _encode_json_value(value.to_dict(orient="records")),
        }
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, datetime):
        return {_TYPE_KEY: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE_KEY: "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {_TYPE_KEY: "decimal", "value": str(value)}
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "items": [_encode_json_value(item) for item in value]}
    if isinstance(value, set):
        encoded = [_encode_json_value(item) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return {_TYPE_KEY: "set", "items": encoded}
    if isinstance(value, Mapping):
        encoded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"cache mapping keys must be strings, got {type(key).__name__}")
            encoded[key] = _encode_json_value(item)
        return encoded
    if isinstance(value, (list, range)):
        return [_encode_json_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value

    # NumPy scalar types expose ``item``.  Convert them before rejecting an
    # unsupported object instead of silently stringifying it.
    item_method = getattr(value, "item", None)
    if callable(item_method):
        converted = item_method()
        if converted is not value:
            return _encode_json_value(converted)
    raise TypeError(f"unsupported cache value type: {type(value).__name__}")


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode_json_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(_TYPE_KEY)
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "decimal":
        return Decimal(value["value"])
    if kind == "bytes":
        return base64.b64decode(value["value"], validate=True)
    if kind == "tuple":
        return tuple(_decode_json_value(item) for item in value["items"])
    if kind == "set":
        return {_decode_json_value(item) for item in value["items"]}
    if kind == "dataframe":
        columns = [_decode_json_value(column) for column in value["columns"]]
        records = _decode_json_value(value["records"])
        return pd.DataFrame.from_records(records, columns=columns)
    return {key: _decode_json_value(item) for key, item in value.items()}


def _collect_type_paths(value: Any) -> list[list[str | int]]:
    """Index tagged values once at write time so large primitive trees load fast."""
    paths: list[list[str | int]] = []
    stack: list[tuple[tuple[str | int, ...], Any]] = [((), value)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            if node.get(_TYPE_KEY) in _TAGGED_JSON_KINDS:
                paths.append(list(path))
            for key, item in node.items():
                if isinstance(item, (dict, list)):
                    stack.append(((*path, key), item))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, (dict, list)):
                    stack.append(((*path, index), item))
    paths.sort(key=lambda path: tuple((0, item) if isinstance(item, int) else (1, item) for item in path))
    return paths


def _indexed_child(container: Any, segment: str | int) -> Any:
    if isinstance(container, dict) and isinstance(segment, str) and segment in container:
        return container[segment]
    if (
        isinstance(container, (list, tuple))
        and isinstance(segment, int)
        and not isinstance(segment, bool)
        and 0 <= segment < len(container)
    ):
        return container[segment]
    raise ValueError("cache type path does not resolve")


def _decode_tagged_json_value(value: dict[str, Any]) -> Any:
    kind = value.get(_TYPE_KEY)
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "decimal":
        return Decimal(value["value"])
    if kind == "bytes":
        return base64.b64decode(value["value"], validate=True)
    if kind == "tuple":
        return tuple(value["items"])
    if kind == "set":
        return set(value["items"])
    if kind == "dataframe":
        return pd.DataFrame.from_records(value["records"], columns=value["columns"])
    raise ValueError("cache type path points to an unsupported tagged value")


def _decode_indexed_json_value(value: Any, raw_paths: Any) -> Any:
    """Decode only writer-indexed tagged nodes; primitive financial trees stay in place."""
    if not isinstance(raw_paths, list) or len(raw_paths) > _MAX_TYPE_PATHS:
        raise ValueError("cache type path index is invalid")
    paths: list[tuple[str | int, ...]] = []
    seen: set[tuple[str | int, ...]] = set()
    for raw_path in raw_paths:
        if not isinstance(raw_path, list) or len(raw_path) > _MAX_TYPE_PATH_DEPTH:
            raise ValueError("cache type path is invalid")
        path: list[str | int] = []
        for segment in raw_path:
            if isinstance(segment, bool) or not isinstance(segment, (str, int)):
                raise ValueError("cache type path segment is invalid")
            if isinstance(segment, int) and segment < 0:
                raise ValueError("cache type path index is negative")
            path.append(segment)
        prepared = tuple(path)
        if prepared in seen:
            raise ValueError("cache type path index contains duplicates")
        seen.add(prepared)
        paths.append(prepared)

    root = value
    for path in sorted(paths, key=lambda item: len(item), reverse=True):
        target = root
        for segment in path:
            target = _indexed_child(target, segment)
        if not isinstance(target, dict) or target.get(_TYPE_KEY) not in _TAGGED_JSON_KINDS:
            raise ValueError("cache type path does not identify a tagged value")
        decoded = _decode_tagged_json_value(target)
        if not path:
            root = decoded
            continue
        parent = root
        for segment in path[:-1]:
            parent = _indexed_child(parent, segment)
        final = path[-1]
        if isinstance(parent, dict) and isinstance(final, str) and final in parent:
            parent[final] = decoded
        elif (
            isinstance(parent, list)
            and isinstance(final, int)
            and not isinstance(final, bool)
            and 0 <= final < len(parent)
        ):
            parent[final] = decoded
        else:
            raise ValueError("cache type path parent is not mutable")
    return root


def _canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON without the stdlib's large-cache cost."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


class Cache:
    """SQLite KV cache with per-entry expiry and explicit failures."""

    def __init__(self, db_path: str = DB_PATH, ttl: int = CACHE_TTL_SECONDS):
        self.db_path = db_path
        self.ttl = int(ttl)
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            try:
                self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            except sqlite3.Error as exc:
                raise CacheError(f"cannot open cache database {self.db_path}: {exc}") from exc
        return self._local.conn

    def _init_db(self) -> None:
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL
                    )
                    """
                )
                columns = {row[1] for row in conn.execute("PRAGMA table_info(cache)")}
                if "expires_at" not in columns:
                    conn.execute("ALTER TABLE cache ADD COLUMN expires_at REAL")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON cache(expires_at)")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise CacheError(f"cannot initialize cache database {self.db_path}: {exc}") from exc

    def get(self, key: str) -> Any:
        """Return a cached value, or ``None`` only for a genuine miss/expiry."""
        try:
            row = self._conn.execute("SELECT value, updated_at, expires_at FROM cache WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            value_str, updated_at, expires_at = row
            effective_expiry = expires_at
            if effective_expiry is None:  # legacy rows created before per-entry TTL
                effective_expiry = float(updated_at) + self.ttl
            if time.time() >= float(effective_expiry):
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
            return _decode_json_value(json.loads(value_str))
        except Exception as exc:
            raise CacheError(f"cannot read cache key {key!r}: {exc}") from exc

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store ``value`` without lossy ``str`` coercion."""
        ttl_seconds = self.ttl if ttl is None else int(ttl)
        if ttl_seconds < 0:
            raise ValueError("ttl must be non-negative")
        now = time.time()
        try:
            value_str = _canonical_json_bytes(_encode_json_value(value)).decode("utf-8")
            self._conn.execute(
                """
                INSERT OR REPLACE INTO cache (key, value, updated_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, value_str, now, now + ttl_seconds),
            )
            self._conn.commit()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise CacheError(f"cannot write cache key {key!r}: {exc}") from exc

    def clear(self) -> None:
        try:
            self._conn.execute("DELETE FROM cache")
            self._conn.commit()
        except sqlite3.Error as exc:
            raise CacheError(f"cannot clear cache: {exc}") from exc

    def clear_expired(self) -> None:
        try:
            self._conn.execute(
                "DELETE FROM cache WHERE COALESCE(expires_at, updated_at + ?) <= ?",
                (self.ttl, time.time()),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise CacheError(f"cannot clear expired cache: {exc}") from exc

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


@dataclass(frozen=True)
class SafeCacheLoadResult:
    """Result of a safe snapshot load.

    Integrity, schema and expiry failures are explicit misses with a human
    readable reason; callers do not need to catch data-decoding exceptions.
    """

    hit: bool
    value: Any = None
    reason: str = ""
    metadata: Mapping[str, Any] | None = None


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.RLock:
    resolved = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(resolved, threading.RLock())


@contextmanager
def _cross_process_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive one-byte advisory lock on Windows or POSIX."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI only
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on POSIX CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _infer_counts(value: Any) -> dict[str, int]:
    if isinstance(value, Mapping):
        counts: dict[str, int] = {}
        for key, item in value.items():
            if isinstance(item, (pd.DataFrame, Mapping, list, tuple, set)):
                counts[str(key)] = len(item)
        return counts
    if isinstance(value, (pd.DataFrame, list, tuple, set, Mapping)):
        return {"items": len(value)}
    return {}


class SafeFileCache:
    """Atomic, checksummed JSON/GZIP snapshot cache.

    A typical caller stores both active datasets in one generation::

        cache.save({"quotes": quotes_df, "fin_map": fin_map})
        loaded = cache.load()
        if loaded.hit:
            quotes_df = loaded.value["quotes"]
            fin_map = loaded.value["fin_map"]
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        schema_version: int = 1,
        ttl: int = CACHE_TTL_SECONDS,
        max_uncompressed_bytes: int = 512 * 1024 * 1024,
    ):
        self.path = Path(path)
        self.schema_version = int(schema_version)
        self.ttl = int(ttl)
        self.max_uncompressed_bytes = int(max_uncompressed_bytes)
        self._thread_lock = _thread_lock_for(self.path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def save(
        self,
        value: Any,
        *,
        counts: Mapping[str, int] | None = None,
        ttl: int | None = None,
    ) -> Mapping[str, Any]:
        metadata, envelope_bytes = self._prepare_save(value, counts=counts, ttl=ttl)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._thread_lock, _cross_process_lock(self._lock_path):
                self._write_envelope_unlocked(envelope_bytes)
        except Exception as exc:
            raise SafeCacheError(f"cannot save safe cache {self.path}: {exc}") from exc
        return metadata

    def compare_and_swap(
        self,
        value: Any,
        *,
        expected_payload_sha256: str | None,
        expected_artifact_sha256: str | None | object = _EXPECTED_ARTIFACT_UNSET,
        allow_replace_invalid: bool = False,
        counts: Mapping[str, int] | None = None,
        ttl: int | None = None,
    ) -> Mapping[str, Any]:
        """Atomically save only if the verified active payload still matches.

        ``None`` means that no active file is expected.  The comparison and
        replacement use the same process/thread lock as ordinary ``save``
        calls, so a writer that bypasses a higher-level promotion lock cannot
        slip a generation between the final comparison and replacement.
        """
        if expected_payload_sha256 is not None and not (
            isinstance(expected_payload_sha256, str) and _SHA256_HEX.fullmatch(expected_payload_sha256)
        ):
            raise ValueError("expected_payload_sha256 must be a lowercase SHA-256 or None")
        if (
            expected_artifact_sha256 is not _EXPECTED_ARTIFACT_UNSET
            and expected_artifact_sha256 is not None
            and not (isinstance(expected_artifact_sha256, str) and _SHA256_HEX.fullmatch(expected_artifact_sha256))
        ):
            raise ValueError("expected_artifact_sha256 must be a lowercase SHA-256 or None")
        metadata, envelope_bytes = self._prepare_save(value, counts=counts, ttl=ttl)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._thread_lock, _cross_process_lock(self._lock_path):
                current = self._load_unlocked(allow_expired=True)
                if not current.hit and current.reason != "not_found":
                    stored_schema = (
                        current.metadata.get("schema_version") if isinstance(current.metadata, Mapping) else None
                    )
                    newer_schema = (
                        isinstance(stored_schema, int)
                        and not isinstance(stored_schema, bool)
                        and stored_schema > self.schema_version
                    )
                    if not allow_replace_invalid or newer_schema:
                        raise SafeCacheError(
                            f"cannot compare against invalid active cache {self.path}: {current.reason}"
                        )
                current_hash = None
                current_artifact_hash = None
                if isinstance(current.metadata, Mapping):
                    candidate = current.metadata.get("payload_sha256")
                    current_hash = (
                        candidate if isinstance(candidate, str) and _SHA256_HEX.fullmatch(candidate) else None
                    )
                    artifact = current.metadata.get("artifact_sha256")
                    current_artifact_hash = (
                        artifact if isinstance(artifact, str) and _SHA256_HEX.fullmatch(artifact) else None
                    )
                if (
                    expected_artifact_sha256 is not _EXPECTED_ARTIFACT_UNSET
                    and current_artifact_hash != expected_artifact_sha256
                ):
                    raise SafeCacheConflict(
                        f"active artifact changed: expected {expected_artifact_sha256}, found {current_artifact_hash}"
                    )
                if current_hash != expected_payload_sha256:
                    raise SafeCacheConflict(
                        f"active payload changed: expected {expected_payload_sha256}, found {current_hash}"
                    )
                self._write_envelope_unlocked(envelope_bytes)
        except SafeCacheConflict:
            raise
        except SafeCacheError:
            raise
        except Exception as exc:
            raise SafeCacheError(f"cannot save safe cache {self.path}: {exc}") from exc
        return metadata

    def read_bytes_if_payload(self, expected_payload_sha256: str) -> bytes:
        """Return one immutable artifact image only if its payload still matches.

        The verification and byte read are protected by the ordinary cache
        lock.  This is intended for audit/export callers that must hash the
        exact generation they analysed instead of a later writer's file.
        """
        if (
            not isinstance(expected_payload_sha256, str)
            or len(expected_payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_payload_sha256)
        ):
            raise ValueError("expected_payload_sha256 must be a lowercase SHA-256")
        try:
            with self._thread_lock, _cross_process_lock(self._lock_path):
                current = self._load_unlocked(allow_expired=True)
                current_hash = (
                    current.metadata.get("payload_sha256")
                    if current.hit and isinstance(current.metadata, Mapping)
                    else None
                )
                if current_hash != expected_payload_sha256:
                    raise SafeCacheConflict(
                        "active payload changed before artifact capture: "
                        f"expected {expected_payload_sha256}, found {current_hash}"
                    )
                return self.path.read_bytes()
        except SafeCacheConflict:
            raise
        except Exception as exc:
            raise SafeCacheError(f"cannot capture safe cache {self.path}: {exc}") from exc

    def _prepare_save(
        self,
        value: Any,
        *,
        counts: Mapping[str, int] | None,
        ttl: int | None,
    ) -> tuple[Mapping[str, Any], bytes]:
        ttl_seconds = self.ttl if ttl is None else int(ttl)
        if ttl_seconds < 0:
            raise ValueError("ttl must be non-negative")
        encoded_payload = _encode_json_value(value)
        type_paths = _collect_type_paths(encoded_payload)
        payload_bytes = _canonical_json_bytes(encoded_payload)
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        inferred_counts = _infer_counts(value)
        expected_counts = dict(counts) if counts is not None else inferred_counts
        expected_counts = {str(key): int(item) for key, item in expected_counts.items()}
        if expected_counts != inferred_counts:
            raise ValueError(f"provided counts {expected_counts} do not match payload {inferred_counts}")
        now = time.time()
        metadata = {
            "format": _SAFE_CACHE_FORMAT,
            "schema_version": self.schema_version,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "counts": expected_counts,
            "payload_sha256": payload_hash,
            "type_paths": type_paths,
        }
        metadata["metadata_sha256"] = hashlib.sha256(_canonical_json_bytes(metadata)).hexdigest()
        envelope = dict(metadata)
        envelope["payload"] = encoded_payload
        envelope_bytes = _canonical_json_bytes(envelope)

        return metadata, envelope_bytes

    def _write_envelope_unlocked(self, envelope_bytes: bytes) -> None:
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=self.path.parent,
            )
            try:
                with os.fdopen(fd, "wb") as raw:
                    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as zipped:
                        zipped.write(envelope_bytes)
                    raw.flush()
                    os.fsync(raw.fileno())
                os.replace(temp_path, self.path)
                temp_path = None
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _load_unlocked(
        self,
        *,
        expected_counts: Mapping[str, int] | None = None,
        allow_expired: bool = False,
    ) -> SafeCacheLoadResult:
        if not self.path.is_file():
            return SafeCacheLoadResult(False, reason="not_found")
        artifact_hash: str | None = None
        try:
            if self.path.stat().st_size > self.max_uncompressed_bytes:
                return SafeCacheLoadResult(False, reason="size_limit_exceeded")
            artifact_digest = hashlib.sha256()
            with open(self.path, "rb") as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    artifact_digest.update(chunk)
            artifact_hash = artifact_digest.hexdigest()
            with gzip.open(self.path, "rb") as zipped:
                raw = zipped.read(self.max_uncompressed_bytes + 1)
            if len(raw) > self.max_uncompressed_bytes:
                return SafeCacheLoadResult(
                    False,
                    reason="size_limit_exceeded",
                    metadata={"artifact_sha256": artifact_hash},
                )
            try:
                envelope = orjson.loads(raw)
            except orjson.JSONDecodeError:
                return SafeCacheLoadResult(
                    False,
                    reason="invalid_json",
                    metadata={"artifact_sha256": artifact_hash},
                )
            if not isinstance(envelope, dict):
                return SafeCacheLoadResult(
                    False,
                    reason="invalid_envelope",
                    metadata={"artifact_sha256": artifact_hash},
                )
            stored_metadata = {key: value for key, value in envelope.items() if key != "payload"}
            metadata = {**stored_metadata, "artifact_sha256": artifact_hash}
            if envelope.get("format") != _SAFE_CACHE_FORMAT:
                return SafeCacheLoadResult(False, reason="format_mismatch", metadata=metadata)
            if envelope.get("schema_version") != self.schema_version:
                return SafeCacheLoadResult(False, reason="schema_version_mismatch", metadata=metadata)
            required_keys = {
                "format",
                "schema_version",
                "created_at",
                "expires_at",
                "counts",
                "payload_sha256",
                "metadata_sha256",
                "payload",
            }
            if "type_paths" in envelope:
                required_keys.add("type_paths")
            if set(envelope) != required_keys:
                return SafeCacheLoadResult(False, reason="invalid_envelope_keys", metadata=metadata)
            created_at = envelope.get("created_at")
            expires_at = envelope.get("expires_at")
            if (
                isinstance(created_at, bool)
                or not isinstance(created_at, (int, float))
                or not math.isfinite(float(created_at))
                or float(created_at) <= 0
            ):
                return SafeCacheLoadResult(False, reason="invalid_created_at", metadata=metadata)
            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(float(expires_at))
                or float(expires_at) < float(created_at)
            ):
                return SafeCacheLoadResult(False, reason="invalid_expiry", metadata=metadata)
            declared_metadata_hash = stored_metadata.get("metadata_sha256")
            checksum_metadata = dict(stored_metadata)
            checksum_metadata.pop("metadata_sha256", None)
            actual_metadata_hash = hashlib.sha256(_canonical_json_bytes(checksum_metadata)).hexdigest()
            if not isinstance(declared_metadata_hash, str) or not _SHA256_HEX.fullmatch(declared_metadata_hash):
                return SafeCacheLoadResult(False, reason="invalid_metadata_hash", metadata=metadata)
            if declared_metadata_hash != actual_metadata_hash:
                return SafeCacheLoadResult(False, reason="metadata_hash_mismatch", metadata=metadata)
            if not allow_expired and time.time() >= float(expires_at):
                return SafeCacheLoadResult(False, reason="expired", metadata=metadata)
            encoded_payload = envelope.get("payload")
            actual_hash = hashlib.sha256(_canonical_json_bytes(encoded_payload)).hexdigest()
            declared_payload_hash = envelope.get("payload_sha256")
            if not isinstance(declared_payload_hash, str) or not _SHA256_HEX.fullmatch(declared_payload_hash):
                return SafeCacheLoadResult(False, reason="invalid_payload_hash", metadata=metadata)
            if actual_hash != declared_payload_hash:
                return SafeCacheLoadResult(False, reason="hash_mismatch", metadata=metadata)
            type_paths = envelope.get("type_paths")
            value = (
                _decode_json_value(encoded_payload)
                if type_paths is None
                else _decode_indexed_json_value(encoded_payload, type_paths)
            )
            actual_counts = _infer_counts(value)
            stored_counts = envelope.get("counts")
            if not isinstance(stored_counts, dict) or actual_counts != stored_counts:
                return SafeCacheLoadResult(False, reason="count_mismatch", metadata=metadata)
            if expected_counts is not None:
                normalized = {str(key): int(item) for key, item in expected_counts.items()}
                if normalized != actual_counts:
                    return SafeCacheLoadResult(False, reason="expected_count_mismatch", metadata=metadata)
            return SafeCacheLoadResult(True, value=value, metadata=metadata)
        except Exception as exc:
            metadata = {"artifact_sha256": artifact_hash} if artifact_hash is not None else None
            return SafeCacheLoadResult(False, reason=f"read_error:{type(exc).__name__}", metadata=metadata)

    def load(
        self,
        *,
        expected_counts: Mapping[str, int] | None = None,
        allow_expired: bool = False,
    ) -> SafeCacheLoadResult:
        try:
            with self._thread_lock, _cross_process_lock(self._lock_path):
                return self._load_unlocked(
                    expected_counts=expected_counts,
                    allow_expired=allow_expired,
                )
        except Exception as exc:
            return SafeCacheLoadResult(False, reason=f"read_error:{type(exc).__name__}")
