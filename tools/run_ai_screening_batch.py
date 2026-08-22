"""Run OpenCode Go reviews in a small number of cache-friendly batches.

The deterministic candidate packet is still read-only.  This command groups
packets by a shared rule context, asks Reasonix or the OpenCode CLI for a JSON array, and writes
one validated JSONL review file.  A batch is intentionally bounded so a
single malformed model response can be retried without losing the full run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from tools.ai_screening_contract import decision_text_conflicts, normalise_decision_text, validate_review


_REVIEW_KEYS = frozenset(
    {
        "schema_version",
        "security_code",
        "type_key",
        "verdict",
        "recommended_action",
        "buy_attractiveness_score",
        "ai_action",
        "confidence",
    }
)
_WEB_URL_RE = re.compile(r"https?://[^\s<>\"'）)\]]+", re.IGNORECASE)
_ASCII_URL_PREFIX_RE = re.compile(r"[A-Za-z0-9:/?#\[\]@!$&'()*+,;=%._~\-]+")
_RULE_EXCERPT_LIMIT = 1400
_RULES_PER_TYPE_LIMIT = 3


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _review_array(value: Any) -> list[dict[str, Any]] | None:
    """Return only arrays that look like the final review payload.

    Reasonix text mode can include JSON from intermediate tool messages.  A
    generic list-of-objects parser may mistake one of those lists for the
    final answer, so require the stable review identity/decision keys before
    accepting a candidate array.
    """
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
        return None
    if not all(_REVIEW_KEYS.issubset(item) for item in value):
        return None
    return value


def _extract_array(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    # OpenCode occasionally serialises the final JSON as a JSON string inside
    # its text event (escaped quotes and brackets).  Unwrap that one layer
    # before scanning code fences/bracket fragments; this does not accept any
    # new schema, it only normalises the transport representation.
    try:
        wrapped = json.loads(cleaned)
    except json.JSONDecodeError:
        wrapped = None
    if isinstance(wrapped, str) and wrapped.strip() != cleaned:
        try:
            return _extract_array(wrapped)
        except ValueError:
            pass
    # Some OpenCode transports leave one escaped JSON layer in the assistant
    # text rather than returning a valid JSON string.  Remove only that
    # transport escaping; the review schema is still checked below.
    if '\\"' in cleaned:
        unescaped = cleaned.replace('\\"', '"').replace("\\n", "\n")
        if unescaped != cleaned:
            try:
                return _extract_array(unescaped)
            except ValueError:
                pass
    for block in re.findall(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL):
        try:
            value = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        candidate = _review_array(value)
        if candidate is not None:
            return candidate
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for start, char in enumerate(cleaned):
        if char != "[":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        candidate = _review_array(value)
        if candidate is not None:
            return candidate
    for start, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("reviews"), list):
            reviews = value["reviews"]
            candidate = _review_array(reviews)
            if candidate is not None:
                return candidate
    raise ValueError("Reasonix did not return a JSON review array")


def _extract_opencode_text(text: str) -> str:
    """Extract final assistant text from OpenCode JSON event output."""
    parts: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "text":
            continue
        part = event.get("part")
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    if not parts:
        raise ValueError("OpenCode did not return a final assistant text event")
    return "\n".join(parts)


def _completed_opencode_websearch_events(text: str) -> list[dict[str, Any]]:
    """Read completed searches and returned URLs from OpenCode's event stream."""

    searches: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "tool_use":
            continue
        part = event.get("part")
        if not isinstance(part, Mapping) or str(part.get("tool") or "") not in {"websearch", "web_search"}:
            continue
        state = part.get("state")
        if not isinstance(state, Mapping) or state.get("status") != "completed":
            continue
        tool_input = state.get("input")
        if not isinstance(tool_input, Mapping):
            continue
        queries: list[str] = []
        raw_queries = tool_input.get("queries")
        if isinstance(raw_queries, list):
            queries.extend(str(value).strip() for value in raw_queries if str(value).strip())
        query = str(tool_input.get("query") or "").strip()
        if query:
            queries.append(query)
        output = state.get("output")
        output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, sort_keys=True)
        urls = list(dict.fromkeys(match.group(0).rstrip(".,;，。；") for match in _WEB_URL_RE.finditer(output_text)))
        searches.append({"queries": queries, "urls": urls})
    return searches


