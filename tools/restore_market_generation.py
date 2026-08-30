"""Restore a retained, signed market generation without rebuilding market data."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen


SITE = "https://quant.custard.top"
RELEASE = "https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data"
DATABASE_ID = "1ea1f08e-640f-4e75-a25e-c47d0a41ae66"
RESTORE_SQL = """
UPDATE current_generation SET generation_id = ?, updated_at = ?
WHERE singleton = 1 AND generation_id = ?
AND EXISTS (SELECT 1 FROM generations WHERE generation_id = ? AND manifest_sha256 = ?)
"""


def read_url(url: str) -> bytes:
    if not (url.startswith(f"{SITE}/") or url.startswith(f"{RELEASE}/")):
        raise ValueError("Recovery downloads must use the fixed website or retained release URLs")
    # Both allowed roots are fixed HTTPS endpoints; release redirects carry no credentials.
    with urlopen(Request(url, headers={"User-Agent": "DS-DCF-market-recovery"}), timeout=60) as response:  # nosec B310
        return response.read()


def check_health(generation: str) -> dict:
    health = json.loads(read_url(f"{SITE}/api/health?generation_id={generation}&deep=1"))
    if health.get("generation_id") != generation or not all(
        health.get(key) is True for key in ("ok", "integrity_ok", "signature_ok", "company_details_ready")
    ):
        raise ValueError("The retained generation did not pass the live signature/integrity check")
    return health


def d1_query(sql: str, params: list[str]) -> dict:
    account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    token = os.environ["CLOUDFLARE_API_TOKEN"]
    request = Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{DATABASE_ID}/query",
        data=json.dumps({"sql": sql, "params": params}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    # The authenticated destination is the fixed Cloudflare HTTPS API above.
    with urlopen(request, timeout=60) as response:  # nosec B310
        payload = json.load(response)
    if payload.get("success") is not True or len(payload.get("result", [])) != 1:
        raise RuntimeError("Cloudflare did not confirm the recovery query")
    result = payload["result"][0]
    if result.get("success") is not True:
        raise RuntimeError("Cloudflare rejected the recovery query")
    return result


def restore(generation: str, expected_current: str, manifest_sha256: str) -> dict:
    check_health(generation)
    before = d1_query("SELECT generation_id FROM current_generation WHERE singleton=1", [])["results"]
    if len(before) == 1 and before[0]["generation_id"] == generation:
        current = json.loads(read_url(f"{SITE}/api/meta"))
        if current.get("generation_id") != generation or current.get("manifest_sha256") != manifest_sha256:
            raise ValueError("The already-restored generation does not match the approved manifest")
        return {"restored_generation": generation, "already_restored": True, "health": check_health(generation)}
    if len(before) != 1 or before[0]["generation_id"] != expected_current:
        raise ValueError("The current generation changed; refusing to overwrite another publication")
    result = d1_query(
        RESTORE_SQL,
        [generation, datetime.now(timezone.utc).isoformat(), expected_current, generation, manifest_sha256],
    )
    if result.get("meta", {}).get("changes") != 1:
        raise RuntimeError("No pointer was changed: current generation or target manifest no longer matches")
    after = json.loads(read_url(f"{SITE}/api/meta"))
    if after.get("generation_id") != generation or after.get("manifest_sha256") != manifest_sha256:
        raise RuntimeError("The public website has not confirmed the restored generation")
    return {
        "previous_generation": expected_current,
        "restored_generation": generation,
        "health": check_health(generation),
    }


def manifest_assets(manifest: dict, generation: str) -> list[dict]:
    assets = [manifest["catalogue"], manifest["signals"], *manifest["company_details"]["shards"]]
    expected = [f"catalog-{generation}.json.gz", f"signals-{generation}.json.gz"] + [
        f"company-details-{generation}-{index:02x}.json.gz" for index in range(16)
    ]
    if [asset["filename"] for asset in assets] != expected:
        raise ValueError("The retained manifest does not name exactly one complete generation")
    if manifest["signature"]["filename"] != f"manifest-{generation}.sig":
        raise ValueError("The signature does not belong to the retained generation")
    return assets


def prepare_pages(generation: str, manifest_sha256: str, pages_dir: Path) -> dict:
    data_dir = pages_dir / "mobile-data"
    manifest_path = data_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise ValueError("The downloaded R2 manifest differs from the approved rollback target")
    manifest = json.loads(manifest_bytes)
    assets = manifest_assets(manifest, generation)

    def download(asset: dict) -> None:
        content = read_url(f"{RELEASE}/{asset['filename']}")
        if len(content) != asset["size"] or hashlib.sha256(content).hexdigest() != asset["sha256"]:
            raise ValueError(f"The retained release asset failed checksum verification: {asset['filename']}")
        (data_dir / asset["filename"]).write_bytes(content)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download, assets))
    signature_path = data_dir / manifest["signature"]["filename"]
    signature_path.write_bytes(read_url(f"{RELEASE}/{signature_path.name}"))
    # Node's built-in verifier uses the same checked-in public key as both Workers.
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import fs from 'node:fs'; import crypto from 'node:crypto';"
            "const [key,manifest,signature]=process.argv.slice(1);"
            "const publicKey=crypto.createPublicKey({key:Buffer.from(fs.readFileSync(key,'utf8').trim(),'base64'),format:'der',type:'spki'});"
            "if(!crypto.verify('sha256',fs.readFileSync(manifest),publicKey,fs.readFileSync(signature)))process.exit(1);",
            str(Path(__file__).resolve().parents[1] / "cloudflare/quant-dashboard/market_signing_public_key.txt"),
            str(manifest_path),
            str(signature_path),
        ],
        check=True,
    )
    (pages_dir / "index.html").write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        "<title>DS_DCF 网站市场数据服务</title><h1>DS_DCF 网站市场数据服务</h1>"
        '<p><a href="mobile-data/manifest.json">查看当前签名数据清单</a></p></html>\n',
        encoding="utf-8",
    )
    return {"generation_id": generation, "verified_assets": len(assets) + 2}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("switch", "prepare"))
    parser.add_argument("--generation", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--expected-current")
    parser.add_argument("--pages-dir", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{16}", args.generation) or not re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256):
        parser.error("generation and manifest checksum must be lowercase hexadecimal identifiers")
    if args.action == "switch":
        if not re.fullmatch(r"[0-9a-f]{16}", args.expected_current or "") or args.expected_current == args.generation:
            parser.error("switch requires a different expected current generation")
        result = restore(args.generation, args.expected_current, args.manifest_sha256)
    else:
        if args.pages_dir is None:
            parser.error("prepare requires --pages-dir")
        result = prepare_pages(args.generation, args.manifest_sha256, args.pages_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
