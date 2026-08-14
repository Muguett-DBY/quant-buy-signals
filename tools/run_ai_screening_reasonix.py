"""Run a bounded local AI second-pass through Reasonix/OpenCode Go.

This command is intentionally local-only.  It writes review JSONL and never
changes the deterministic snapshot or sends model credentials to Cloudflare.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.ai_screening_contract import validate_review


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    decoder = json.JSONDecoder()
    for start, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Reasonix did not return a valid JSON object")


def _prompt(protocol: str, packet: dict[str, Any], *, require_web_search: bool = False) -> str:
    search_instruction = ""
    if require_web_search:
        search_instruction = (
            "\nMANDATORY WEB SEARCH: Call the provider web_search tool before reviewing this packet. "
            "Search the company code and name on CNINFO, SSE/SZSE, HKEX when applicable, and the "
            "company investor-relations or official filing page. Use only returned URLs. Set "
            "web_search_performed=true only after searching; include an https source_ref when found. "
            "If no reliable source is returned, set web_search_performed=false, confidence=low, and "
            "do not promote the company to priority_buy.\n"
        )
    return (
        protocol
        + search_instruction
        + "\n\nReview packet (JSON; deterministic fields are read-only):\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True)
        + "\n\nReturn exactly one JSON object using the protocol schema."
    )


def run_one(
    packet: dict[str, Any],
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
    reasonix_dir: Path | None,
) -> dict[str, Any]:
    reasonix = shutil.which("reasonix.cmd") or shutil.which("reasonix")
    if not reasonix:
        appdata = os.environ.get("APPDATA", "")
        candidate = Path(appdata) / "npm" / "reasonix.cmd"
        if candidate.exists():
            reasonix = str(candidate)
    if not reasonix:
        raise FileNotFoundError("reasonix.cmd is not on PATH")
    command = [
        reasonix,
        "run",
        "-p",
    ]
    if reasonix_dir:
        command.extend(["--dir", str(reasonix_dir)])
    command.extend(
        [
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
    )
    if allowed_tools:
        command.extend(["--allowed-tools", allowed_tools])
    if ablate:
        command.extend(["--ablate", ablate])
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        input=_prompt(protocol, packet, require_web_search=require_web_search),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    try:
        review = _extract_json(completed.stdout)
    except (ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Reasonix failed for {packet.get('security_code')}/{packet.get('type_key')}: "
            f"exit={completed.returncode} stderr={completed.stderr[-1000:]}"
        ) from error
    if completed.returncode != 0:
        print(
            f"warning: Reasonix returned exit={completed.returncode} after a parseable result "
            f"for {packet.get('security_code')}/{packet.get('type_key')}",
            file=sys.stderr,
        )
    errors = validate_review(review)
    expected = (str(packet.get("security_code")), str(packet.get("type_key")))
    actual = (str(review.get("security_code")), str(review.get("type_key")))
    if actual != expected:
        errors.append("candidate_identity")
    if errors:
        raise ValueError(f"invalid AI review {expected}: {','.join(errors)}")
    review["model"] = model
    review["effort"] = effort
    if require_web_search and review.get("web_search_performed") is not True:
        print(f"warning: web search was unavailable for {expected}; keeping low-confidence review", file=sys.stderr)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, default=Path("tools/ai_screening_reasonix_prompt.md"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--model", default="opencode-go/deepseek-v4-flash")
    parser.add_argument("--effort", default="max")
    parser.add_argument("--preset", default="balanced", choices=("light", "balanced", "delivery"))
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--permission-mode", default="dontAsk", choices=("plan", "dontAsk", "auto"))
    parser.add_argument(
        "--allowed-tools",
        default="",
        help="Optional extra tools. OpenCode Go search is provider-native; leave empty for the safe default.",
    )
    parser.add_argument("--ablate", default="none")
    parser.add_argument("--require-web-search", action="store_true")
    parser.add_argument(
        "--reasonix-dir",
        type=Path,
        default=Path("tools/reasonix-opencode-go"),
        help="directory containing the isolated OpenCode Go Reasonix config",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    if args.limit < 1 or args.offset < 0:
        raise SystemExit("limit must be positive and offset must be non-negative")
    if not args.reasonix_dir.is_absolute():
        args.reasonix_dir = (args.root / args.reasonix_dir).resolve()
    if not (args.reasonix_dir / "reasonix.toml").exists():
        raise SystemExit(f"missing Reasonix config: {args.reasonix_dir / 'reasonix.toml'}")
    lines = [line for line in args.candidates.read_text(encoding="utf-8").splitlines() if line.strip()]
    packets = [json.loads(line) for line in lines[args.offset : args.offset + args.limit]]
    protocol = args.protocol.read_text(encoding="utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    error_path = args.out.with_name(args.out.stem + "-errors.jsonl")
    with args.out.open("w", encoding="utf-8") as handle, error_path.open("w", encoding="utf-8") as errors:
        for packet in packets:
            try:
                review = run_one(
                    packet,
                    protocol=protocol,
                    model=args.model,
                    effort=args.effort,
                    preset=args.preset,
                    max_steps=args.max_steps,
                    permission_mode=args.permission_mode,
                    allowed_tools=args.allowed_tools,
                    ablate=args.ablate,
                    require_web_search=args.require_web_search,
                    root=args.root,
                    reasonix_dir=args.reasonix_dir,
                )
            except Exception as error:
                error_record = {
                    "security_code": packet.get("security_code"),
                    "type_key": packet.get("type_key"),
                    "error": str(error),
                }
                errors.write(json.dumps(error_record, ensure_ascii=False, sort_keys=True) + "\n")
                errors.flush()
                print(json.dumps(error_record, ensure_ascii=False), file=sys.stderr)
                if args.fail_fast:
                    raise
                continue
            handle.write(json.dumps(review, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            print(
                json.dumps(
                    {
                        "security_code": review["security_code"],
                        "type_key": review["type_key"],
                        "verdict": review["verdict"],
                    },
                    ensure_ascii=False,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
