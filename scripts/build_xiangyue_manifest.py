"""Pull 67 merchant assets cloud:// fileIDs from Supabase merchants table → write TSV manifest.

Reads from Supabase REST (PostgREST). Designed to run on the production server (or Sam's
laptop) where SUPABASE_URL + SUPABASE_KEY env vars are set. Output TSV is fed to
`migrate_xiangyue.py` which downloads via tcb CLI and uploads to QideDAM.

Usage:
    export SUPABASE_URL="https://<xiangyue-project>.supabase.co"
    export SUPABASE_KEY="<anon-or-service-role-key>"
    python -m scripts.build_xiangyue_manifest \\
        --out /tmp/xiangyue_manifest.tsv

Output rows (TAB separated, matches migrate_xiangyue.py format):
    <fileID>\\t<local_filename>\\t<merchant_name>

Notes:
- Pulls ALL `live` merchants from `source='merchant_form_2026'`
- Extracts cloud:// IDs from 3 columns: logo_url (single str) / cover_image (single str) / images (jsonb array of str)
- Skips rows whose URL doesn't start with `cloud://`
- Local filename derived from the cloud:// path tail (strip dirs)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def fetch_merchants(supabase_url: str, supabase_key: str) -> list[dict]:
    """Pull all live xiangyue merchants from Supabase REST."""
    qs = urllib.parse.urlencode({
        "select": "id,name,logo_url,cover_image,images,source,status",
        "source": "eq.merchant_form_2026",
        "status": "eq.live",
    })
    url = f"{supabase_url.rstrip('/')}/rest/v1/merchants?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def extract_cloud_ids(merchant: dict) -> list[tuple[str, str]]:
    """Return [(file_id, local_filename), ...] for one merchant."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(value: str | None):
        if not value or not isinstance(value, str):
            return
        if not value.startswith("cloud://"):
            return
        if value in seen:
            return
        seen.add(value)
        # local_filename = path tail; strip query-string just in case
        tail = value.split("?", 1)[0].rsplit("/", 1)[-1]
        if not tail:
            tail = f"file_{len(rows):03d}.bin"
        rows.append((value, tail))

    _add(merchant.get("logo_url"))
    _add(merchant.get("cover_image"))
    images = merchant.get("images") or []
    if isinstance(images, list):
        for img in images:
            _add(img if isinstance(img, str) else None)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, type=Path, help="Output TSV path")
    ap.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL"))
    ap.add_argument("--supabase-key", default=os.environ.get("SUPABASE_KEY"))
    args = ap.parse_args()

    if not args.supabase_url or not args.supabase_key:
        print("[ERR] SUPABASE_URL + SUPABASE_KEY required (env or CLI flag)", file=sys.stderr)
        return 2

    merchants = fetch_merchants(args.supabase_url, args.supabase_key)
    print(f"[INFO] Fetched {len(merchants)} live merchants from {args.supabase_url}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    by_merchant: dict[str, int] = {}
    with args.out.open("w", encoding="utf-8") as fh:
        for m in merchants:
            name = m.get("name") or f"merchant-{m.get('id')}"
            for file_id, filename in extract_cloud_ids(m):
                fh.write(f"{file_id}\t{filename}\t{name}\n")
                total += 1
                by_merchant[name] = by_merchant.get(name, 0) + 1

    print(f"[OK] Wrote {total} assets across {len(by_merchant)} merchants → {args.out}")
    for name, cnt in sorted(by_merchant.items(), key=lambda kv: -kv[1]):
        print(f"     {cnt:>3}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
