from __future__ import annotations

import json

from data import nbs_commodity_evidence as nbs
from data.cache import SafeFileCache
from data.nbs_commodity_evidence import (
    NBS_CACHE_MODEL_ID,
    NBS_CACHE_SCHEMA_VERSION,
    NBS_INDEX_URL,
    load_nbs_commodity_context,
)


ARTICLE_URL = "https://www.stats.gov.cn/sj/zxfbhjd/202608/t20260821_1965093.html"


def _index() -> bytes:
    return (
        '<html><a href="./202608/t20260821_1965093.html" '
        'title="2026年8月中旬流通领域重要生产资料市场价格变动情况">报告</a></html>'
    ).encode()


def _article() -> bytes:
    rows = [
        ("螺纹钢（Φ20mm，HRB400E）", "吨", "3062.9", "0.2", "0.0"),
        ("电解铜（1#）", "吨", "108236.3", "738.0", "0.7"),
        ("甲醇（优等品）", "吨", "2635.6", "156.2", "6.3"),
        ("普通硅酸盐水泥（P.O 42.5散装）", "吨", "249.1", "2.3", "0.9"),
        ("液化天然气（LNG）", "吨", "5572.8", "33.3", "0.6"),
        ("焦煤（主焦煤）", "吨", "2043.1", "134.8", "7.1"),
    ]
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return (
        '<html><head><meta name="ArticleTitle" content="2026年8月中旬流通领域重要生产资料市场价格变动情况">'
        '<meta name="PubDate" content="2026/08/24 09:30"></head><body><table>' + body + "</table></body></html>"
    ).encode()


class _Response:
    def __init__(self, url: str, raw: bytes) -> None:
        self.url = url
        self._raw = raw
        self.headers = {"Content-Length": str(len(raw))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield from (self._raw[offset : offset + chunk_size] for offset in range(0, len(self._raw), chunk_size))

    def close(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        if url == NBS_INDEX_URL:
            return _Response(url, _index())
        if url == ARTICLE_URL:
            return _Response(url, _article())
        raise AssertionError(url)


def test_nbs_context_is_official_current_context_without_score_effect(tmp_path):
    session = _Session()
    result = load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=session)

    assert set(result) == {"STEEL", "NONFERROUS", "CHEMICAL", "BUILDING_MATERIAL", "OIL_GAS", "COAL"}
    assert result["STEEL"]["current_price_yuan"] == 3062.9
    assert result["COAL"]["change_pct"] == 7.1
    assert result["STEEL"]["score_effect"] == "context_only"
    assert result["STEEL"]["source_url"] == ARTICLE_URL
    assert result["STEEL"]["published_at"] == "2026-08-24T09:30:00+08:00"
    assert session.calls == [NBS_INDEX_URL, ARTICLE_URL]


def test_nbs_cache_replays_raw_article_and_rejects_tampering(tmp_path):
    first = _Session()
    expected = load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=first)
    no_network = _Session()
    assert load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=no_network) == expected
    assert no_network.calls == []

    path = next(tmp_path.glob(f"{NBS_CACHE_MODEL_ID}_*.json.gz"))
    cache = SafeFileCache(path, schema_version=NBS_CACHE_SCHEMA_VERSION, ttl=18 * 3600)
    poisoned = dict(cache.load().value)
    poisoned["article_sha256"] = "0" * 64
    cache.save(poisoned)
    replacement = _Session()
    load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=replacement)
    assert replacement.calls == [NBS_INDEX_URL, ARTICLE_URL]


def test_nbs_cache_payload_contains_raw_responses_not_normalized_rows(tmp_path):
    load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=_Session())
    path = next(tmp_path.glob("*.json.gz"))
    payload = SafeFileCache(path, schema_version=NBS_CACHE_SCHEMA_VERSION, ttl=18 * 3600).load().value

    assert "index_raw_base64" in payload
    assert "article_raw_base64" in payload
    assert "rows" not in json.dumps(payload, ensure_ascii=False)


def test_nbs_cache_only_miss_never_fetches(tmp_path):
    session = _Session()

    assert (
        load_nbs_commodity_context(
            as_of="2026-08-28",
            cache_dir=tmp_path,
            session=session,
            cache_only=True,
        )
        == {}
    )
    assert session.calls == []


def test_nbs_cache_only_replays_expired_valid_raw_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(nbs, "NBS_CACHE_TTL_SECONDS", 0)
    expected = load_nbs_commodity_context(as_of="2026-08-28", cache_dir=tmp_path, session=_Session())
    offline = _Session()

    replayed = load_nbs_commodity_context(
        as_of="2026-08-28",
        cache_dir=tmp_path,
        session=offline,
        cache_only=True,
    )

    assert replayed == expected
    assert offline.calls == []