def _completed_opencode_websearch_queries(text: str) -> list[str]:
    """Read completed web-search queries from OpenCode's JSON event stream."""

    return [query for search in _completed_opencode_websearch_events(text) for query in search["queries"]]


def _canonical_web_url(value: Any) -> str:
    match = _WEB_URL_RE.search(str(value or ""))
    if not match:
        return ""
    ascii_match = _ASCII_URL_PREFIX_RE.match(match.group(0))
    if not ascii_match:
        return ""
    parsed = urlsplit(ascii_match.group(0).rstrip(".,;"))
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))


def _review_websearch_evidence(
    searches: list[Mapping[str, Any]],
    *,
    code: str,
    companies: Mapping[str, str],
    claims: Any,
) -> dict[str, Any]:
    matched = [search for search in searches if _search_company_matches(search, companies) == {code}]
    queries = list(dict.fromkeys(query for search in matched for query in search["queries"]))
    returned_urls = {
        canonical for search in matched for value in search["urls"] if (canonical := _canonical_web_url(value))
    }
    claim_rows = claims if isinstance(claims, list) else []
    claim_urls = [
        canonical
        for claim in claim_rows
        if isinstance(claim, Mapping)
        if (canonical := _canonical_web_url(claim.get("source_ref") or claim.get("source_context")))
    ]
    missing_claim_urls = sorted(set(claim_urls) - returned_urls)
    return {
        "queries": queries,
        "claim_urls": list(dict.fromkeys(claim_urls)),
        "verified_claim_urls": list(dict.fromkeys(url for url in claim_urls if url in returned_urls)),
        "missing_claim_urls": missing_claim_urls,
    }


def _search_company_matches(search: Mapping[str, Any], companies: Mapping[str, str]) -> set[str]:
    """Return companies uniquely named by one completed web-search event."""

    matches: set[str] = set()
    queries = search.get("queries")
    if not isinstance(queries, list):
        return matches
    for raw_query in queries:
        query = str(raw_query).casefold()
        for code, name in companies.items():
            if code.casefold() in query or (name and name.casefold() in query):
                matches.add(code)
    return matches


def _opencode_session_id(text: str) -> str:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping) and str(event.get("sessionID") or "").startswith("ses_"):
            return str(event["sessionID"])
    return ""


def _missing_websearch_companies(text: str, packets: list[Mapping[str, Any]]) -> list[str]:
    """Return company codes not covered by an actual completed search call."""

    companies: dict[str, str] = {}
    for packet in packets:
        code = str(packet.get("security_code") or "").strip()
        if code:
            companies.setdefault(code, str(packet.get("name") or "").strip())
    searches = _completed_opencode_websearch_events(text)
    covered = {
        next(iter(matches)) for search in searches if len(matches := _search_company_matches(search, companies)) == 1
    }
    return [code for code in companies if code not in covered]


