"""Check that AI-screening claim URLs are real, reachable web resources.

This audit does not decide whether a claim is financially correct.  It verifies
the narrower release facts that can be checked mechanically: every claimed URL
is public HTTP(S), the resource responds (or explicitly rate-limits the audit),
and the report records which links came from exchange/disclosure domains.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse


_OFFICIAL_DOMAIN_SUFFIXES = (
    "cninfo.com.cn",
    "sse.com.cn",
    "szse.cn",
    "bse.cn",
    "hkexnews.hk",
)
_BLOCKED_HTTP_STATUSES = frozenset({401, 403, 407, 429})
_REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})


class UnsafeUrlError(ValueError):
    """Raised when a URL can reach a non-public network destination."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def _public_http_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in url)
    ):
        return ""
    try:
        _ = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    return "" if not address.is_global else url


def _resolve_public_addresses(url: str) -> list[str]:
    public_url = _public_http_url(url)
    if not public_url:
        raise UnsafeUrlError("URL is not public HTTP(S)")
    parsed = urlparse(public_url)
    host = str(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {
        str(sockaddr[0]).split("%", 1)[0]
        for family, _socket_type, _protocol, _canonical_name, sockaddr in socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        if family in {socket.AF_INET, socket.AF_INET6} and sockaddr
    }
    if not addresses:
        raise OSError(f"DNS returned no A/AAAA addresses for {host}")
    non_public = sorted(address for address in addresses if not ipaddress.ip_address(address).is_global)
    if non_public:
        raise UnsafeUrlError(f"DNS resolved to non-public address(es): {','.join(non_public)}")
    return sorted(addresses)


def _official_domain(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _OFFICIAL_DOMAIN_SUFFIXES)


def _check_url(url: str, *, timeout: float, max_bytes: int, max_redirects: int = 5) -> dict[str, Any]:
    base = {"url": url, "official_market_domain": _official_domain(url)}
    opener = urllib.request.build_opener(_NoRedirectHandler())
    current_url = url
    redirect_count = 0
    while True:
        try:
            resolved_addresses = _resolve_public_addresses(current_url)
            request = urllib.request.Request(
                current_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DS-DCF-source-audit/1.0)",
                    "Accept": "text/html,application/pdf,application/json;q=0.9,*/*;q=0.8",
                },
            )
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes)
                status = int(response.status or 200)
                return {
                    **base,
                    "result": "ok" if 200 <= status < 400 else "failed",
                    "status": status,
                    "final_url": current_url,
                    "redirect_count": redirect_count,
                    "resolved_addresses": resolved_addresses,
                    "content_type": str(response.headers.get("content-type") or "")[:160],
                    "bytes_checked": len(body),
                }
        except urllib.error.HTTPError as error:
            status = int(error.code)
            if status in _REDIRECT_HTTP_STATUSES:
                location = str(error.headers.get("location") or "").strip()
                error.close()
                if not location:
                    return {
                        **base,
                        "result": "failed",
                        "status": status,
                        "error": "redirect response has no Location header",
                    }
                if redirect_count >= max_redirects:
                    return {
                        **base,
                        "result": "failed",
                        "status": status,
                        "error": f"redirect limit exceeded ({max_redirects})",
                    }
                current_url = urljoin(current_url, location)
                redirect_count += 1
                continue
            return {
                **base,
                "result": "blocked" if status in _BLOCKED_HTTP_STATUSES else "failed",
                "status": status,
                "error": str(error.reason or error)[:240],
            }
        except UnsafeUrlError as error:
            return {
                **base,
                "result": "invalid",
                "status": 0,
                "final_url": current_url,
                "error": str(error)[:240],
            }
        except (OSError, urllib.error.URLError, ValueError) as error:
            return {**base, "result": "failed", "status": 0, "error": str(error)[:240]}


def audit(
    merged_path: Path,
    output_path: Path,
    *,
    workers: int = 16,
    timeout: float = 15.0,
    max_bytes: int = 262_144,
) -> dict[str, Any]:
    merged_bytes = merged_path.read_bytes()
    payload = json.loads(merged_bytes.decode("utf-8"))
    packets = payload.get("packets")
    if not isinstance(packets, list):
        raise ValueError("AI screening packets are missing")
    references: dict[str, set[tuple[str, str]]] = {}
    claim_count = 0
    invalid_claim_urls: list[dict[str, str]] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ValueError("AI screening packet is not an object")
        review = packet.get("ai_review")
        if not isinstance(review, Mapping):
            continue
        code = str(packet.get("security_code") or "")
        type_key = str(packet.get("type_key") or "")
        claims = review.get("claims") if isinstance(review.get("claims"), list) else []
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            claim_count += 1
            raw = claim.get("source_ref") or claim.get("source_context")
            url = _public_http_url(raw)
            if not url:
                invalid_claim_urls.append({"security_code": code, "type_key": type_key, "source": str(raw or "")[:240]})
                continue
            references.setdefault(url, set()).add((code, type_key))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        results = list(
            executor.map(
                lambda url: _check_url(url, timeout=timeout, max_bytes=max_bytes),
                sorted(references),
            )
        )
    for result in results:
        result["references"] = [
            {"security_code": code, "type_key": type_key} for code, type_key in sorted(references[result["url"]])
        ]
        if result["result"] == "invalid":
            invalid_claim_urls.extend(
                {
                    "security_code": code,
                    "type_key": type_key,
                    "source": result["url"][:240],
                    "reason": str(result.get("error") or "unsafe destination")[:240],
                }
                for code, type_key in sorted(references[result["url"]])
            )
    counts = {key: sum(result["result"] == key for result in results) for key in ("ok", "failed", "blocked", "invalid")}
    report = {
        "merged_sha256": hashlib.sha256(merged_bytes).hexdigest(),
        "snapshot_generation": payload.get("snapshot_generation"),
        "market_as_of": payload.get("market_as_of"),
        "checked": len(results),
        **counts,
        "claim_count": claim_count,
        "invalid_claim_url_count": len(invalid_claim_urls),
        "official_market_domain_count": sum(bool(result["official_market_domain"]) for result in results),
        "invalid_claim_urls": invalid_claim_urls,
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--max-bytes", type=int, default=262_144)
    args = parser.parse_args()
    if args.workers < 1 or args.timeout <= 0 or args.max_bytes < 1:
        raise SystemExit("workers, timeout and max-bytes must be positive")
    report = audit(
        args.merged,
        args.output,
        workers=args.workers,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )
    print(json.dumps({key: report[key] for key in ("checked", "ok", "failed", "blocked")}, sort_keys=True))
    return 1 if report["invalid_claim_url_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
