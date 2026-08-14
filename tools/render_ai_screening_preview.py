"""Render the local AI screening artifact as a dependency-free HTML preview."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


VERDICT_LABELS = {
    "confirmed": "AI\u590d\u6838\u901a\u8fc7",
    "caution": "\u8c28\u614e\u89c2\u5bdf",
    "misclassified": "\u7591\u4f3c\u8bef\u5224",
    "missed_candidate": "\u7591\u4f3c\u6f0f\u5224",
    "needs_review": "\u5f85\u6838\u9a8c",
}


def _text(value: object) -> str:
    return html.escape("" if value is None else str(value))


def render(payload: dict) -> str:
    packets = payload.get("packets") or []
    rows = []
    for packet in packets:
        deterministic = packet.get("deterministic") or {}
        review = packet.get("ai_review") or {}
        verdict = str(review.get("verdict") or "needs_review")
        label = VERDICT_LABELS.get(verdict, verdict)
        score = deterministic.get("score")
        if score is None:
            score = deterministic.get("total_score")
        summary = review.get("summary") or "\u7b49\u5f85 Reasonix \u590d\u6838"
        rows.append(
            f'<tr data-verdict="{_text(verdict)}" data-type="{_text(packet.get("type_key"))}">'
            f"<td><strong>{_text(packet.get('security_code'))}</strong><br>{_text(packet.get('name'))}</td>"
            f"<td>{_text(packet.get('type_key'))}</td>"
            f"<td>{_text(deterministic.get('status'))}<br>{_text(score)}</td>"
            f'<td><span class="verdict verdict-{_text(verdict)}">{_text(label)}</span></td>'
            f"<td>{_text(summary)}</td></tr>"
        )
    generation = _text(payload.get("snapshot_generation"))
    market_as_of = _text(payload.get("market_as_of"))
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI\u7b5b\u67e5 · \u672c\u5730\u9884\u89c8</title>
<style>
:root{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#16202a;background:#f4f7fa}}
body{{margin:0;padding:32px}}main{{max-width:1180px;margin:auto}}
.hero,.card{{background:white;border:1px solid #dce5ec;border-radius:16px;padding:22px;box-shadow:0 8px 24px #19324b0d}}
h1{{margin:0 0 8px}}.muted{{color:#617181}}.toolbar{{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}}
label{{display:flex;gap:8px;align-items:center;color:#506274;font-size:14px}}
select{{border:1px solid #dce5ec;border-radius:8px;padding:7px 10px;background:white}}
table{{width:100%;border-collapse:collapse;margin-top:18px}}th,td{{padding:12px 10px;border-bottom:1px solid #e7edf2;text-align:left;vertical-align:top}}
th{{color:#506274;font-size:13px}}.verdict{{display:inline-block;padding:4px 9px;border-radius:999px;background:#edf2f7}}
.verdict-confirmed{{background:#dff5e7;color:#146b3a}}.verdict-caution,.verdict-needs_review{{background:#fff1cf;color:#825c00}}
.verdict-misclassified{{background:#ffe0e0;color:#9e2929}}.verdict-missed_candidate{{background:#e4e0ff;color:#49389a}}
</style></head><body><main>
<section class="hero"><h1>AI\u7b5b\u67e5</h1><p class="muted">\u672c\u5730\u9884\u89c8 · AI\u590d\u6838\u4e0d\u4fee\u6539\u4e03\u7c7b\u786e\u5b9a\u6027\u8bc4\u5206</p>
<p>\u6570\u636e\u4ee3\u9645：{generation}　\u4ea4\u6613\u65e5：{market_as_of}　\u5019\u9009\u5bf9：<span id="count">{len(packets)}</span></p>
<div class="toolbar"><label>\u7ed3\u8bba<select id="verdict-filter"><option value="all">\u5168\u90e8</option>{''.join(f'<option value="{_text(k)}">{_text(v)}</option>' for k, v in VERDICT_LABELS.items())}</select></label>
<label>\u7c7b\u578b<select id="type-filter"><option value="all">\u5168\u90e8</option>{''.join(f'<option value="type{n}">Type{n}</option>' for n in range(1, 8))}</select></label></div></section>
<section class="card" style="margin-top:18px"><table><thead><tr><th>\u516c\u53f8</th><th>\u7c7b\u578b</th><th>\u89c4\u5219\u7ed3\u679c</th><th>AI\u590d\u6838</th><th>\u6458\u8981</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section></main>
<script>
const apply=()=>{{const verdict=document.querySelector('#verdict-filter').value;const type=document.querySelector('#type-filter').value;let n=0;document.querySelectorAll('tbody tr').forEach(row=>{{const show=(verdict==='all'||row.dataset.verdict===verdict)&&(type==='all'||row.dataset.type===type);row.hidden=!show;if(show)n++;}});document.querySelector('#count').textContent=n;}};
document.querySelectorAll('select').forEach(el=>el.addEventListener('change',apply));
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.write_text(render(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
