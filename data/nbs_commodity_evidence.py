"""Official NBS commodity-price context for the strong-cycle model.

The National Bureau of Statistics publishes ten-day prices for 50 important
means of production.  The observations corroborate commodity identity and the
latest spot-price direction.  They are deliberately context-only: a current
official quote does not by itself prove a five-year cycle or a cycle bottom.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import hashlib
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests

from config import CACHE_DIRECTORY
from data.as_of import shanghai_today
from data.cache import SafeFileCache
from data.provider_http import read_bounded_response_bytes, thread_local_session


NBS_INDEX_URL = "https://www.stats.gov.cn/sj/zxfbhjd/"
NBS_SOURCE_NAME = "国家统计局流通领域重要生产资料市场价格"
NBS_CACHE_DIR = CACHE_DIRECTORY / "commodity_cycle"
NBS_CACHE_MODEL_ID = "nbs-means-of-production-v1"
NBS_CACHE_SCHEMA_VERSION = 1
NBS_CACHE_TTL_SECONDS = 18 * 3600
NBS_MAX_RESPONSE_BYTES = 3 * 1024 * 1024
NBS_MAX_ARTICLE_BYTES = 6 * 1024 * 1024
NBS_MAX_ARTICLE_PROBES = 36
NBS_TIMEOUT = (15, 30)

INDUSTRY_PRODUCT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "STEEL": ("螺纹钢",),
    "NONFERROUS": ("电解铜",),
    "CHEMICAL": ("甲醇",),
    "BUILDING_MATERIAL": ("普通硅酸盐水泥",),
    "OIL_GAS": ("液化天然气",),
    "COAL": ("焦煤",),
}

_CANONICAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ARTICLE_PATH = re.compile(r"^/sj/zxfbhjd/\d{6}/t\d{8}_\d+\.html$")
_REPORT_TITLE = re.compile(r"^\d{4}年.+流通领域重要生产资料市场价格变动情况$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NbsCommodityEvidenceError(RuntimeError):
    """The official index, article or cached raw response was invalid."""


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._title = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        self._href = values.get("href")
        self._title = values.get("title", "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = " ".join(" ".join(self._text).split())
        self.links.append((self._href, self._title or text))
        self._href = None
        self._title = ""
        self._text = []


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if lowered == "meta":
            name = values.get("name", "")
            if name:
                self.meta[name] = values.get("content", "")
        elif lowered == "tr":
            self._row = []
        elif lowered in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(" ".join(self._cell).split()))
            self._cell = None
        elif lowered == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _parse_as_of(value: Any) -> date:
    if not isinstance(value, str) or _CANONICAL_DATE.fullmatch(value) is None:
        raise ValueError("NBS commodity as_of must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("NBS commodity as_of must be a valid calendar date") from exc
    if parsed > shanghai_today():
        raise ValueError("NBS commodity as_of cannot be in the future")
    return parsed


def _strict_https_url(value: Any, *, article: bool) -> str:
    url = str(value or "")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise NbsCommodityEvidenceError("NBS source URL is invalid") from exc
    valid_path = bool(_ARTICLE_PATH.fullmatch(parsed.path)) if article else parsed.path == "/sj/zxfbhjd/"
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "www.stats.gov.cn"
        or port not in {None, 443}
        or not valid_path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise NbsCommodityEvidenceError("NBS source redirected outside the official endpoint")
    return url


def _decode(raw: bytes, *, label: str) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NbsCommodityEvidenceError(f"{label} is not UTF-8") from exc


def _number(value: str, *, field: str) -> float:
    try:
        result = float(value.replace(",", "").strip())
    except (TypeError, ValueError, OverflowError) as exc:
        raise NbsCommodityEvidenceError(f"NBS article has invalid {field}") from exc
    if not math.isfinite(result):
        raise NbsCommodityEvidenceError(f"NBS article has invalid {field}")
    return result


def _index_articles(raw: bytes) -> list[str]:
    parser = _IndexParser()
    parser.feed(_decode(raw, label="NBS index"))
    result: list[str] = []
    seen: set[str] = set()
    for href, title in parser.links:
        normalized_title = "".join(str(title).split())
        if _REPORT_TITLE.fullmatch(normalized_title) is None:
            continue
        url = _strict_https_url(urljoin(NBS_INDEX_URL, href), article=True)
        if url not in seen:
            seen.add(url)
            result.append(url)
    if not result:
        raise NbsCommodityEvidenceError("NBS index contains no production-price reports")
    return result


def _parse_article(raw: bytes, url: str, cutoff: date) -> dict[str, dict[str, Any]]:
    parser = _ArticleParser()
    parser.feed(_decode(raw, label="NBS article"))
    title = "".join(parser.meta.get("ArticleTitle", "").split())
    if _REPORT_TITLE.fullmatch(title) is None:
        raise NbsCommodityEvidenceError("NBS article title contract changed")
    try:
        published_at = datetime.strptime(parser.meta.get("PubDate", ""), "%Y/%m/%d %H:%M")
    except ValueError as exc:
        raise NbsCommodityEvidenceError("NBS article publication date is invalid") from exc
    if published_at.date() > cutoff:
        raise NbsCommodityEvidenceError("NBS article was published after the analysis cutoff")

    source_sha256 = hashlib.sha256(raw).hexdigest()
    result: dict[str, dict[str, Any]] = {}
    for industry, keywords in INDUSTRY_PRODUCT_KEYWORDS.items():
        matches: list[tuple[str, str, float, float, float]] = []
        for row in parser.rows:
            if len(row) < 5 or not any(keyword in row[0] for keyword in keywords):
                continue
            matches.append(
                (
                    row[0],
                    row[1],
                    _number(row[2], field="price"),
                    _number(row[3], field="price change"),
                    _number(row[4].replace("%", ""), field="percentage change"),
                )
            )
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) != 1:
            raise NbsCommodityEvidenceError(f"NBS article product identity is missing or conflicting: {industry}")
        product_name, unit, price, change, change_pct = unique_matches[0]
        if price <= 0 or not unit:
            raise NbsCommodityEvidenceError(f"NBS article product value is invalid: {industry}")
        result[industry] = {
            "source": NBS_SOURCE_NAME,
            "source_url": url,
            "source_sha256": source_sha256,
            "published_at": published_at.replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat(),
            "period_title": title.removesuffix("流通领域重要生产资料市场价格变动情况"),
            "product_name": product_name,
            "unit": unit,
            "current_price_yuan": price,
            "change_yuan": change,
            "change_pct": change_pct,
            "as_of": cutoff.isoformat(),
            "score_effect": "context_only",
        }
    return result


def _cache_path(cutoff: date, directory: Path) -> Path:
    return directory / f"{NBS_CACHE_MODEL_ID}_{cutoff.strftime('%Y%m%d')}.json.gz"


def _replay_cache(value: Any, cutoff: date) -> dict[str, dict[str, Any]] | None:
    expected_fields = {
        "model_id",
        "as_of",
        "index_url",
        "article_url",
        "index_raw_base64",
        "article_raw_base64",
        "index_sha256",
        "article_sha256",
        "captured_at_utc",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        return None
    if value.get("model_id") != NBS_CACHE_MODEL_ID or value.get("as_of") != cutoff.isoformat():
        return None
    if value.get("index_url") != NBS_INDEX_URL:
        return None
    article_url = _strict_https_url(value.get("article_url"), article=True)
    try:
        index_raw = base64.b64decode(str(value.get("index_raw_base64") or ""), validate=True)
        article_raw = base64.b64decode(str(value.get("article_raw_base64") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise NbsCommodityEvidenceError("NBS raw cache is not valid base64") from exc
    index_sha = hashlib.sha256(index_raw).hexdigest()
    article_sha = hashlib.sha256(article_raw).hexdigest()
    if (
        _SHA256.fullmatch(str(value.get("index_sha256") or "")) is None
        or _SHA256.fullmatch(str(value.get("article_sha256") or "")) is None
        or index_sha != value.get("index_sha256")
        or article_sha != value.get("article_sha256")
        or article_url not in _index_articles(index_raw)
    ):
        return None
    return _parse_article(article_raw, article_url, cutoff)


def _get_raw(session: Any, url: str, *, limit: int, article: bool) -> bytes:
    response = session.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
        timeout=NBS_TIMEOUT,
        stream=True,
    )
    try:
        response.raise_for_status()
        _strict_https_url(getattr(response, "url", ""), article=article)
        return read_bounded_response_bytes(response, limit)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def load_nbs_commodity_context(
    *,
    as_of: str,
    cache_dir: str | Path = NBS_CACHE_DIR,
    session: Any = requests,
    cache_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load the latest official report published no later than ``as_of``.

    ``cache_only`` replays a validated raw-response cache or returns no context;
    it never requests the NBS index or an article.
    """

    if not isinstance(cache_only, bool):
        raise TypeError("cache_only must be boolean")
    cutoff = _parse_as_of(as_of)
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cache = SafeFileCache(
        _cache_path(cutoff, directory),
        schema_version=NBS_CACHE_SCHEMA_VERSION,
        ttl=NBS_CACHE_TTL_SECONDS,
        max_uncompressed_bytes=2 * (NBS_MAX_RESPONSE_BYTES + NBS_MAX_ARTICLE_BYTES),
    )
    loaded = cache.load(allow_expired=cache_only or (shanghai_today() - cutoff).days > 2)
    if loaded.hit:
        replayed = _replay_cache(loaded.value, cutoff)
        if replayed is not None:
            return replayed

    if cache_only:
        return {}

    if session is requests:
        session = thread_local_session()
    index_raw = _get_raw(session, NBS_INDEX_URL, limit=NBS_MAX_RESPONSE_BYTES, article=False)
    articles = _index_articles(index_raw)
    selected_url = ""
    selected_raw = b""
    selected_context: dict[str, dict[str, Any]] | None = None
    for article_url in articles[:NBS_MAX_ARTICLE_PROBES]:
        raw = _get_raw(session, article_url, limit=NBS_MAX_ARTICLE_BYTES, article=True)
        try:
            context = _parse_article(raw, article_url, cutoff)
        except NbsCommodityEvidenceError as exc:
            if "published after" in str(exc):
                continue
            raise
        selected_url = article_url
        selected_raw = raw
        selected_context = context
        break
    if selected_context is None:
        raise NbsCommodityEvidenceError("NBS index has no report published by the analysis cutoff")

    cache.save(
        {
            "model_id": NBS_CACHE_MODEL_ID,
            "as_of": cutoff.isoformat(),
            "index_url": NBS_INDEX_URL,
            "article_url": selected_url,
            "index_raw_base64": base64.b64encode(index_raw).decode("ascii"),
            "article_raw_base64": base64.b64encode(selected_raw).decode("ascii"),
            "index_sha256": hashlib.sha256(index_raw).hexdigest(),
            "article_sha256": hashlib.sha256(selected_raw).hexdigest(),
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    return selected_context


__all__ = [
    "INDUSTRY_PRODUCT_KEYWORDS",
    "NBS_CACHE_DIR",
    "NBS_CACHE_MODEL_ID",
    "NBS_INDEX_URL",
    "NBS_SOURCE_NAME",
    "NbsCommodityEvidenceError",
    "load_nbs_commodity_context",
]
