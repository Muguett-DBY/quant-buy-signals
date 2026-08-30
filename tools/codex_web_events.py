"""Verify Codex CLI web-search events and bind them to researched companies.

Research JSONL is model output, so its ``search_queries`` field is not proof
that a search actually ran.  This module reads the independent ``codex exec
--json`` event stream, accepts only a successfully completed turn, and binds
completed ``web_search`` actions to the company code or full company name.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


_MAX_EVENT_LOG_BYTES = 128 * 1024 * 1024
_MAX_EVENT_LOG_LINES = 100_000
_SPACE_RE = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass(frozen=True)
class CodexWebEvent:
    event_id: str
    queries: tuple[str, ...]
    log_sha256: str
    thread_id: str


@dataclass(frozen=True)
class CompanyWebEvidence:
    queries: tuple[str, ...]
    event_ids: tuple[str, ...]
    log_sha256s: tuple[str, ...]
    thread_ids: tuple[str, ...]


def _clean_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _item_id(item: Mapping[str, Any]) -> str:
    value = _clean_text(item.get("id"), 160)
    if not value:
        raise ValueError("Codex web-search event has no item id")
    return value


def _search_queries(item: Mapping[str, Any]) -> tuple[str, ...]:
    action = item.get("action")
    if not isinstance(action, Mapping) or action.get("type") != "search":
        return ()
    raw_queries = action.get("queries")
    values = raw_queries if isinstance(raw_queries, list) else [item.get("query")]
    queries = tuple(dict.fromkeys(text for value in values if (text := _clean_text(value))))
    if not queries:
        raise ValueError("completed Codex search event has no query")
    return queries


def parse_codex_web_event_log(path: Path) -> list[CodexWebEvent]:
    """Return completed searches from one successful, immutable CLI event log."""

    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_EVENT_LOG_BYTES:
        raise ValueError(f"Codex event log has invalid size: {path}")
    digest = hashlib.sha256(raw).hexdigest()
    thread_ids: list[str] = []
    turn_completed = 0
    failed = False
    started_ids: set[str] = set()
    completed_ids: set[str] = set()
    searches: list[tuple[str, tuple[str, ...]]] = []
    json_lines = 0

    for line_number, raw_line in enumerate(raw.decode("utf-8-sig").splitlines(), start=1):
        if line_number > _MAX_EVENT_LOG_LINES:
            raise ValueError(f"Codex event log has too many lines: {path}")
        line = raw_line.strip()
        # Codex can emit diagnostic warnings beside the JSONL stream.  Ignore
        # those human-readable lines, but never ignore a malformed JSON line.
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid Codex event JSON at {path}:{line_number}") from exc
        if not isinstance(event, Mapping):
            raise ValueError(f"Codex event is not an object at {path}:{line_number}")
        json_lines += 1
        event_type = _clean_text(event.get("type"), 80)
        if event_type == "thread.started":
            thread_id = _clean_text(event.get("thread_id"), 160)
            if not thread_id:
                raise ValueError(f"Codex thread event has no id: {path}")
            thread_ids.append(thread_id)
        elif event_type == "turn.completed":
            turn_completed += 1
        elif event_type in {"turn.failed", "error"}:
            failed = True
        elif event_type in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, Mapping) or item.get("type") != "web_search":
                continue
            event_id = _item_id(item)
            target = started_ids if event_type == "item.started" else completed_ids
            if event_id in target:
                raise ValueError(f"duplicate Codex {event_type} id {event_id}: {path}")
            target.add(event_id)
            if event_type == "item.completed":
                queries = _search_queries(item)
                if queries:
                    searches.append((event_id, queries))

    if not json_lines:
        raise ValueError(f"Codex event log has no JSON events: {path}")
    if failed or turn_completed != 1:
        raise ValueError(f"Codex event log did not complete successfully: {path}")
    if len(thread_ids) != 1:
        raise ValueError(f"Codex event log must contain exactly one thread: {path}")
    if started_ids != completed_ids:
        raise ValueError(f"Codex web-search event pairs are incomplete: {path}")
    if not searches:
        raise ValueError(f"Codex event log contains no completed search: {path}")
    thread_id = thread_ids[0]
    return [
        CodexWebEvent(
            event_id=event_id,
            queries=queries,
            log_sha256=digest,
            thread_id=thread_id,
        )
        for event_id, queries in searches
    ]


def load_codex_web_events(paths: Iterable[Path]) -> list[CodexWebEvent]:
    resolved: set[Path] = set()
    digests: set[str] = set()
    events: list[CodexWebEvent] = []
    for path in paths:
        absolute = path.resolve()
        if absolute in resolved:
            raise ValueError(f"duplicate Codex event-log path: {path}")
        resolved.add(absolute)
        parsed = parse_codex_web_event_log(absolute)
        digest = parsed[0].log_sha256
        if digest in digests:
            raise ValueError(f"duplicate Codex event-log content: {path}")
        digests.add(digest)
        events.extend(parsed)
    if not events:
        raise ValueError("at least one successful Codex --json event log is required")
    return events


def _normalized_identity(value: Any) -> str:
    return _SPACE_RE.sub("", unicodedata.normalize("NFKC", _clean_text(value, 200))).casefold()


def bind_codex_web_events(
    events: Iterable[CodexWebEvent], packets: Iterable[Mapping[str, Any]]
) -> dict[str, CompanyWebEvidence]:
    """Require at least one real search query for every queued company."""

    event_list = list(events)
    bound: dict[str, CompanyWebEvidence] = {}
    for packet in packets:
        code = _clean_text(packet.get("security_code"), 16)
        name = _normalized_identity(packet.get("name"))
        if not code or not name or code in bound:
            raise ValueError(f"invalid or duplicate candidate identity for Codex event binding: {code}")
        code_pattern = re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")
        name_pattern = re.compile(
            r"(?<![A-Za-z0-9\u3400-\u9fff])"
            + r"\s*".join(re.escape(char) for char in name)
            + r"(?![A-Za-z\u3400-\u9fff])",
            re.IGNORECASE,
        )
        matched: list[tuple[CodexWebEvent, str]] = []
        for event in event_list:
            for query in event.queries:
                normalized_query = unicodedata.normalize("NFKC", query)
                if code_pattern.search(normalized_query) or name_pattern.search(normalized_query):
                    matched.append((event, query))
        if not matched:
            raise ValueError(f"no completed Codex web-search event matches company {code} {packet.get('name')}")
        bound[code] = CompanyWebEvidence(
            queries=tuple(dict.fromkeys(query for _, query in matched)),
            event_ids=tuple(dict.fromkeys(event.event_id for event, _ in matched)),
            log_sha256s=tuple(dict.fromkeys(event.log_sha256 for event, _ in matched)),
            thread_ids=tuple(dict.fromkeys(event.thread_id for event, _ in matched)),
        )
    return bound
