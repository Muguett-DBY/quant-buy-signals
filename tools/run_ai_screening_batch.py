"""Run OpenCode Go reviews in a small number of cache-friendly batches.

The deterministic candidate packet is still read-only.  This command groups
packets by a shared rule context, asks Reasonix for a JSON array, and writes
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


def _extract_array(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
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
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    for start, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("reviews"), list):
            reviews = value["reviews"]
            if all(isinstance(item, dict) for item in reviews):
                return reviews
    raise ValueError("Reasonix did not return a JSON review array")


def _batch_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Drop duplicate rule prose; the batch carries it once per type."""
    return {key: value for key, value in packet.items() if key != "rule_context"}


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


def _prompt(protocol: str, packets: list[Mapping[str, Any]]) -> str:
    shared = _shared_rules(packets)
    slim = [_batch_packet(packet) for packet in packets]
    return (
        protocol
        + "\n\nBatch review instructions:\n"
        + "Review every packet independently. The output array must contain exactly one object "
        + "for every packet, preserving security_code and type_key. Do not omit a packet.\n"
        + "Shared rule fragments are keyed by type_key; use the matching fragments when useful:\n"
        + json.dumps(shared, ensure_ascii=False, sort_keys=True)
        + "\nPackets (JSON array):\n"
        + json.dumps(slim, ensure_ascii=False, sort_keys=True)
        + "\nIMPORTANT: for this batch, override the protocol's single-packet output example: "
        + "return exactly one JSON array containing one review object per packet, and no Markdown."
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
    root: Path,
    reasonix_dir: Path,
) -> list[dict[str, Any]]:
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
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        input=_prompt(protocol, packets),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    try:
        reviews = _extract_array(completed.stdout)
    except ValueError as error:
        tail = completed.stdout[-2000:].replace("\n", " ")
        stderr_tail = completed.stderr[-500:].replace("\n", " ")
        raise ValueError(f"{error}; stdout_tail={tail!r}; stderr_tail={stderr_tail!r}") from error
    if completed.returncode != 0:
        print(
            f"warning: Reasonix batch returned exit={completed.returncode} after a parseable result",
            file=sys.stderr,
        )
    expected = {(str(packet.get("security_code")), str(packet.get("type_key"))) for packet in packets}
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for review in reviews:
        key = (str(review.get("security_code")), str(review.get("type_key")))
        if key in actual:
            raise ValueError(f"duplicate AI review in batch: {key}")
        errors = validate_review(review)
        if errors:
            raise ValueError(f"invalid AI review {key}: {','.join(errors)}")
        actual[key] = review
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
    parser.add_argument("--reasonix-dir", type=Path, default=Path("tools/reasonix-opencode-go"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1 or args.offset < 0 or (args.limit is not None and args.limit < 1):
        raise SystemExit("batch-size must be positive; offset non-negative; limit positive")
    if not args.reasonix_dir.is_absolute():
        args.reasonix_dir = (args.root / args.reasonix_dir).resolve()
    if not (args.reasonix_dir / "reasonix.toml").exists():
        raise SystemExit(f"missing Reasonix config: {args.reasonix_dir / 'reasonix.toml'}")
    packets = [json.loads(line) for line in args.candidates.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = packets[args.offset :]
    if args.limit is not None:
        selected = selected[: args.limit]
    protocol = args.protocol.read_text(encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    error_path = args.out.with_name(args.out.stem + "-errors.jsonl")
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
                    root=args.root,
                    reasonix_dir=args.reasonix_dir,
                )
            except Exception as error:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
