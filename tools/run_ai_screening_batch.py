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

from tools.ai_screening_contract import validate_review


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


def _batch_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Drop duplicate rule prose; the batch carries it once per type."""
    return {key: value for key, value in packet.items() if key != "rule_context"}


def _normalise_model_review(review: dict[str, Any]) -> dict[str, Any]:
    """Drop source-less claim shells without inventing provenance.

    The protocol permits an empty claims list when a search found no reliable
    source.  MiMo occasionally emits an otherwise empty claim object in that
    case; removing only that object preserves the review and lets publication
    expose the lower source confidence instead of losing a whole batch.
    """
    claims = review.get("claims")
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


def _shared_rules(packets: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for packet in packets:
        type_key = str(packet.get("type_key") or "")
        for rule in packet.get("rule_context", []):
            if not isinstance(rule, Mapping):
                continue
            key = "|".join(str(rule.get(field) or "") for field in ("source_id", "line_start", "heading"))
            grouped[type_key][key] = dict(rule)
    return {type_key: list(values.values()) for type_key, values in sorted(grouped.items())}


def _prompt(protocol: str, packets: list[Mapping[str, Any]], *, require_web_search: bool = False) -> str:
    shared = _shared_rules(packets)
    slim = [_batch_packet(packet) for packet in packets]
    search_instruction = ""
    if require_web_search:
        search_instruction = (
            "\nMANDATORY WEB SEARCH: Before writing each review, call the available websearch/web_search tool "
            "for that packet. Search the company code and name against CNINFO, SSE/SZSE, HKEX when "
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
        )
    return (
        protocol
        + search_instruction
        + "\n\nBatch review instructions:\n"
        + "Review every packet independently. The output array must contain exactly one object "
        + "for every packet, preserving security_code and type_key. Do not omit a packet.\n"
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
        + "Do not describe a plan or ask for permission: execute the available websearch tool now, then return the JSON array. "
        + "For every packet, actually search the company code/name and prefer a returned official source. "
        + "If a usable source is returned, put its exact URL in claims[].source_ref; never invent or rewrite a URL. "
        + "Keep every object compact: summary <= 280 Chinese characters, at most 2 strengths, "
        + "3 risks, and 3 source claims; do not repeat facts."
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
) -> list[dict[str, Any]]:
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
        env = os.environ.copy()
        env["OPENCODE_ENABLE_EXA"] = "1"
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
    try:
        response_text = _extract_opencode_text(completed.stdout) if backend == "opencode" else completed.stdout
        reviews = _extract_array(response_text)
    except ValueError as error:
        tail = completed.stdout[-2000:].replace("\n", " ")
        stderr_tail = completed.stderr[-500:].replace("\n", " ")
        raise ValueError(f"{error}; stdout_tail={tail!r}; stderr_tail={stderr_tail!r}") from error
    if completed.returncode != 0:
        print(
            f"warning: {backend} batch returned exit={completed.returncode} after a parseable result",
            file=sys.stderr,
        )
    expected = {(str(packet.get("security_code")), str(packet.get("type_key"))) for packet in packets}
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for review in reviews:
        review = _normalise_model_review(review)
        key = (str(review.get("security_code")), str(review.get("type_key")))
        if key in actual:
            raise ValueError(f"duplicate AI review in batch: {key}")
        errors = validate_review(review)
        if errors:
            raise ValueError(f"invalid AI review {key}: {','.join(errors)}")
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
    return [actual[(str(packet.get("security_code")), str(packet.get("type_key")))] for packet in packets]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=Path("tools/ai_screening_reasonix_prompt.md"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--model", default="opencode-go/deepseek-v4-flash")
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
    if args.batch_size < 1 or args.offset < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("batch-size must be positive; offset non-negative; limit positive")
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
    with args.out.open("w", encoding="utf-8") as output, error_path.open("w", encoding="utf-8") as errors:
        for start in range(0, len(selected), args.batch_size):
            batch = selected[start : start + args.batch_size]
            try:
                reviews = _run_batch(
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
                )
            except Exception as error:
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
                continue
            for review in reviews:
                output.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            print(json.dumps({"offset": args.offset + start, "count": len(reviews), "status": "ok"}))
    if require_web_search and failed_batches:
        raise SystemExit(
            f"required web search failed for {failed_batches} batch(es); no complete AI artifact is publishable"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