def _batch_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Drop duplicate rule prose; the batch carries it once per type."""
    return {key: value for key, value in packet.items() if key != "rule_context"}


def _company_batches(packets: list[dict[str, Any]], maximum: int) -> list[tuple[int, list[dict[str, Any]]]]:
    """Build bounded company-local chunks without cross-wiring type pairs."""

    batches: list[tuple[int, list[dict[str, Any]]]] = []
    start = 0
    current: list[dict[str, Any]] = []
    index = 0
    while index < len(packets):
        code = str(packets[index].get("security_code") or "")
        end = index + 1
        while end < len(packets) and str(packets[end].get("security_code") or "") == code:
            end += 1
        company = packets[index:end]
        for chunk_start in range(0, len(company), maximum):
            chunk = company[chunk_start : chunk_start + maximum]
            absolute_start = index + chunk_start
            if current and len(current) + len(chunk) > maximum:
                batches.append((start, current))
                start = absolute_start
                current = []
            current.extend(chunk)
        index = end
    if current:
        batches.append((start, current))
    return batches


def _normalise_model_review(review: dict[str, Any]) -> dict[str, Any]:
    """Drop source-less claim shells without inventing provenance.

    The protocol permits an empty claims list when a search found no reliable
    source.  MiMo occasionally emits an otherwise empty claim object in that
    case; removing only that object preserves the review and lets publication
    expose the lower source confidence instead of losing a whole batch.
    """
    for field in ("key_strengths", "risk_flags"):
        value = review.get(field)
        if isinstance(value, str):
            review[field] = [value] if value.strip() else []
    claims = review.get("claims")
    if isinstance(claims, Mapping):
        claims = [claims]
    if isinstance(claims, list):
        review["claims"] = [
            claim
            for claim in claims
            if isinstance(claim, Mapping) and (claim.get("source_ref") or claim.get("source_context"))
        ]
    elif claims is not None:
        review["claims"] = []
    # MiMo sometimes uses the action name as the verdict.  Keep the
    # distinction in the public contract: ``ai_action`` carries the
    # insufficient-evidence state, while ``verdict`` uses the review enum.
    if review.get("verdict") == "insufficient_evidence":
        review["verdict"] = "needs_review"
    return review


def _cohere_local_review(review: dict[str, Any]) -> dict[str, Any]:
    """Keep an offline model response inside the public decision contract.

    Local Ox batches are opinions without live evidence.  The model can still
    emit a contradictory tuple (for example ``watchlist`` with
    ``recommended_action=keep``) or a 100-point ``avoid`` score.  Correct the
    mechanical tuple/score boundary here while leaving the explanatory text
    intact except for an unqualified buy phrase that would contradict a
    non-buy action.
    """
    action = str(review.get("ai_action") or "watchlist")
    tuple_values = {
        "priority_buy": ("confirmed", "keep"),
        "watchlist": ("caution", "manual_review"),
        "avoid": ("misclassified", "demote"),
        "insufficient_evidence": ("needs_review", "manual_review"),
    }
    if action not in tuple_values:
        action = "watchlist"
        review["ai_action"] = action
    verdict, recommended_action = tuple_values[action]
    review["verdict"] = verdict
    review["recommended_action"] = recommended_action
    score = _finite_float(review.get("buy_attractiveness_score"))
    if score is not None:
        if action == "priority_buy":
            review["buy_attractiveness_score"] = max(60.0, score)
        elif action == "watchlist":
            review["buy_attractiveness_score"] = min(69.0, score)
        else:
            review["buy_attractiveness_score"] = min(49.0, score)
    if action != "priority_buy" and isinstance(review.get("summary"), str):
        summary = normalise_decision_text(review["summary"])
        if action == "watchlist":
            summary = summary.replace("当前结论：不建议买", "当前结论：观察（暂不建议买）")
        if decision_text_conflicts(action, summary):
            for old, new in (
                ("当前建议买入", "当前不建议买入"),
                ("建议立即买入", "暂不建议立即买入"),
                ("建议买入", "不建议买入"),
                ("建议买", "不建议买"),
                ("值得买入", "暂不建议买入"),
                ("值得买", "暂不建议买"),
                ("可以买", "暂不构成买点"),
                ("可买", "暂不可买"),
            ):
                summary = summary.replace(old, new)
        review["summary"] = normalise_decision_text(summary)
    return review


def _shared_rules(packets: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for packet in packets:
        type_key = str(packet.get("type_key") or "")
        for rule in packet.get("rule_context", []):
            if not isinstance(rule, Mapping):
                continue
            key = "|".join(str(rule.get(field) or "") for field in ("source_id", "line_start", "heading"))
            if key in grouped[type_key]:
                continue
            compact = dict(rule)
            text = str(compact.get("text") or "")
            if len(text) > _RULE_EXCERPT_LIMIT:
                compact["text"] = (
                    text[:950] + "\n...[knowledge-base excerpt shortened for this batch]...\n" + text[-350:]
                )
            grouped[type_key][key] = compact
    return {type_key: list(values.values())[:_RULES_PER_TYPE_LIMIT] for type_key, values in sorted(grouped.items())}


def _prompt(protocol: str, packets: list[Mapping[str, Any]], *, require_web_search: bool = False) -> str:
    shared = _shared_rules(packets)
    slim = [_batch_packet(packet) for packet in packets]
    search_instruction = ""
    if require_web_search:
        search_targets = []
        seen_codes: set[str] = set()
        for packet in packets:
            code = str(packet.get("security_code") or "").strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            name = str(packet.get("name") or "").strip()
            search_targets.append(
                {
                    "security_code": code,
                    "name": name,
                    "exact_query": f"{code} {name} 2026 最新公告 财报".strip(),
                }
            )
        search_instruction = (
            "\nMANDATORY WEB SEARCH: Before writing each review, call the available websearch/web_search tool "
            "for every distinct company in the explicit target list below. Make one separate tool call per "
            "distinct security_code; do not combine two companies in one query and do not skip a target. "
            "For each target, use its exact_query (or a minimally expanded query that still contains the exact "
            "security_code) as the first search query. Search the company code and name against CNINFO, SSE/SZSE, HKEX when "
            "applicable, and the company's investor-relations or official filing page. Use only URLs "
            "actually returned by the search tool. Set web_search_performed=true only after searching; "
            "set web_search_performed=true immediately after the search attempt, even when no reliable "
            "source is returned; set confidence=low in that case. HTTPS is preferred for source quality "
            "but is not a hard gate for an AI opinion when a returned official HTTP source is identifiable. "
            "The packet's market_as_of is the cutoff: "
            "report periods 2025/2026 are current/recent for this snapshot, while 2024 or earlier "
            "must be called historical and cannot justify priority_buy. Do not treat a URL publication "
            "date, forecast/target year, or stock code as an actual report period. Deterministic status "
            "is context, not a hard gate: if current evidence supports buying a near-qualified candidate, "
            "return priority_buy and explain the independent AI judgment.\n"
            "Explicit search targets (one completed search event for each code is required):\n"
            + json.dumps(search_targets, ensure_ascii=False, sort_keys=True)
            + "\n"
        )
    else:
        search_instruction = (
            "\nLOCAL REVIEW MODE: Do not call or claim any web-search tool. Use only the deterministic packet "
            "and the supplied knowledge-base excerpts. Do not invent current prices, report figures, dates, "
            "or source URLs. Keep claims as an empty array, set confidence=low, and state that this is a "
            "local model opinion without live-search evidence. Choose exactly one current conclusion: "
            "priority_buy means the summary may say 建议买 but never 观察 or 不建议; watchlist and "
            "insufficient_evidence mean 观察 and never an unqualified 建议买; avoid means 不建议 and "
            "never 观察 as the current conclusion. Conditional future language is allowed only after "
            "the one current conclusion, and do not put another action label in the same sentence.\n"
        )
    execution_instruction = (
        "Do not describe a plan or ask for permission: execute the available websearch tool now, then return the JSON array."
        if require_web_search
        else "Do not call a web-search tool in this local batch; return the JSON array from the supplied packet and rules."
    )
    packet_search_instruction = (
        "For every packet, actually search the company code/name and prefer a returned official source."
        if require_web_search
        else "For every packet, use only the supplied deterministic fields and knowledge-base excerpts; do not add live facts."
    )
    batch_search_instruction = (
        "When one company has several type packets in this batch, search that company once and reuse the "
        "same current factual evidence, but still write a separate type-specific review for every packet."
        if require_web_search
        else "When one company has several type packets in this batch, write a separate type-specific local review for every packet."
    )
    active_protocol = protocol
    if not require_web_search:
        # The shared protocol is written for live research and contains a
        # mandatory-search section.  Keep its output schema, but remove that
        # research preamble in local mode so the model cannot be instructed to
        # search and not search in the same prompt.
        output_contract = protocol.find("## 输出契约")
        if output_contract >= 0:
            active_protocol = protocol[output_contract:]
    return (
        active_protocol
        + search_instruction
        + "\n\nBatch review instructions:\n"
        + "Review every packet independently. The output array must contain exactly one object "
        + "for every packet, preserving security_code and type_key. Do not omit a packet.\n"
        + batch_search_instruction
        + "\n"
        + "The rule_context fragments come from the local 模板汇总MD knowledge base; use them as the "
        + "authoritative interpretation of the seven-type rules, never as company facts.\n"
        + "Shared rule fragments are keyed by type_key; use the matching fragments when useful:\n"
        + json.dumps(shared, ensure_ascii=False, sort_keys=True)
        + "\nPackets (JSON array):\n"
        + json.dumps(slim, ensure_ascii=False, sort_keys=True)
        + "\nIMPORTANT: for this batch, override the protocol's single-packet output example: "
        + "return exactly one JSON array containing one review object per packet, and no Markdown. "
        + "Use only these exact enums: verdict=confirmed|caution|misclassified|missed_candidate|needs_review; "
        + "recommended_action=keep|demote|manual_review; ai_action=priority_buy|watchlist|avoid|insufficient_evidence. "
        + "Keep the decision tuple coherent: priority_buy uses confirmed+keep; misclassified uses avoid+demote; "
        + "needs_review uses watchlist/insufficient_evidence+manual_review. "
        + execution_instruction
        + " "
        + packet_search_instruction
        + " "
        + "If a usable source is returned, put its exact URL in claims[].source_ref; never invent or rewrite a URL. "
        + "key_strengths, risk_flags and claims must always be JSON arrays, even when they contain one item. "
        + "Make the decision language internally consistent: begin summary with the current conclusion (建议买/观察/不建议), "
        + "do not use another action's label or enum as the current conclusion, and do not mention priority_buy/watchlist/avoid "
        + "as a hypothetical label inside a different action's summary. "
        + "Keep every object compact: summary <= 280 Chinese characters, at most 2 strengths, 3 risks, "
        + "and 3 source claims; do not repeat facts."
    )


def _reasonix_path() -> str:
    reasonix = shutil.which("reasonix.cmd") or shutil.which("reasonix")
    if reasonix:
        return reasonix
    appdata = os.environ.get("APPDATA", "")
    candidate = Path(appdata) / "npm" / "reasonix.cmd"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("reasonix.cmd is not on PATH")


def _opencode_path() -> str:
    opencode = shutil.which("opencode.cmd") or shutil.which("opencode")
    if opencode:
        return opencode
    appdata = os.environ.get("APPDATA", "")
    candidate = Path(appdata) / "npm" / "opencode.cmd"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("opencode.cmd is not on PATH")


def _run_batch(
    packets: list[dict[str, Any]],
    *,
    protocol: str,
    model: str,
    effort: str,
    preset: str,
    max_steps: int,
    permission_mode: str,
    allowed_tools: str,
    ablate: str,
    require_web_search: bool,
    root: Path,
    reasonix_dir: Path,
    backend: str = "reasonix",
    session_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if backend == "opencode":
        command = [
            _opencode_path(),
            "run",
            "--model",
            model,
            "--variant",
            effort,
            "--format",
            "json",
            "--auto",
            "--title",
            f"ai-screening-{packets[0].get('market_as_of', 'batch')}-{packets[0].get('security_code', 'batch')}",
        ]
        if session_id:
            command.extend(["--session", session_id])
        env = os.environ.copy()
        env["OPENCODE_ENABLE_EXA"] = "1"
        # The desktop environment enables OpenCode's parallel search path by
        # default.  That path routes through a rate-limited Parallel endpoint;
        # the direct Exa tool is the supported, stable path for this batch.
        env["OPENCODE_ENABLE_PARALLEL"] = "0"
        env["OPENCODE_EXPERIMENTAL_PARALLEL"] = "0"
    else:
        command = [
            _reasonix_path(),
            "run",
            "-p",
            "--dir",
            str(reasonix_dir),
            "--model",
            model,
            "--effort",
            effort,
            "--preset",
            preset,
            "--permission-mode",
            permission_mode,
            "--max-steps",
            str(max_steps),
            "--output-format",
            "text",
            "--add-dir",
            str(root),
        ]
        if allowed_tools:
            command.extend(["--allowed-tools", allowed_tools])
        if ablate:
            command.extend(["--ablate", ablate])
        env = None
    opencode_workdir = root / "tools" / "opencode-screening"
    working_dir = opencode_workdir if backend == "opencode" and opencode_workdir.is_dir() else root
    completed = subprocess.run(
        command,
        cwd=working_dir,
        check=False,
        input=_prompt(protocol, packets, require_web_search=require_web_search),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
        env=env,
    )
    if completed.returncode != 0:
        stderr_tail = completed.stderr[-800:].replace("\n", " ")
        raise RuntimeError(f"{backend} batch exited with {completed.returncode}: {stderr_tail}")
    completed_searches = _completed_opencode_websearch_events(completed.stdout) if backend == "opencode" else []
    if backend == "opencode" and require_web_search:
        missing_searches = _missing_websearch_companies(completed.stdout, packets)
        if missing_searches:
            raise ValueError(f"OpenCode did not complete websearch for companies: {missing_searches}")
    try:
        response_text = _extract_opencode_text(completed.stdout) if backend == "opencode" else completed.stdout
        reviews = _extract_array(response_text)
    except ValueError as error:
        tail = completed.stdout[-2000:].replace("\n", " ")
        stderr_tail = completed.stderr[-500:].replace("\n", " ")
        raise ValueError(f"{error}; stdout_tail={tail!r}; stderr_tail={stderr_tail!r}") from error
    expected = {(str(packet.get("security_code")), str(packet.get("type_key"))) for packet in packets}
    batch_companies = {
        str(packet.get("security_code")): str(packet.get("name") or "").strip()
        for packet in packets
        if str(packet.get("security_code") or "").strip()
    }
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for review in reviews:
        review = _normalise_model_review(review)
        if backend == "opencode" and not require_web_search:
            review = _cohere_local_review(review)
        key = (str(review.get("security_code")), str(review.get("type_key")))
        if key in actual:
            raise ValueError(f"duplicate AI review in batch: {key}")
        if key in expected and backend == "opencode" and require_web_search:
            evidence = _review_websearch_evidence(
                completed_searches,
                code=key[0],
                companies=batch_companies,
                claims=review.get("claims"),
            )
            if not evidence["queries"]:
                raise ValueError(f"OpenCode event proof is missing for AI review {key}")
            missing_claim_urls = set(evidence["missing_claim_urls"])
            if missing_claim_urls:
                review["claims"] = [
                    claim
                    for claim in (review.get("claims") or [])
                    if not isinstance(claim, Mapping)
                    or _canonical_web_url(claim.get("source_ref") or claim.get("source_context"))
                    not in missing_claim_urls
                ]
                risks = review.get("risk_flags") if isinstance(review.get("risk_flags"), list) else []
                risks.append(f"{len(missing_claim_urls)} 个模型引用未出现在该公司搜索结果中，发布前已移除")
                review["risk_flags"] = risks
            review["web_search_performed"] = True
            review["web_search_event_verified"] = True
            review["web_search_queries"] = evidence["queries"][:16]
            review["web_search_claim_urls_verified"] = True
            review["web_search_verified_claim_urls"] = evidence["verified_claim_urls"][:16]
            review["web_search_dropped_claim_url_count"] = len(missing_claim_urls)
        elif backend == "opencode" and not require_web_search:
            review["web_search_performed"] = False
            review["web_search_event_verified"] = False
            review["web_search_claim_urls_verified"] = False
            review["web_search_queries"] = []
            review["web_search_verified_claim_urls"] = []
            review["web_search_dropped_claim_url_count"] = 0
            review["claims"] = []
            risks = review.get("risk_flags") if isinstance(review.get("risk_flags"), list) else []
            risks.append("本条为本地模型复核，未执行联网搜索；不把模型记忆当作当前事实")
            review["risk_flags"] = risks
        errors = validate_review(review)
        if errors:
            summary = str(review.get("summary") or "").replace("\n", " ")[:240]
            raise ValueError(f"invalid AI review {key}: {','.join(errors)}; summary={summary!r}")
        actual[key] = review
        if require_web_search and review.get("web_search_performed") is not True:
            raise ValueError(f"required web search was not completed for {key}")
    missing = expected - set(actual)
    extra = set(actual) - expected
    if missing or extra or len(actual) != len(expected):
        raise ValueError(f"batch identity mismatch missing={sorted(missing)} extra={sorted(extra)}")
    for review in actual.values():
        review["model"] = model
        review["effort"] = effort
    active_session = _opencode_session_id(completed.stdout) if backend == "opencode" else ""
    if backend == "opencode" and not active_session:
        raise ValueError("OpenCode did not report a reusable session ID")
    return (
        [actual[(str(packet.get("security_code")), str(packet.get("type_key")))] for packet in packets],
        active_session,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=Path("tools/ai_screening_reasonix_prompt.md"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--session-batches",
        type=int,
        default=1,
        help="Reuse one OpenCode session for this many consecutive batches (default: 1).",
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="max")
    parser.add_argument("--preset", default="balanced", choices=("light", "balanced", "delivery"))
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--permission-mode", default="dontAsk", choices=("plan", "dontAsk", "auto"))
    parser.add_argument("--allowed-tools", default="")
    parser.add_argument("--ablate", default="none")
    parser.add_argument(
        "--backend",
        choices=("reasonix", "opencode"),
        default="reasonix",
        help="Use the legacy Reasonix wrapper or the OpenCode CLI directly.",
    )
    parser.add_argument(
        "--require-web-search",
        action="store_true",
        default=True,
        help="Require a completed provider search for every packet (the default).",
    )
    parser.add_argument(
        "--allow-unsearched",
        action="store_true",
        help="Explicitly opt out of the full-search gate for offline development only.",
    )
    parser.add_argument("--reasonix-dir", type=Path, default=Path("tools/reasonix-opencode-go"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    if (
        args.batch_size < 1
        or args.session_batches < 1
        or args.offset < 0
        or (args.limit is not None and args.limit < 1)
    ):
        raise SystemExit("batch-size/session-batches must be positive; offset non-negative; limit positive")
    if not args.reasonix_dir.is_absolute():
        args.reasonix_dir = (args.root / args.reasonix_dir).resolve()
    if args.backend == "reasonix" and not (args.reasonix_dir / "reasonix.toml").exists():
        raise SystemExit(f"missing Reasonix config: {args.reasonix_dir / 'reasonix.toml'}")
    packets = [json.loads(line) for line in args.candidates.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = packets[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    protocol = args.protocol.read_text(encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    error_path = args.out.with_name(args.out.stem + "-errors.jsonl")
    failed_batches = 0
    require_web_search = not args.allow_unsearched
    session_id: str | None = None
    session_batch_count = 0
    with args.out.open("w", encoding="utf-8") as output, error_path.open("w", encoding="utf-8") as errors:

        def run_or_split(batch: list[dict[str, Any]], start: int) -> None:
            nonlocal failed_batches, session_id, session_batch_count
            try:
                if session_batch_count >= args.session_batches:
                    session_id = None
                    session_batch_count = 0
                reviews, active_session = _run_batch(
                    batch,
                    protocol=protocol,
                    model=args.model,
                    effort=args.effort,
                    preset=args.preset,
                    max_steps=args.max_steps,
                    permission_mode=args.permission_mode,
                    allowed_tools=args.allowed_tools,
                    ablate=args.ablate,
                    require_web_search=require_web_search,
                    root=args.root,
                    reasonix_dir=args.reasonix_dir,
                    backend=args.backend,
                    session_id=session_id,
                )
                if args.backend == "opencode":
                    session_id = active_session
                    session_batch_count += 1
            except Exception as error:
                session_id = None
                session_batch_count = 0
                if not args.fail_fast and len(batch) > 1:
                    middle = len(batch) // 2
                    print(
                        json.dumps(
                            {
                                "offset": args.offset + start,
                                "count": len(batch),
                                "status": "retry_split",
                                "reason": str(error)[:500],
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                    )
                    run_or_split(batch[:middle], start)
                    run_or_split(batch[middle:], start + middle)
                    return
                failed_batches += 1
                record = {
                    "offset": args.offset + start,
                    "count": len(batch),
                    "error": str(error),
                    "security_codes": [packet.get("security_code") for packet in batch],
                }
                errors.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                errors.flush()
                print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
                if args.fail_fast:
                    raise
                return
            for review in reviews:
                output.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            print(json.dumps({"offset": args.offset + start, "count": len(reviews), "status": "ok"}))

        for start, batch in _company_batches(selected, args.batch_size):
            run_or_split(batch, start)
    if require_web_search and failed_batches:
        raise SystemExit(
            f"required web search failed for {failed_batches} batch(es); no complete AI artifact is publishable"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
